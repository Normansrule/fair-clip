"""Studio core (milestone 4): captions import, rights choice, clip finding, plans, finishing, export."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from fairclip import studio
from fairclip.captions import CaptionsError, parse_captions
from fairclip.media.analyze import rms_db_from_pcm
from fairclip.models import RightsBasis, TemplateKind, Transcript

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo"
FONTS = ROOT / "src" / "fairclip" / "data" / "fonts"


def to_srt(tr: Transcript) -> str:
    def ts(t):
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"
    return "\n".join(f"{i}\n{ts(s.start)} --> {ts(s.end)}\n{s.text}\n" for i, s in enumerate(tr.sentences, 1))


# ------------------------------------------------------------------------------------ captions
def test_srt_round_trip_keeps_sentences_and_times():
    tr = Transcript.model_validate(json.loads((DEMO / "transcript.json").read_text()))
    got = parse_captions(to_srt(tr))
    # Same words in the same order; sentence starts that survive keep their time to the millisecond.
    assert " ".join(s.text for s in got.sentences) == " ".join(s.text for s in tr.sentences)
    starts = {round(s.start, 2) for s in got.sentences}
    assert all(round(s.start, 2) in starts for s in tr.sentences) and got.engine == "captions:srt"


def test_youtube_vtt_inline_word_times_and_rolling_duplicates():
    vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n00:00:00.160 --> 00:00:02.710 align:start position:0%\n \n"
           "so<00:00:00.480><c> what</c><00:00:00.640><c> actually</c>\n\n"
           "00:00:02.710 --> 00:00:02.720 align:start position:0%\nso what actually\n \n\n"
           "00:00:02.720 --> 00:00:05.430 align:start position:0%\nso what actually\nthreatens<00:00:03.360><c> bridges</c>\n")
    words = parse_captions(vtt).words
    assert [w.text for w in words] == ["so", "what", "actually", "threatens", "bridges"]
    assert (words[1].start, words[4].start) == (0.48, 3.36)


def test_unpunctuated_captions_split_at_pauses():
    srt = "1\n00:00:00,000 --> 00:00:02,000\nhello there friends\n\n2\n00:00:03,500 --> 00:00:05,000\nnew thought here\n"
    assert [s.text for s in parse_captions(srt).sentences] == ["hello there friends", "new thought here"]


@pytest.mark.parametrize("bad", ["", "just some text", "1\n00:00:05,000 --> 00:00:01,000\nbackwards\n"])
def test_bad_captions_are_refused(bad):
    with pytest.raises(CaptionsError):
        parse_captions(bad)


def test_caption_tags_and_hidden_characters_are_stripped():
    srt = "1\n00:00:00,000 --> 00:00:02,000\n<b>bold</b> te​xt &amp; more\n"
    assert [w.text for w in parse_captions(srt).words] == ["bold", "text", "&", "more"]


# ------------------------------------------------------------------------------------ rights + plan
def test_rights_choice_becomes_a_checked_record():
    rec = studio.rights_record(studio.RightsChoice(basis=RightsBasis.OWN_CONTENT, title="My talk", holder="Me"), "a" * 64)
    assert rec.source_sha256 == "a" * 64 and studio.rights_check(rec) == []
    nc = studio.rights_record(studio.RightsChoice(basis=RightsBasis.CREATIVE_COMMONS, title="T", holder="H",
                                                  cc_license="CC-BY-NC-4.0", attribution="T by H"), "b" * 64)
    assert any("non-commercial" in p for p in studio.rights_check(nc))
    with pytest.raises(ValueError):  # a permission needs evidence
        studio.rights_record(studio.RightsChoice(basis=RightsBasis.WRITTEN_PERMISSION, title="T", holder="H"), "c" * 64)


@pytest.fixture(scope="module")
def sample():
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    tr = parse_captions(to_srt(Transcript.model_validate(json.loads((DEMO / "transcript.json").read_text()))))
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(DEMO / "source.mp4"), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    rms = rms_db_from_pcm(np.frombuffer(pcm, dtype="<i2"))
    sha = __import__("hashlib").sha256((DEMO / "source.mp4").read_bytes()).hexdigest()
    rights = studio.rights_record(studio.RightsChoice(basis=RightsBasis.CREATIVE_COMMONS, title="Sample talk", holder="fair-clip contributors",
                                                      cc_license="CC0-1.0", synthetic=True), sha)
    brand = studio.Brand(channel="Bridge Basics", topics=["bridge", "cable", "wind", "tower", "deck"], clips=3)
    return tr, rms, rights, brand


def test_find_clips_and_suggestions(sample):
    tr, rms, _rights, brand = sample
    cands = studio.find_clips("src-x", tr, rms, [], brand)
    assert len(cands) == 3 and cands[0].score >= cands[-1].score
    for c in cands:
        sug = studio.suggest_take(c)
        assert sug["prompt"] and sug["template"] in {t.value for t in TemplateKind}
        if sug["template"] == "key_number":
            assert sug["data"]["headline"] in c.text  # pre-filled facts come from what's said


def test_empty_take_is_blocked_by_the_gate(sample):
    tr, rms, rights, brand = sample
    c = studio.find_clips("src-x", tr, rms, [], brand)[0]
    plan = studio.build_plan(c, studio.Take(text=""), brand, 1)
    done = studio.finish(plan=plan, n=1, tr=tr, rms=rms, rights=rights, brand=brand, settings=studio.studio_settings(brand),
                         screen_terms=studio.default_screen_terms(), clip_sha256="0" * 64)
    assert not done.passed and "VALUE_ADD_MISSING" in {r.code for r in done.report.blocks}


def test_end_to_end_render_finish_and_package(sample, tmp_path):
    """The exact Studio flow in Python: plan -> real FFmpeg render -> gate -> upload package."""
    tr, rms, rights, brand = sample
    c = studio.find_clips("src-x", tr, rms, [], brand)[0]
    take = studio.Take(text="Flutter is a feedback loop: each twist changes how the air pushes on the deck, which feeds the next twist.")
    plan = studio.build_plan(c, take, brand, 1)
    words, tmap, sents = studio.clip_window(plan, tr, rms)
    ass = studio.compose_overlay(plan, words, tmap, sents, rights, brand.channel, brand.topics)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "overlay.ass").write_text(ass)
    shutil.copy(DEMO / "source.mp4", tmp_path / "in" / "source.mp4")
    args = studio.render_args(plan, tmap, src_name="source.mp4", quality="fast")
    args = [a.replace("@in/", f"{tmp_path}/in/").replace("@out/", f"{tmp_path}/out/").replace("@fonts/", f"{FONTS}/") for a in args]
    subprocess.run(["ffmpeg", "-loglevel", "error", *args], check=True, timeout=600)
    clip = tmp_path / "out" / "clip.mp4"
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(clip)],
                                      capture_output=True, text=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920) and abs(float(probe["format"]["duration"]) - tmap.duration) < 0.5
    sha = __import__("hashlib").sha256(clip.read_bytes()).hexdigest()
    done = studio.finish(plan=plan, n=1, tr=tr, rms=rms, rights=rights, brand=brand, settings=studio.studio_settings(brand),
                         screen_terms=studio.default_screen_terms(), clip_sha256=sha)
    assert done.passed, [(r.code, r.message) for r in done.report.blocks]
    assert done.filename.startswith("01-") and done.filename.endswith(".mp4")
    files = studio.upload_package([done], brand)
    txt = files[done.filename.removesuffix(".mp4") + ".txt"]
    assert done.metadata.title in txt and "Altered or synthetic content: YES" in txt  # synthetic narrator
    assert "CC0-1.0" in txt  # credit travels with the description
    rows = list(csv.DictReader(io.StringIO(files["upload-sheet.csv"])))
    assert rows[0]["file"] == done.filename and rows[0]["altered_or_synthetic"] == "yes"
    assert "README.txt" in files and "ALL-TITLES-AND-DESCRIPTIONS.txt" in files
    assert json.dumps(done.to_json())  # serialisable for the browser


def test_blocked_clips_are_listed_not_exported(sample):
    tr, rms, rights, brand = sample
    c = studio.find_clips("src-x", tr, rms, [], brand)[0]
    bad = studio.finish(plan=studio.build_plan(c, studio.Take(text=""), brand, 2), n=2, tr=tr, rms=rms, rights=rights, brand=brand,
                        settings=studio.studio_settings(brand), screen_terms=studio.default_screen_terms(), clip_sha256="0" * 64)
    files = studio.upload_package([bad], brand)
    assert "VALUE_ADD_MISSING" in files["README.txt"] and not any(k.startswith("02-") for k in files)


def test_brand_accent_cannot_mimic_disclosures(sample):
    tr, rms, _r, brand = sample
    c = studio.find_clips("src-x", tr, rms, [], brand)[0]
    with pytest.raises(ValueError):
        studio.build_plan(c, studio.Take(text="x" * 20), brand.model_copy(update={"accent": "#FDE047"}), 1)


def test_owner_title_and_hashtags(sample):
    tr, rms, rights, brand = sample
    c = studio.find_clips("src-x", tr, rms, [], brand)[0]
    take = studio.Take(text="Wind, not weight, is the real enemy: stiff, deep decks are how engineers stopped flutter.")
    plan = studio.build_plan(c, take, brand, 1)
    kw = dict(plan=plan, n=1, tr=tr, rms=rms, rights=rights, brand=brand, settings=studio.studio_settings(brand),
              screen_terms=studio.default_screen_terms(), clip_sha256="1" * 64)
    done = studio.finish(**kw, title="Why wind, not weight, breaks bridges")
    assert done.metadata.title == "Why wind, not weight, breaks bridges" and done.filename.startswith("01-why-wind")
    assert done.metadata.description.rstrip().endswith("#Shorts #Bridge #Wind")  # topics this clip talks about come first
    assert done.metadata.description.index(take.text) < done.metadata.description.index("In this clip:")  # owner's words lead
    bad = studio.finish(**kw, title="<script> x")
    assert not bad.passed and "METADATA_LIMITS" in {r.code for r in bad.report.blocks}


def test_hashtags_are_letters_and_digits_only():
    b = studio.Brand(channel="C", topics=["suspension bridges", "#wind!!", "x-y z"])
    assert studio.hashtags(b, True) == "#Shorts #SuspensionBridges #Wind"
    assert studio.hashtags(b, False) == "#SuspensionBridges #Wind #XYZ"


def test_suggested_figures_drop_trailing_punctuation_and_years():
    from fairclip.models import Candidate, Features

    f = Features(audio_energy_db=-20, speech_rate_wps=2.5, relevance=0.5, scene_density=0.1, silence_ratio=0.1)
    c = Candidate(candidate_id="x", source_id="s", start=0, end=30, text="In June 2000, they fitted 37, then more.", features=f, score=0.5)
    assert studio.suggest_take(c)["data"] == {"headline": "37"}


def test_title_feedback_matches_the_gate(sample):
    tr, rms, rights, brand = sample
    c = studio.find_clips("src-x", tr, rms, [], brand)[0]
    plan = studio.build_plan(c, studio.Take(text="Stiff, deep decks are how engineers stopped flutter."), brand, 1)
    words = {w.text.strip(".,?!").lower() for w in studio.clip_window(plan, tr, rms)[0]}
    assert "wind" in words
    assert studio.title_problems("Wind, not weight: how engineers stopped flutter", plan, tr, rms, brand.channel) == []
    probs = studio.title_problems("Aliens destroyed 9 bridges", plan, tr, rms, brand.channel)
    assert any("aliens" in p for p in probs) and any("9" in p for p in probs)


def test_grounding_tolerates_simple_inflections():
    from fairclip.gate import ungrounded_words

    assert ungrounded_words("Bridges swaying with walkers", {"bridge", "sways", "walkers"}) == []
    assert ungrounded_words("Bridges exploded", {"bridge"}) == ["exploded"]
