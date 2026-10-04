"""Milestone 4 look: presets, accent rules, hook fitting, cards, punch-in and the audio chain."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError
from test_core import W

from fairclip.media.edit import ENCODE, encode_args, filtergraph
from fairclip.media.overlay import build_ass
from fairclip.media.style import PRESETS, accent_problem, card_events, hook_events, rounded_rect
from fairclip.media.timeline import Interval, TimelineMap, keep_intervals
from fairclip.models import ClipPlan


def plan(**kw):
    return ClipPlan.model_validate({"clip_id": "c-001", "source_id": "s", "start": 0, "end": 20} | kw)


@pytest.mark.parametrize("colour,ok", [("#3DDC97", True), ("#2563EB", True), ("#FDE047", False), ("#FFE04A", False),
                                       ("#101010", False), ("red", False)])
def test_accent_rules(colour, ok):
    assert (accent_problem(colour) is None) is ok


def test_disclosure_yellow_accent_rejected_by_the_plan():
    with pytest.raises(ValidationError, match="disclosures"):
        plan(accent="#FDE047")


@pytest.mark.parametrize("style", sorted(PRESETS))
def test_every_preset_renders_captions_and_hook(style):
    words = [W("so", 0.5, 0.7), W("what", 0.8, 1.0), W("threatens", 1.1, 1.6), W("bridges", 1.7, 2.2)]
    ass = build_ass(plan(style=style), words, TimelineMap(keep_intervals(words, 0, 20, 5.0)),
                    hook_text="a quote", channel="Bridge Basics", attribution="")
    assert ("PCaption" in ass) is (style != "clean")
    assert "a quote" in ass and "Bridge Basics" in ass
    if style == "bold":
        assert "THREATENS" in ass and "\\fscx112" in ass  # upper case + pop


def test_hook_never_drops_words():
    text = "“So what actually threatens a bridge like this? Usually it is not weight, it is the wind.”"
    ev = hook_events(PRESETS["bold"], text, 2.0, True, 1080)
    shown = ev[-1].split("}", 1)[1].replace("\\N", " ")
    assert shown == text


def test_card_escapes_owner_text():
    ev = card_events(PRESETS["bold"], "MY TAKE", "{\\alpha&HFF&}hidden", 0, 5, True, "#3DDC97", 1080)
    assert "{\\alpha&HFF&}" not in "".join(ev)


def test_progress_bar_spans_the_clip():
    ass = build_ass(plan(), [], TimelineMap([Interval(0, 12.5)]), hook_text="", channel="", attribution="")
    assert "\\t(0,12500,\\fscx100)" in ass
    assert "\\t(0,12500" not in build_ass(plan(progress_bar=False), [], TimelineMap([Interval(0, 12.5)]), hook_text="", channel="", attribution="")


def test_rounded_rect_is_closed_and_in_bounds():
    d = rounded_rect(300, 100, 24)
    nums = [int(x) for x in re.findall(r"-?\d+", d)]
    assert d.startswith("m 24 0") and d.endswith("24 0") and min(nums) >= 0 and max(nums) <= 300


def test_punch_in_alternates_and_audio_is_polished():
    ivs = [Interval(0, 4), Interval(5, 9), Interval(10, 14)]
    g = filtergraph(ivs, 0.0, plan(reframe="crop"), 0.5, "o.ass")
    crops = re.findall(r"crop=ih\*9/16/([0-9.]+)", g)
    assert crops == ["1.0000", "1.0700", "1.0000"]
    assert "loudnorm=I=-14" in g and "acompressor" in g and "highpass" in g and "afade=t=out:st=11.880" in g
    assert "unsharp" in g and "subtitles=filename=@in/o.ass:fontsdir=@fonts/" in g
    flat = filtergraph(ivs, 0.0, plan(reframe="crop", punch_in=False), 0.5, "o.ass")
    assert set(re.findall(r"crop=ih\*9/16/([0-9.]+)", flat)) == {"1.0000"}


def test_fit_mode_punch_in_is_gentle_and_horizontal_works():
    ivs = [Interval(0, 4), Interval(5, 9)]
    assert "scale=1124:-2" in filtergraph(ivs, 0.0, plan(), 0.5, "o.ass")  # 1080 * 1.04 -> even
    h = filtergraph(ivs, 0.0, plan(aspect="16:9"), 0.5, "o.ass")
    assert "pad=1920:1080" in h and "crop=1920:1080" in h


def test_encode_presets():
    assert "-crf" in encode_args("high") and ENCODE["high"][ENCODE["high"].index("-crf") + 1] == "18"
    assert ENCODE["fast"][ENCODE["fast"].index("-preset") + 1] == "veryfast"
    assert "+faststart" in encode_args("fast")
