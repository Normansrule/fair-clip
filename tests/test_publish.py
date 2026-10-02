"""Approval rules, publisher re-verification, YouTube upload retries/quota, token revocation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import base_plan, own_rights

from fairclip.approval import ApprovalError, decide, maybe_auto_approve
from fairclip.models import CheckResult, Decision, GateReport, Metadata, Severity
from fairclip.publish.body import body_hash, build_body
from fairclip.publish.publisher import publish_approved
from fairclip.publish.youtube import PublishError, QuotaError, revoke, upload
from fairclip.vault import EncryptedFileVault


def add_clip(store, tmp_path: Path, cid="clip-a-0001", plan=None, warn=False, block=False, rights=None):
    plan = plan or base_plan(clip_id=cid)
    rights = rights or own_rights()
    f = tmp_path / f"{cid}.mp4"
    f.write_bytes(b"\0\0\0\x18ftypisom" + os.urandom(64))
    sha = store.put_media(f)
    md = Metadata(title="So what actually threatens a bridge?", description="desc", tags=["bridge"])
    body = build_body(md, plan, rights)
    results = [CheckResult(code="OK", check="Safety", severity=Severity.PASS, message="ok")]
    if warn:
        results.append(CheckResult(code="SCREEN_REVIEW", check="Screen", severity=Severity.WARN, message="w"))
    if block:
        results.append(CheckResult(code="VALUE_ADD_MISSING", check="Value-add", severity=Severity.BLOCK, message="b"))
    gate = GateReport(clip_id=cid, passed=not block, results=results, clip_sha256=sha, metadata_sha256=body_hash(body), created_at=datetime.now(UTC))
    store.upsert("sources", plan.source_id, rights=rights.model_dump(mode="json"), media_sha="a" * 64)
    store.upsert("clips", cid, source_id=plan.source_id, created=datetime.now(UTC).isoformat(), plan=plan.model_dump(mode="json"),
                 metadata=md.model_dump(mode="json"), body=body, gate=gate.model_dump(mode="json"), clip_sha=sha,
                 status="blocked" if block else "awaiting_approval")
    return cid, sha


def ok(cid, **kw):
    return Decision(clip_id=cid, approved=True, reviewer="owner", **kw)


# ---------------------------------------------------------------------------------- approval
def test_blocked_clip_cannot_be_approved(store, settings, tmp_path):
    cid, _ = add_clip(store, tmp_path, block=True)
    store.upsert("clips", cid, status="awaiting_approval")  # even if someone forces the status
    with pytest.raises(ApprovalError, match="blocked"):
        decide(store, settings, ok(cid))


def test_warnings_need_acknowledgement(store, settings, tmp_path):
    cid, _ = add_clip(store, tmp_path, warn=True)
    with pytest.raises(ApprovalError, match="acknowledge"):
        decide(store, settings, ok(cid))
    decide(store, settings, ok(cid, warnings_acknowledged=True))
    assert store.row("clips", cid)["status"] == "approved"


def test_reject_needs_reason():
    with pytest.raises(ValueError):
        Decision(clip_id="c", approved=False, reviewer="o", reason="")


def test_paid_promotion_must_stay_private(store, settings, tmp_path):
    cid, _ = add_clip(store, tmp_path, plan=base_plan(clip_id="clip-paid-01", paid_promotion=True), cid="clip-paid-01")
    with pytest.raises(ApprovalError, match="Studio"):
        decide(store, settings, ok(cid, visibility="unlisted"))
    decide(store, settings, ok(cid, visibility="private"))


def test_schedule_respects_daily_cap(store, settings, tmp_path):
    settings.channel.daily_cap = 1
    when = datetime.now(UTC) + timedelta(days=2)
    a, _ = add_clip(store, tmp_path, cid="clip-a-0001")
    b, _ = add_clip(store, tmp_path, cid="clip-b-0002", plan=base_plan(clip_id="clip-b-0002"))
    decide(store, settings, ok(a, visibility="scheduled_public", publish_at=when))
    with pytest.raises(ApprovalError, match="daily cap"):
        decide(store, settings, ok(b, visibility="scheduled_public", publish_at=when))


def test_auto_approve_only_plain_clips(store, settings, tmp_path):
    settings.publish.require_human_approval = False
    a, _ = add_clip(store, tmp_path, cid="clip-plain-01", plan=base_plan(clip_id="clip-plain-01"))
    m, _ = add_clip(store, tmp_path, cid="clip-mkts-01", plan=base_plan(clip_id="clip-mkts-01", tags=["markets"]))
    assert maybe_auto_approve(store, settings, a) is True
    assert store.row("clips", a)["decision"]["visibility"] == "private"
    assert maybe_auto_approve(store, settings, m) is False  # markets: human only, whatever the config says


def test_approval_is_audited_with_hashes(store, settings, tmp_path):
    cid, sha = add_clip(store, tmp_path)
    decide(store, settings, ok(cid, reason="looks right"))
    e = store.audit.entries(limit=1)[0]
    assert e.action == "approve" and e.details["clip_sha256"] == sha and e.details["rights_record"] == "own-001"


# ---------------------------------------------------------------------------------- publisher
def test_dry_run_writes_request_and_never_uploads(store, settings, tmp_path):
    cid, _ = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))
    called = []
    res = publish_approved(store, settings, uploader=lambda b, p: called.append(1) or "vid")
    assert res[0]["status"] == "dry_run_published" and not called
    req = json.loads((store.root / "dry-run" / f"{cid}.json").read_text())
    assert req["body"]["status"]["privacyStatus"] == "private" and req["body"]["status"]["containsSyntheticMedia"] is False


def live(store, settings):
    settings.publish.mode = "live"
    store.set("live_enabled", True)
    store.set("dry_runs_completed", 1)


def test_live_upload_scheduled(store, settings, tmp_path):
    live(store, settings)
    cid, _ = add_clip(store, tmp_path)
    when = datetime.now(UTC) + timedelta(days=1)
    decide(store, settings, ok(cid, visibility="scheduled_public", publish_at=when))
    sent = {}
    res = publish_approved(store, settings, uploader=lambda body, path: sent.update(body) or "VID123")
    assert res[0] == {"clip": cid, "status": "published", "video_id": "VID123"}
    assert sent["status"]["privacyStatus"] == "private" and sent["status"]["publishAt"].endswith("Z")


def test_file_swapped_after_approval_is_refused(store, settings, tmp_path):
    live(store, settings)
    cid, sha = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))
    p = store.find_media(sha)
    p.chmod(0o644)
    p.write_bytes(b"tampered")
    res = publish_approved(store, settings, uploader=lambda b, x: "VID")
    assert res[0]["status"] == "failed" and "changed after approval" in res[0]["error"]


def test_metadata_changed_after_approval_is_refused(store, settings, tmp_path):
    live(store, settings)
    cid, _ = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))
    row = store.row("clips", cid)
    md = row["metadata"] | {"title": "Something sneaky added later"}
    store.upsert("clips", cid, metadata=md)
    res = publish_approved(store, settings, uploader=lambda b, x: "VID")
    assert res[0]["status"] == "failed" and "metadata changed" in res[0]["error"]


def test_kill_switch_halts_publishing(store, settings, tmp_path):
    live(store, settings)
    cid, _ = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))
    store.set_kill_switch(True, "owner")
    assert publish_approved(store, settings, uploader=lambda b, x: "VID") == []
    assert store.row("clips", cid)["status"] == "approved"


def test_quota_defers(store, settings, tmp_path):
    live(store, settings)
    cid, _ = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))

    def boom(b, p):
        raise QuotaError("uploadLimitExceeded")

    assert publish_approved(store, settings, uploader=boom)[0]["status"] == "deferred_quota"


def test_live_needs_a_dry_run_first(store, settings, tmp_path):
    settings.publish.mode = "live"
    store.set("live_enabled", True)
    cid, _ = add_clip(store, tmp_path)
    decide(store, settings, ok(cid))
    called = []
    publish_approved(store, settings, uploader=lambda b, p: called.append(1) or "VID")
    assert not called and store.row("clips", cid)["status"] == "dry_run_published"


# ---------------------------------------------------------------------------------- YouTube client
class FakeHttpError(Exception):
    def __init__(self, status, reason):
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class FakeRequest:
    def __init__(self, script):
        self.script = list(script)

    def next_chunk(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeYT:
    def __init__(self, script):
        self.req = FakeRequest(script)
        self.kw = None

    def videos(self):
        return self

    def insert(self, **kw):
        self.kw = kw
        return self.req


def test_upload_retries_then_succeeds(tmp_path):
    yt = FakeYT([FakeHttpError(503, "backendError"), (None, None), (None, {"id": "abc"})])
    sleeps = []
    vid = upload(yt, {"snippet": {}, "status": {}}, tmp_path / "x.mp4", sleep=sleeps.append, media_factory=lambda p: "media")
    assert vid == "abc" and len(sleeps) == 1 and yt.kw["part"] == "snippet,status" and yt.kw["notifySubscribers"] is False


def test_upload_quota_and_fatal_errors(tmp_path):
    with pytest.raises(QuotaError):
        upload(FakeYT([FakeHttpError(403, "uploadLimitExceeded")]), {"snippet": {}}, tmp_path / "x", media_factory=lambda p: 0)
    with pytest.raises(PublishError):
        upload(FakeYT([FakeHttpError(400, "invalidTitle")]), {"snippet": {}}, tmp_path / "x", media_factory=lambda p: 0)


def test_revoke_calls_google_and_deletes_local(tmp_path):
    v = EncryptedFileVault(tmp_path / "v.enc", passphrase="a long enough passphrase")
    v.save("youtube-oauth", {"refresh_token": "1//secret-token-value"})
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200)

    assert revoke(v, httpx.Client(transport=httpx.MockTransport(handler))) is True
    assert seen["url"].startswith("https://oauth2.googleapis.com/revoke") and v.load("youtube-oauth") is None
