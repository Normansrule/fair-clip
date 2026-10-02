r"""Stage 3: highlight scoring (see docs/EQUATIONS.md, Equation 1).

    S = w_E·z(E) + w_R·R + w_r·z(r) + w_D·D − w_Q·Q

E  audio energy, mean RMS level over the segment (dBFS)
R  transcript relevance, cosine similarity to the channel's topics (0..1)
r  speech rate (words per second)
D  scene-change density, cuts per minute divided by D_REF and capped at 1 (0..1)
Q  silence ratio, share of 50 ms windows quieter than SILENCE_DB (0..1)
z(x) = (x − mean) / std across this source's candidates, clipped to ±3

Weights live in [W_MIN, W_MAX], sum to 1, and can only move by at most MAX_STEP per approved
update, so the analytics feedback loop can't run away (Equation 4).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

from .media.analyze import AudioSignal
from .models import Candidate, Features, Transcript

FEATURES = ("energy", "relevance", "rate", "scene", "silence")
SIGN = {"energy": 1.0, "relevance": 1.0, "rate": 1.0, "scene": 1.0, "silence": -1.0}
W_MIN, W_MAX, MAX_STEP = 0.02, 0.60, 0.05
D_REF = 6.0  # cuts per minute that counts as "maximally dynamic"
SILENCE_DB = -40.0
MIN_CLIPS_FOR_UPDATE = 10

STOP = set(
    """a an and are as at be been but by can could did do does for from had has have he her his how
    i if in into is it its just like me more most my no not of on or our out so some than that the
    their them then there these they this those to too up us very was we were what when where which
    who why will with would you your yes okay um uh really going get got thing things one two also
    about because here now well let lets it's that's we're you're i'm don't can't""".split()
)
_TOKEN = re.compile(r"[a-z][a-z'-]+")


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP and len(t) > 2]


def relevance(text: str, topics: list[str]) -> float:
    """Cosine similarity between the segment's word counts and a 0/1 topic-word vector."""
    c = Counter(tokens(text))
    topic_words = {t for phrase in topics for t in tokens(phrase)}
    if not c or not topic_words:
        return 0.0
    dot = sum(c[t] for t in topic_words)
    norm = math.sqrt(sum(v * v for v in c.values())) * math.sqrt(len(topic_words))
    return float(min(1.0, dot / norm))


def default_topics(tr: Transcript, k: int = 10) -> list[str]:
    return [w for w, _ in Counter(tokens(" ".join(s.text for s in tr.sentences))).most_common(k)]


def zscores(values: list[float]) -> list[float]:
    a = np.asarray(values, dtype=float)
    sd = a.std()
    if len(a) < 2 or sd < 1e-9:
        return [0.0] * len(a)
    return list(np.clip((a - a.mean()) / sd, -3, 3))


def normalise(w: dict[str, float]) -> dict[str, float]:
    """Project weights into the box [W_MIN, W_MAX] with sum 1 (a few clip-and-rescale rounds)."""
    raw = [w.get(k, 0.0) for k in FEATURES]
    if not all(math.isfinite(x) for x in raw):
        raise ValueError("weights must be finite numbers")
    v = np.array([max(0.0, x) for x in raw])
    if v.sum() == 0:
        v = np.ones(len(FEATURES))
    v = v / v.sum()
    for _ in range(50):
        v = np.clip(v, W_MIN, W_MAX)
        free = (v > W_MIN + 1e-12) & (v < W_MAX - 1e-12)
        excess = v.sum() - 1.0
        if abs(excess) < 1e-12:
            break
        if free.any():
            v[free] -= excess * v[free] / v[free].sum()
        else:
            v = v / v.sum()
    return {k: float(round(x, 6)) for k, x in zip(FEATURES, v, strict=True)}


def scene_density(n_cuts: int, duration_s: float) -> float:
    """D = min(1, cuts per minute / D_REF)."""
    return min(1.0, (n_cuts / (max(duration_s, 1e-6) / 60.0)) / D_REF)


def score_value(z: dict[str, float], w: dict[str, float]) -> float:
    return sum(SIGN[k] * w[k] * z[k] for k in FEATURES)


@dataclass
class Window:
    start: float
    end: float
    text: str
    n_words: int


def windows(tr: Transcript, min_s: float, max_s: float) -> list[Window]:
    """Every run of whole sentences lasting min_s..max_s (clips never cut mid-sentence)."""
    out: list[Window] = []
    ss = tr.sentences
    for i in range(len(ss)):
        for j in range(i, len(ss)):
            dur = ss[j].end - ss[i].start
            if dur > max_s:
                break
            if dur >= min_s:
                seg = ss[i : j + 1]
                out.append(Window(ss[i].start, ss[j].end, " ".join(s.text for s in seg), sum(len(s.words) for s in seg)))
    return out


def features_for(w: Window, audio: AudioSignal, cuts: list[float], topics: list[str]) -> Features:
    lv = audio.slice(w.start, w.end)
    dur = max(w.end - w.start, 1e-6)
    ncuts = sum(1 for t in cuts if w.start < t < w.end)
    return Features(
        audio_energy_db=float(np.mean(lv)) if lv.size else -120.0,
        speech_rate_wps=w.n_words / dur,
        relevance=relevance(w.text, topics),
        scene_density=scene_density(ncuts, dur),
        silence_ratio=float(np.mean(lv < SILENCE_DB)) if lv.size else 1.0,
    )


def overlap(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.end - a.start, b.end - b.start))


def rank_candidates(source_id: str, tr: Transcript, audio: AudioSignal, cuts: list[float],
                    topics: list[str], weights: dict[str, float], *, min_s: float, max_s: float,
                    k: int, max_overlap: float = 0.3) -> list[Candidate]:
    w = normalise(weights)
    wins = windows(tr, min_s, max_s)
    if not wins:
        return []
    feats = [features_for(x, audio, cuts, topics) for x in wins]
    zE = zscores([f.audio_energy_db for f in feats])
    zr = zscores([f.speech_rate_wps for f in feats])
    cands = []
    for i, (x, f) in enumerate(zip(wins, feats, strict=True)):
        z = {"energy": zE[i], "relevance": f.relevance, "rate": zr[i], "scene": f.scene_density, "silence": f.silence_ratio}
        cands.append(Candidate(candidate_id=f"{source_id}-c{i:04d}", source_id=source_id, start=x.start,
                               end=x.end, text=x.text[:5000], features=f, z=z, score=score_value(z, w)))
    cands.sort(key=lambda c: c.score, reverse=True)
    picked: list[Candidate] = []
    for c in cands:  # non-maximum suppression: no two picks overlap by more than max_overlap
        if all(overlap(c, p) <= max_overlap for p in picked):
            picked.append(c)
        if len(picked) == k:
            break
    for r, c in enumerate(picked, 1):
        c.rank = r
    return picked


# ----------------------------------------------------------------------------- feedback loop


def propose_weights(current: dict[str, float], X: list[dict[str, float]], y: list[float],
                    lam: float = 1.0) -> tuple[dict[str, float], dict[str, float]]:
    """Bounded update from retention data (Equation 4). Returns (proposed, raw_fit).

    Ridge regression pulled toward the current weights:
        w_fit = (AᵀA + λI)⁻¹ (Aᵀ z(y) + λ w_cur),  A_jk = sign_k · x_jk
    w_fit is projected into [W_MIN, W_MAX] with sum 1, then the weights step toward it along a
    straight line, shortened so that no single weight moves more than MAX_STEP. The caller must
    get human approval before using the proposal.
    """
    if len(y) < MIN_CLIPS_FOR_UPDATE:
        raise ValueError(f"need at least {MIN_CLIPS_FOR_UPDATE} published clips, have {len(y)}")
    cur = normalise(current)
    A = np.array([[SIGN[k] * row[k] for k in FEATURES] for row in X], dtype=float)
    if not (np.isfinite(A).all() and np.isfinite(np.asarray(y, dtype=float)).all()):
        raise ValueError("analytics rows contain NaN or infinite values")
    zy = np.asarray(zscores(list(y)), dtype=float)
    w0 = np.array([cur[k] for k in FEATURES])
    fit = np.linalg.solve(A.T @ A + lam * np.eye(len(FEATURES)), A.T @ zy + lam * w0)
    raw = {k: float(v) for k, v in zip(FEATURES, fit, strict=True)}
    fit_n = normalise({k: max(0.0, v) for k, v in raw.items()})
    return step_toward(cur, fit_n), raw


def step_toward(cur: dict[str, float], target: dict[str, float]) -> dict[str, float]:
    """Move along the straight line cur -> target, shortened so no weight moves > MAX_STEP.

    t = min(1, MAX_STEP / max_k |target_k - cur_k|);  w_new = cur + t (target - cur)
    Both ends lie in the (convex) feasible box with sum 1, so every point between does too.
    """
    gap = max(abs(target[k] - cur[k]) for k in FEATURES)
    t = 1.0 if gap <= MAX_STEP else MAX_STEP / gap
    return {k: float(round(cur[k] + t * (target[k] - cur[k]), 6)) for k in FEATURES}
