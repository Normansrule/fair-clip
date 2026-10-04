"""Local state: one SQLite database plus a content-addressed media folder.

Media files are stored by their SHA-256 (``media/ab/abcdef...mp4``), so a file can never be
swapped under an ID without the hash changing, and the dashboard never builds a file path from
user input.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .util import now, sha256_file

__all__ = ["CLIP_STATES", "Store", "now", "sha256_file"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, created TEXT, rights TEXT, media_sha TEXT, probe TEXT,
  transcript TEXT, stages TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, source_id TEXT, rank INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY, source_id TEXT, created TEXT, plan TEXT, metadata TEXT,
  clip_sha TEXT, preview_sha TEXT, posters TEXT, summary TEXT, body TEXT, gate TEXT, status TEXT, decision TEXT, upload TEXT,
  fingerprint TEXT, logs TEXT);
CREATE TABLE IF NOT EXISTS weight_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, current TEXT, proposed TEXT,
  evidence TEXT, status TEXT, decided_by TEXT);
"""

# Clip lifecycle. Only "approved" clips can be uploaded; only the publisher sets "published".
CLIP_STATES = (
    "planned", "rendered", "blocked", "awaiting_approval", "approved", "rejected",
    "uploading", "published", "dry_run_published", "deferred_quota", "failed",
)


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir)
        (self.root / "media").mkdir(parents=True, exist_ok=True)
        (self.root / "work").mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.root / "fairclip.sqlite3", check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        # One connection is shared by the dashboard's threads: serialize every write through this lock.
        self.lock = threading.RLock()
        self.audit = AuditLog(self.conn, self.lock)

    # ------------------------------------------------------------------ key/value settings
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value)),
            )

    def update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """Read-modify-write one key atomically, across threads AND processes (the CLI and the
        dashboard are separate processes): BEGIN IMMEDIATE takes the database write lock first."""
        with self.lock:
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
                new = fn(json.loads(row[0]) if row else None)
                self.conn.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                                  (key, json.dumps(new)))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
        return new

    # ------------------------------------------------------------------ kill switch
    @property
    def kill_switch(self) -> bool:
        return bool(self.get("kill_switch", False)) or (self.root / "KILL").exists()

    def set_kill_switch(self, on: bool, actor: str) -> None:
        self.set("kill_switch", on)
        flag = self.root / "KILL"
        if on:
            flag.write_text(f"kill switch engaged by {actor} at {now()}\n")
        elif flag.exists():
            flag.unlink()
        self.audit.append(actor, "kill_switch", "global", {"on": on})

    # ------------------------------------------------------------------ media (content-addressed)
    def media_path(self, sha: str, suffix: str = ".mp4") -> Path:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("bad media id")
        return self.root / "media" / sha[:2] / f"{sha}{suffix}"

    def put_media(self, src: Path, suffix: str | None = None, move: bool = False) -> str:
        """Copy a file into the store under its SHA-256. Always a *copy*: the stored file is created by
        this process (so we own it and can make it read-only), even when the source was written by the
        media worker running as another user. The hash is taken from the copy itself."""
        tmp = self.root / "media" / f".incoming-{uuid.uuid4().hex}"
        shutil.copyfile(src, tmp)
        sha = sha256_file(tmp)
        dst = self.media_path(sha, suffix or src.suffix.lower())
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            tmp.unlink()
        else:
            tmp.chmod(0o444)  # media is immutable once stored
            os.replace(tmp, dst)
        if move:
            try:
                src.unlink()
            except OSError:
                pass  # the worker's output folder may not let us delete; it's cleaned with the job
        return sha

    def find_media(self, sha: str) -> Path:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("bad media id")
        d = self.root / "media" / sha[:2]
        for p in d.glob(f"{sha}.*"):
            return p
        raise FileNotFoundError(sha)

    # ------------------------------------------------------------------ rows as dicts
    def row(self, table: str, id_: str) -> dict[str, Any] | None:
        assert table in {"sources", "candidates", "clips"}
        cur = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (id_,))  # noqa: S608 (table allowlisted)
        r = cur.fetchone()
        if r is None:
            return None
        cols = [d[0] for d in cur.description]
        return {c: _maybe_json(v) for c, v in zip(cols, r, strict=True)}

    def rows(self, table: str, where: str = "", args: tuple[Any, ...] = (), order: str = "") -> list[dict[str, Any]]:
        assert table in {"sources", "candidates", "clips", "weight_proposals"}
        q = f"SELECT * FROM {table}"  # noqa: S608 (table allowlisted; where/order are code constants)
        if where:
            q += f" WHERE {where}"
        if order:
            q += f" ORDER BY {order}"
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [{c: _maybe_json(v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()]

    def upsert(self, table: str, id_: str, **fields: Any) -> None:
        assert table in {"sources", "candidates", "clips"}
        enc = {k: (json.dumps(v, default=str) if not isinstance(v, str | int | float | type(None)) else v) for k, v in fields.items()}
        cols = list(enc)
        with self.lock, self.conn:
            exists = self.conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone()  # noqa: S608
            if exists:
                sets = ", ".join(f"{c}=?" for c in cols)
                self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*enc.values(), id_))  # noqa: S608
            else:
                ph = ", ".join("?" for _ in cols)
                self.conn.execute(
                    f"INSERT INTO {table} (id, {', '.join(cols)}) VALUES (?, {ph})",  # noqa: S608
                    (id_, *enc.values()),
                )

    def set_if_status(self, table: str, id_: str, expected: set[str], **fields: Any) -> bool:
        """Update a row only if its status is still one of `expected` (checked and written in one
        write transaction). Returns False if someone else changed it first."""
        assert table == "clips"
        with self.lock:
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute("SELECT status FROM clips WHERE id=?", (id_,)).fetchone()
                if cur is None or cur[0] not in expected:
                    self.conn.rollback()
                    return False
                enc = {k: (json.dumps(v, default=str) if not isinstance(v, str | int | float | type(None)) else v) for k, v in fields.items()}
                sets = ", ".join(f"{c}=?" for c in enc)
                self.conn.execute(f"UPDATE clips SET {sets} WHERE id=?", (*enc.values(), id_))  # noqa: S608 (columns are code constants)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
        return True

    # ------------------------------------------------------------------ stage status for dashboard
    def stage(self, source_id: str, stage: str, status: str, log: str = "") -> None:
        with self.lock:
            self._stage(source_id, stage, status, log)

    def _stage(self, source_id: str, stage: str, status: str, log: str = "") -> None:
        row = self.row("sources", source_id) or {}
        stages = row.get("stages") or {}
        entry = stages.get(stage, {"log": []})
        entry["status"] = status
        entry["updated"] = now()
        if log:
            entry["log"] = (entry.get("log") or [])[-49:] + [f"{now()} {log}"]
        stages[stage] = entry
        self.upsert("sources", source_id, stages=stages)

    def clip_log(self, clip_id: str, msg: str) -> None:
        with self.lock:
            row = self.row("clips", clip_id) or {}
            logs = (row.get("logs") or [])[-99:] + [f"{now()} {msg}"]
            self.upsert("clips", clip_id, logs=logs)


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v
