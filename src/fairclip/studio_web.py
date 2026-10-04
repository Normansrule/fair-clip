"""JSON bridge between the browser Studio (Pyodide) and the `studio` core.

The browser does the I/O (files, ffmpeg.wasm, the zip); this module does every decision, with
the same code the app runs: rights record, clip finding, overlays, FFmpeg arguments, metadata and
the full compliance gate. Every function takes and returns JSON strings, so the JavaScript side
never needs to know a Python type, and nothing the page sends is trusted without validation.

State is one Studio session per page (module globals): the page is the only user.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from . import studio
from .captions import CaptionsError, parse_captions
from .media.analyze import rms_db_from_pcm
from .models import Candidate, RightsRecord, Transcript

MAX_DURATION_S = 3 * 3600


class _S:
    hasher: Any = None
    sha: str = ""
    rights: RightsRecord | None = None
    tr: Transcript | None = None
    rms: np.ndarray | None = None
    brand: studio.Brand | None = None
    cands: list[Candidate] = []
    done: dict[int, studio.Finished] = {}
    plans: dict[int, Any] = {}


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


def _ok(**kw: Any) -> str:
    return json.dumps({"ok": True, **kw})


# ------------------------------------------------------------------------------- step 1
def hash_start() -> None:
    _S.hasher = hashlib.sha256()


def _bytes(x: Any) -> bytes:
    """A JS Uint8Array arrives as a Pyodide JsProxy; CPython callers pass bytes."""
    return x.to_bytes() if hasattr(x, "to_bytes") else bytes(x)


def hash_update(chunk: Any) -> None:
    _S.hasher.update(_bytes(chunk))


def hash_done() -> str:
    _S.sha = _S.hasher.hexdigest()
    return _S.sha


def analyze(meta: str, captions: str, pcm: Any) -> str:
    """meta = {"brand": {...}, "rights": {...}, "duration": seconds}; pcm = mono s16le 16 kHz."""
    try:
        m = json.loads(meta)
        brand = studio.Brand.model_validate(m.get("brand") or {})
        choice = studio.RightsChoice.model_validate(m.get("rights") or {})
        duration = float(m.get("duration") or 0)  # 0 = the browser couldn't tell
    except (ValueError, TypeError) as exc:
        return _err(f"check the form: {str(exc)[:400]}")
    if duration > MAX_DURATION_S:
        return _err("the video is longer than 3 hours: trim it first, or use the app")
    if not _S.sha:
        return _err("internal: the video was not hashed")
    try:
        rec = studio.rights_record(choice, _S.sha)
    except ValueError as exc:
        return _err(f"rights: {exc}")
    settings = studio.studio_settings(brand)
    if problems := studio.rights_check(rec, settings.channel.monetized):
        return _err("rights: " + "; ".join(problems))
    if not captions.strip():
        return _err("add a captions file (.srt or .vtt): the browser version reads what was said from it")
    try:
        tr = parse_captions(captions)
    except CaptionsError as exc:
        return _err(f"captions: {exc}")
    if duration and tr.words and tr.words[-1].end > duration + 5:
        return _err("these captions run longer than the video: is it the right captions file?")
    raw = _bytes(pcm)
    rms = rms_db_from_pcm(np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2"))
    cands = studio.find_clips(f"src-{_S.sha[:12]}", tr, rms, [], brand)
    if not cands:
        return _err("no clip-sized moments found: the video may be too short, or the captions too sparse")
    _S.rights, _S.tr, _S.rms, _S.brand, _S.cands, _S.done, _S.plans = rec, tr, rms, brand, cands, {}, {}
    out = studio.candidates_json(cands, tr, brand)
    return _ok(candidates=out, credit=studio.rights_mod.attribution_line(rec, short=True),
               synthetic=rec.synthetic_or_altered)


# ------------------------------------------------------------------------------- step 2
def _take(i: int, take: dict[str, Any]) -> tuple[Candidate, studio.Take]:
    if _S.brand is None or not 0 <= i < len(_S.cands):
        raise ValueError("internal: no such moment (analyze first)")
    return _S.cands[i], studio.take_from_json(take)


def title_check(i: int, take: str, title: str) -> str:
    try:
        c, t = _take(i, json.loads(take))
        plan = studio.build_plan(c, t, _S.brand, i + 1)  # type: ignore[arg-type]
    except ValueError as exc:
        return _err(str(exc)[:400])
    if not title.strip():
        return _ok(problems=[])
    return _ok(problems=studio.title_problems(title, plan, _S.tr, _S.rms, _S.brand.channel))  # type: ignore[arg-type,union-attr]


# ------------------------------------------------------------------------------- step 3
def plan(i: int, take: str, src_name: str, quality: str = "fast") -> str:
    """The overlay text and exact FFmpeg arguments for one clip (placeholders @in/ @out/ @fonts/)."""
    try:
        c, t = _take(i, json.loads(take))
        p = studio.build_plan(c, t, _S.brand, i + 1)  # type: ignore[arg-type]
    except ValueError as exc:
        return _err(_friendly(exc))
    words, tmap, sents = studio.clip_window(p, _S.tr, _S.rms)  # type: ignore[arg-type]
    ass = studio.compose_overlay(p, words, tmap, sents, _S.rights, _S.brand.channel, _S.brand.topics)  # type: ignore[union-attr]
    _S.plans[i] = p
    args = studio.render_args(p, tmap, src_name=src_name, quality=quality if quality in ("fast", "high") else "fast")
    return _ok(ass=ass, args=args, duration=round(tmap.duration, 3))


def _friendly(exc: Exception) -> str:
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())[:500]
    return str(exc)[:500]


def finish(i: int, title: str, clip_sha256: str) -> str:
    p = _S.plans.get(i)
    if p is None or _S.brand is None:
        return _err("internal: plan the clip first")
    words, _tm, _s = studio.clip_window(p, _S.tr, _S.rms)  # type: ignore[arg-type]
    done = studio.finish(plan=p, n=i + 1, tr=_S.tr, rms=_S.rms, rights=_S.rights, brand=_S.brand,  # type: ignore[arg-type]
                         settings=studio.studio_settings(_S.brand), screen_terms=studio.default_screen_terms(),
                         clip_sha256=clip_sha256, fingerprint={"dhash": [], "shingles": studio.text_fingerprint(words)}, title=title)
    _S.done[i] = done
    return _ok(clip=done.to_json() | {"text": studio.clip_text(done)})


def package(order: str) -> str:
    """The copy-paste texts for the clips in `order` (indices, as made)."""
    items = [_S.done[i] for i in json.loads(order) if i in _S.done]
    return _ok(files=studio.upload_package(items, _S.brand))  # type: ignore[arg-type]
