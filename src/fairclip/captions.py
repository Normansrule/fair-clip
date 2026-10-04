"""Captions in, word-timed transcript out: SRT, WebVTT (including YouTube's auto-captions with
per-word timestamps) and fair-clip's own transcript JSON.

The browser build has no speech-to-text, so a captions file is how it learns what was said and
when. Most creators already have one: YouTube Studio → Subtitles → ⋮ → Download (.srt or .vtt).

Word timings
* If the cue carries inline timestamps (``word<00:00:01.120><c> next</c>``), they are used.
* Otherwise each cue's time is shared across its words in proportion to their length.

Sentences come from punctuation; captions without punctuation (typical auto-captions) are split
at pauses longer than 0.6 s, or every 18 words. Caption text is untrusted data like any
transcript: tags are stripped, control characters removed, and sizes capped.
"""

from __future__ import annotations

import html
import json
import re

from .models import Sentence, Transcript, Word

MAX_BYTES = 10 * 1024 * 1024
_TIME = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_INLINE = re.compile(r"<(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})>")
_TAG = re.compile(r"</?[^>]{1,40}>")
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")
_END = re.compile(r"[.!?…]['\"”’)]*$")


class CaptionsError(ValueError):
    pass


def _secs(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _clean(text: str) -> str:
    text = html.unescape(_TAG.sub("", text))
    return _CTRL.sub("", text).replace(" ", " ").strip()


def _cues(text: str) -> list[tuple[float, float, str]]:
    """[(start, end, raw text with inline tags)] in file order."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, i = [], 0
    while i < len(lines):
        m = _CUE.match(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        a, b = _secs(*g[0:4]), _secs(*g[4:8])
        i += 1
        body = []
        # A cue ends at a truly empty line; YouTube puts lines holding a single space INSIDE cues.
        while i < len(lines) and lines[i] != "" and not _CUE.match(lines[i]):
            if lines[i].strip():
                body.append(lines[i])
            i += 1
        if b > a and body:
            out.append((a, b, "\n".join(body)))
    return out


def _words_from_cue(a: float, b: float, raw: str) -> list[Word]:
    """Inline-timed words when present, else proportional timing across the cue."""
    if _INLINE.search(raw):
        # YouTube style: "first<00:00:01.120><c> second</c><00:00:01.400><c> third</c>"
        pieces = _INLINE.split(raw)
        words: list[tuple[float, str]] = []
        t = a
        k = 0
        while k < len(pieces):
            chunk = pieces[k]
            for w in _clean(chunk).split():
                words.append((t, w))
            if k + 4 < len(pieces):
                t = _secs(pieces[k + 1].rstrip(":") if pieces[k + 1] else None, pieces[k + 2], pieces[k + 3], pieces[k + 4])
            k += 5
        out = []
        for j, (ws, w) in enumerate(words):
            we = words[j + 1][0] if j + 1 < len(words) else b
            out.append(Word(text=w[:64], start=round(ws, 3), end=round(max(we, ws + 0.05), 3)))
        return out
    tokens = _clean(raw).split()
    if not tokens:
        return []
    total = sum(len(w) + 1 for w in tokens)
    out, t = [], a
    for w in tokens:
        d = (b - a) * (len(w) + 1) / total
        out.append(Word(text=w[:64], start=round(t, 3), end=round(t + d * 0.92, 3)))
        t += d
    return out


def _dedupe_rolling(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """YouTube auto-captions repeat the previous line at the top of the next cue; keep new text only."""
    out, prev_last = [], ""
    for a, b, raw in cues:
        lines = [ln for ln in raw.split("\n") if _clean(ln)]
        if lines and prev_last and _clean(lines[0]) == prev_last and not _INLINE.search(lines[0]):
            lines = lines[1:]
        if lines:
            out.append((a, b, "\n".join(lines)))
            prev_last = _clean(lines[-1])
    return out


def _sentences(words: list[Word]) -> list[Sentence]:
    out: list[Sentence] = []
    cur: list[Word] = []
    punctuated = sum(1 for w in words if _END.search(w.text)) >= max(1, len(words) // 40)

    def flush() -> None:
        if cur:
            out.append(Sentence(start=cur[0].start, end=cur[-1].end, text=" ".join(w.text for w in cur)[:2000], words=list(cur)))
            cur.clear()

    for i, w in enumerate(words):
        if cur and not punctuated and (w.start - cur[-1].end > 0.6 or len(cur) >= 18):
            flush()
        cur.append(w)
        if punctuated and _END.search(w.text) or len(cur) >= 60:
            flush()
        if i == len(words) - 1:
            flush()
    return out


def parse_captions(text: str, language: str = "en") -> Transcript:
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise CaptionsError("captions file larger than 10 MB")
    stripped = text.lstrip("﻿").lstrip()
    if stripped.startswith("{"):
        return Transcript.model_validate(json.loads(stripped))
    cues = _cues(stripped)
    if not cues:
        raise CaptionsError("no caption cues found: expected an .srt or .vtt file with lines like 00:00:01,000 --> 00:00:04,000")
    cues = _dedupe_rolling(cues)
    words: list[Word] = []
    for a, b, raw in cues:
        for w in _words_from_cue(a, b, raw):
            if words and w.start < words[-1].start:  # overlapping cues: keep time moving forward
                continue
            words.append(w)
    if not words:
        raise CaptionsError("the captions file has timings but no words")
    engine = "captions:vtt" if stripped.startswith("WEBVTT") else "captions:srt"
    return Transcript(language=language, engine=engine, sentences=_sentences(words))
