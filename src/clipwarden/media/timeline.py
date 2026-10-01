"""Jump-cut planning and the source→clip timeline map.

Jump cuts remove long pauses *between* words (never inside one). Everything drawn on the clip
(captions, cards, disclosures) is positioned on the clip's own timeline, so we need an exact
map from source time to clip time after the pauses are removed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..models import Word

PAD = 0.12  # seconds of breathing room kept on each side of a cut


@dataclass(frozen=True)
class Interval:
    a: float  # source time
    b: float

    @property
    def dur(self) -> float:
        return self.b - self.a


def keep_intervals(words: list[Word], start: float, end: float, max_gap: float,
                   rms_db: np.ndarray | None = None, win: float = 0.05, silence_db: float = -40.0) -> list[Interval]:
    """Split [start, end] into the parts to keep, dropping pauses longer than max_gap.

    A gap is only cut when the audio in it is actually quiet (≥ 80 % of windows below
    silence_db), so music or laughter between words survives.
    """
    ws = [w for w in words if w.start >= start - 1e-6 and w.end <= end + 1e-6]
    cuts: list[tuple[float, float]] = []
    prev_end = start
    for w in ws + [Word(text="END", start=end, end=end)]:
        gap = w.start - prev_end
        if gap > max_gap:
            a, b = prev_end + PAD, w.start - PAD
            quiet = True
            if rms_db is not None and b > a:
                seg = rms_db[int(a / win) : max(int(b / win), int(a / win) + 1)]
                quiet = seg.size == 0 or float(np.mean(seg < silence_db)) >= 0.8
            if quiet and b - a > 0.05:
                cuts.append((a, b))
        prev_end = max(prev_end, w.end)
    out, cur = [], start
    for a, b in cuts:
        if a > cur:
            out.append(Interval(cur, a))
        cur = b
    if end > cur:
        out.append(Interval(cur, end))
    return [iv for iv in out if iv.dur > 0.04]


class TimelineMap:
    def __init__(self, intervals: list[Interval]) -> None:
        self.iv = intervals
        self.offsets = np.cumsum([0.0] + [i.dur for i in intervals])

    @property
    def duration(self) -> float:
        return float(self.offsets[-1])

    def to_clip(self, t: float) -> float | None:
        """Clip time for source time t, or None if t falls inside a removed pause."""
        for k, i in enumerate(self.iv):
            if i.a - 1e-6 <= t <= i.b + 1e-6:
                return float(self.offsets[k] + min(max(t - i.a, 0.0), i.dur))
        return None

    def clamp_to_clip(self, t: float) -> float:
        """Like to_clip, but a time inside a removed pause snaps to the next kept moment."""
        c = self.to_clip(t)
        if c is not None:
            return c
        for k, i in enumerate(self.iv):
            if t < i.a:
                return float(self.offsets[k])
        return self.duration

    def removed_seconds(self, start: float, end: float) -> float:
        return (end - start) - self.duration
