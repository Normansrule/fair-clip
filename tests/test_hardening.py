"""Regression tests for the milestone-5 hardening review: each test pins one finding so it can't regress."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import SHA, TRANSCRIPT, base_plan, make_ctx, own_rights
from pydantic import ValidationError
from test_publish import add_clip, ok

from clipwarden.approval import ApprovalError, decide
from clipwarden.gate import numbers, run_gate
from clipwarden.links import find_links
from clipwarden.models import Decision, Metadata, ValueAddElement
from clipwarden.publish.publisher import publish_approved
from clipwarden.redact import redact
from clipwarden.store import Store


# ------------------------------------------------------------------------- links: obfuscation
@pytest.mark.parametrize("text", [
    "go to evil dot com slash free",
    "evil[.]com/free",
    "evil(dot)com",
    "visit evil.ai/free now",  # any TLD, not just a fixed list
    "evil​.com",  # zero-width space inside the domain
])
def test_obfuscated_links_are_found(text):
    assert find_links(text), text


@pytest.mark.parametrize("text", ["built with Node.js", "see clip.mp4", "e.g. this", "i.e. that", "version 1.2.3"])
def test_ordinary_words_are_not_links(text):
    assert find_links(text) == [], text


def test_spoken_obfuscated_link_in_card_is_blocked():
    va = [{"kind": "context_card", "text": "Flutter feeds itself. Grab the free kit at evil dot com slash kit today.", "start": 1.0, "end": 12.0}]
    rep = run_gate(make_ctx(plan=base_plan(value_add=va)), SHA)
    assert "LINK_NOT_ALLOWLISTED" in {r.code for r in rep.blocks}


# --------------------------------------------------------------------- hidden characters
def test_right_to_left_override_rejected_in_value_add():
    with pytest.raises(ValidationError):
        ValueAddElement(kind="context_card", text="Read more at ‮moc.live", start=0, end=5)


def test_zero_width_rejected_in_tags():
    with pytest.raises(ValidationError):
        Metadata(title="t", description="d", tags=["sc​am"])


# ------------------------------------------------------------------- paid promotion in video
def test_paid_promotion_card_satisfies_in_video_rule():
    plan = base_plan(paid_promotion=True, sponsor="Acme Kits", paid_disclosure_card=(0.0, 4.0))
    codes = {r.code for r in run_gate(make_ctx(plan=plan), SHA).results}
    assert "PAID_PROMOTION_INVIDEO_MISSING" not in codes


def test_paid_promotion_card_too_short_or_late_does_not_count():
    for card in [(0.0, 1.0), (15.0, 20.0)]:
        plan = base_plan(paid_promotion=True, sponsor="Acme", paid_disclosure_card=card)
        assert "PAID_PROMOTION_INVIDEO_MISSING" in {r.code for r in run_gate(make_ctx(plan=plan), SHA).blocks}, card


def test_spoken_paid_disclosure_must_really_be_said():
    plan = base_plan(paid_promotion=True, sponsor="Acme", paid_disclosure_spoken_at=1.0)
    assert "PAID_PROMOTION_INVIDEO_MISSING" in {r.code for r in run_gate(make_ctx(plan=plan), SHA).blocks}
    said = make_ctx(plan=plan, transcript="This video is sponsored by Acme. " + TRANSCRIPT)
    assert "PAID_PROMOTION_INVIDEO_MISSING" not in {r.code for r in run_gate(said, SHA).results}


def test_bare_word_paid_is_not_an_affiliate_disclosure():
    """'I paid nothing for this' must not count as 'I earn a commission'."""
    from test_gate import aff_ctx
    rep = run_gate(aff_ctx(transcript=TRANSCRIPT + " I paid nothing for this kit.", start_card=None), SHA)
    assert "AFFILIATE_START_DISCLOSURE_MISSING" in {r.code for r in rep.blocks}


# ------------------------------------------------------------------- markets numbers
def test_number_magnitudes_are_kept():
    assert numbers("revenue was $1.9B, up 12% and 3.5 million users") >= {"1.9b", "12%", "3.5m"}
    assert "2024" not in numbers("in 2024 it rose")  # years are skipped
    assert numbers("at 2026-09-30 10:15") == set()  # dates and times are not figures


def test_education_term_warns_but_owner_hype_blocks():
    from test_gate import mk_ctx
    said = run_gate(mk_ctx(transcript=TRANSCRIPT + " this is what a short squeeze looks like"), SHA)
    assert "FINANCE_TERM_REVIEW" in {r.code for r in said.warnings}
    va = [{"kind": "context_card", "text": "This hidden gem stock is my favourite pick for the year.", "start": 1.0, "end": 12.0}]
    owned = run_gate(mk_ctx(va=va), SHA)
    assert "FINANCE_PUMP_PATTERN" in {r.code for r in owned.blocks}


def test_model_summary_numbers_must_be_grounded():
    ctx = make_ctx()
    ctx.summary = "Engineers say 97% of bridges fail from wind."
    assert "METADATA_UNGROUNDED" in {r.code for r in run_gate(ctx, SHA).blocks}


# ------------------------------------------------------------------- rights
def test_sharealike_blocked_and_platform_copy_warns():
    sa = make_ctx(rights=own_rights(basis="creative_commons", cc_license="CC-BY-SA-4.0", attribution="A"))
    assert "RIGHTS_SHAREALIKE" in {r.code for r in run_gate(sa, SHA).blocks}
    rip = run_gate(make_ctx(rights=own_rights(obtained_from_rights_holder=False)), SHA)
    assert rip.passed and "RIGHTS_PLATFORM_COPY" in {r.code for r in rip.warnings}


def test_rights_expiring_before_publish_day_blocks_approval(store, settings, tmp_path):
    cid, _ = add_clip(store, tmp_path, rights=own_rights(expires=(datetime.now(UTC) + timedelta(days=2)).date()))
    with pytest.raises(ApprovalError, match="rights"):
        decide(store, settings, ok(cid, visibility="scheduled_public", publish_at=datetime.now(UTC) + timedelta(days=5)))


def test_auto_approve_never_applies_to_licensed_content(store, settings, tmp_path):
    from clipwarden.approval import maybe_auto_approve
    settings.publish.require_human_approval = False
    cid, _ = add_clip(store, tmp_path, rights=own_rights(basis="written_permission", evidence="signed-permission.pdf"))
    assert maybe_auto_approve(store, settings, cid) is False


# ------------------------------------------------------------------- publisher TOCTOU
def test_file_swapped_after_approval_is_not_uploaded(store, settings, tmp_path):
    cid, sha = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))
    stored = store.find_media(sha)
    stored.chmod(0o644)
    stored.write_bytes(b"\0\0\0\x18ftypisom" + os.urandom(64))  # someone swaps the bytes on disk
    sent = []
    res = publish_approved(store, settings, uploader=lambda body, path: sent.append(path) or "vid")
    assert not sent
    assert "changed" in str(res) or store.row("clips", cid)["status"] != "published"
    assert not list((store.root / "upload-staging").glob("*.mp4"))  # staging copy cleaned up


# ------------------------------------------------------------------- audit chain under threads
def test_audit_chain_survives_concurrent_writers(tmp_path):
    s = Store(tmp_path / "d")

    def work(n):
        for i in range(25):
            s.audit.append(f"t{n}", "x", f"s{i}", {"i": i})
            s.upsert("clips", f"c{n}-{i}", status="x")

    ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok_, _bad = s.audit.verify()
    assert ok_ and len(s.audit.entries(limit=1000)) == 150


def test_bad_media_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path / "d").find_media("../../etc/passwd")


# ------------------------------------------------------------------- redaction
def test_url_encoded_refresh_token_redacted():
    assert "abcdefghijkl" not in redact("refresh=1%2F%2Fabcdefghijklmnop&x=1")
    assert "abcdefghijkl" not in redact("?token=abcdefghijklmnop&next=1")


def test_frozen_clip_cannot_be_rerendered(store, settings, tmp_path):
    from clipwarden.pipeline import Pipeline, PipelineError
    cid, _ = add_clip(store, tmp_path)
    decide(store, settings, Decision(clip_id=cid, approved=True, reviewer="o"))
    with pytest.raises(PipelineError, match="frozen"):
        Pipeline(settings, store).render_and_gate(cid)
