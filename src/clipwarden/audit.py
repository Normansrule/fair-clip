"""Append-only, hash-chained audit log.

Each entry stores the SHA-256 of the previous entry, so editing or deleting any row breaks
every hash after it and `verify()` reports exactly where. SQLite triggers also refuse UPDATE
and DELETE on the table, so the application itself cannot rewrite history by accident.

This is tamper-*evident*, not tamper-*proof*: someone with write access to the database file
could rebuild the whole chain. Export the head hash (`clipwarden audit head`) to a place the
pipeline can't write (a printed note, a signed commit, an email to yourself) to anchor it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject TEXT NOT NULL,
  details TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


def _entry_hash(prev_hash: str, ts: str, actor: str, action: str, subject: str, details: str) -> str:
    payload = json.dumps(
        [prev_hash, ts, actor, action, subject, details], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    ts: str
    actor: str
    action: str
    subject: str
    details: dict[str, Any]
    prev_hash: str
    hash: str


class AuditLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SCHEMA)

    def head(self) -> str:
        row = self.conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def append(self, actor: str, action: str, subject: str, details: dict[str, Any] | None = None) -> AuditEntry:
        ts = datetime.now(UTC).isoformat(timespec="microseconds")
        det = json.dumps(details or {}, sort_keys=True, separators=(",", ":"), default=str)
        with self.conn:  # one transaction: read head + insert, so two writers can't fork the chain
            prev = self.head()
            h = _entry_hash(prev, ts, actor, action, subject, det)
            cur = self.conn.execute(
                "INSERT INTO audit (ts, actor, action, subject, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts, actor, action, subject, det, prev, h),
            )
        return AuditEntry(cur.lastrowid or 0, ts, actor, action, subject, json.loads(det), prev, h)

    def entries(self, limit: int = 500, subject: str | None = None) -> list[AuditEntry]:
        q = "SELECT seq, ts, actor, action, subject, details, prev_hash, hash FROM audit"
        args: tuple[Any, ...] = ()
        if subject:
            q += " WHERE subject = ?"
            args = (subject,)
        q += " ORDER BY seq DESC LIMIT ?"
        rows = self.conn.execute(q, (*args, limit)).fetchall()
        return [AuditEntry(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6], r[7]) for r in rows]

    def verify(self) -> tuple[bool, int | None]:
        """Recompute the chain. Returns (ok, first_bad_seq)."""
        prev = GENESIS
        for seq, ts, actor, action, subject, details, prev_hash, h in self.conn.execute(
            "SELECT seq, ts, actor, action, subject, details, prev_hash, hash FROM audit ORDER BY seq"
        ):
            if prev_hash != prev or _entry_hash(prev, ts, actor, action, subject, details) != h:
                return False, seq
            prev = h
        return True, None
