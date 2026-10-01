"""Every worked example in docs/EQUATIONS.md, recomputed. If the doc and the code disagree, this fails."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from clipwarden import analytics as an
from clipwarden.gate import union_len
from clipwarden.media.analyze import rms_db_from_pcm
from clipwarden.models import Sentence, Transcript, Word
from clipwarden.scoring import (
    FEATURES,
    MAX_STEP,
    MIN_CLIPS_FOR_UPDATE,
    W_MAX,
    W_MIN,
    normalise,
    propose_weights,
    relevance,
    score_value,
    step_toward,
    windows,
    zscores,
)

DEFAULT_W = {"energy": 0.25, "relevance": 0.35, "rate": 0.15, "scene": 0.10, "silence": 0.15}


# Equation 1 ---------------------------------------------------------------------------------
def test_eq1_worked_example():
    z = {"energy": 1.2, "relevance": 0.42, "rate": 0.5, "scene": 0.33, "silence": 0.10}
    assert score_value(z, DEFAULT_W) == pytest.approx(0.540, abs=1e-9)


def test_eq1a_zscore_example():
    z = zscores([-20, -22, -18, -24])
    assert np.mean([-20, -22, -18, -24]) == -21
    assert z[2] == pytest.approx(3 / math.sqrt(5), abs=1e-9)  # 1.342
    assert zscores([5.0, 5.0]) == [0.0, 0.0]  # no spread -> no signal


def test_eq1b_relevance_example():
    text = "bridge bridge wind deck flutter"
    assert relevance(text, ["bridge", "wind", "cable"]) == pytest.approx(3 / (math.sqrt(7) * math.sqrt(3)), abs=1e-9)  # 0.6547


def test_eq1c_scene_density_example():
    cuts_per_min = 2 / (30 / 60)
    assert min(1.0, cuts_per_min / 6.0) == pytest.approx(0.6667, abs=1e-4)


def test_eq1d_rms_dbfs():
    sr = 16000
    t = np.arange(sr) / sr
    sine = (0.5 * 32768 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    # RMS of a sine with peak 0.5 is 0.5/√2 -> 20·log10(0.3536) = -9.03 dBFS
    assert float(np.mean(rms_db_from_pcm(sine))) == pytest.approx(-9.03, abs=0.05)


def test_windows_never_cut_mid_sentence():
    ss = [Sentence(start=i * 10.0, end=i * 10.0 + 9.0, text=f"s{i}", words=[Word(text=f"s{i}", start=i * 10.0, end=i * 10.0 + 9.0)]) for i in range(6)]
    tr = Transcript(engine="t", sentences=ss)
    for w in windows(tr, 20, 40):
        assert any(abs(w.start - s.start) < 1e-9 for s in ss) and any(abs(w.end - s.end) < 1e-9 for s in ss)
        assert 20 <= w.end - w.start <= 40


# Equation 4: bounded feedback ----------------------------------------------------------------
def test_eq4_worked_example():
    target = {"energy": 0.45, "relevance": 0.25, "rate": 0.10, "scene": 0.10, "silence": 0.10}
    new = step_toward(DEFAULT_W, target)
    assert new == pytest.approx({"energy": 0.30, "relevance": 0.325, "rate": 0.1375, "scene": 0.10, "silence": 0.1375})
    assert sum(new.values()) == pytest.approx(1.0)


def test_normalise_box_and_sum():
    rng = random.Random(7)
    for _ in range(300):
        w = normalise({k: rng.uniform(-1, 5) for k in FEATURES})
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-5)
        assert all(W_MIN - 1e-9 <= v <= W_MAX + 1e-9 for v in w.values())


def test_feedback_cannot_run_away():
    """Even with adversarial 'analytics', each approved update moves weights by <= MAX_STEP."""
    rng = random.Random(3)
    w = dict(DEFAULT_W)
    for _ in range(50):
        X = [{k: rng.uniform(-3, 3) for k in FEATURES} for _ in range(20)]
        y = [x["energy"] * 100 + rng.uniform(-1, 1) for x in X]  # analytics screaming "only loudness matters"
        new, _ = propose_weights(w, X, y)
        assert max(abs(new[k] - w[k]) for k in FEATURES) <= MAX_STEP + 1e-6
        assert all(W_MIN - 1e-6 <= v <= W_MAX + 1e-6 for v in new.values())
        w = new
    assert w["energy"] <= W_MAX + 1e-6  # capped, never 100 %
    with pytest.raises(ValueError):
        propose_weights(DEFAULT_W, X[: MIN_CLIPS_FOR_UPDATE - 1], y[: MIN_CLIPS_FOR_UPDATE - 1])


# Equations 2-3: metrics ------------------------------------------------------------------------
def test_eq2_metrics_worked_example():
    assert an.ctr(420, 12000) == pytest.approx(0.035)
    assert an.avd(41250, 1500) == pytest.approx(27.5)
    assert an.apv(27.5, 45) == pytest.approx(0.6111, abs=1e-4)
    assert an.conversion_rate(12, 300) == pytest.approx(0.04)
    assert an.epc(18.60, 300) == pytest.approx(0.062)
    assert an.ctr(0, 0) == 0.0  # no division by zero


def test_eq3_retention_area_worked_example():
    assert an.area_under_retention([0, 0.5, 1.0], [1.0, 0.6, 0.4]) == pytest.approx(0.65)
    assert an.biggest_drop([0, 0.5, 1.0], [1.0, 0.6, 0.4]) == (0.5, pytest.approx(0.4))


def test_eq5_value_add_coverage_worked_example():
    assert union_len([(2.2, 10.0), (12.0, 21.0)]) / 23.8 == pytest.approx(0.706, abs=1e-3)
    assert union_len([(0, 5), (3, 8)]) == 8  # overlaps counted once


def test_parse_analytics_report():
    resp = {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}], "rows": [[0.01, 1.0], [0.5, 0.6]]}
    assert an.parse_report(resp) == {"elapsedVideoTimeRatio": [0.01, 0.5], "audienceWatchRatio": [1.0, 0.6]}
