"""Stage 9: upload approved clips. Dry-run by default; live only after an explicit switch-on.

Before every upload the publisher re-verifies, from scratch:
1. the kill switch is off;
2. the file on disk has the exact SHA-256 the human approved (no swap after approval);
3. the request body rebuilt from stored metadata hashes to what the gate checked;
4. today's upload count is under both the channel cap and the API quota.
Any mismatch stops the upload and is written to the audit log.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import ClipPlan, Decision, GateReport, Metadata, RightsRecord
from ..store import Store, sha256_file
from .body import apply_decision, body_hash, build_body
from .youtube import PublishError, QuotaError

DEFAULT_DAILY_UPLOAD_QUOTA = 100  # videos.insert calls/day in the Video Uploads bucket (Sept 2026 docs)


def uploads_today(store: Store) -> int:
    day = datetime.now(UTC).date().isoformat()
    return sum(1 for r in store.rows("clips", "status IN ('published','uploading')")
               if str((r.get("upload") or {}).get("at", ""))[:10] == day)


def live_ready(store: Store, settings: Settings) -> tuple[bool, str]:
    if settings.publish.mode != "live":
        return False, "publish.mode is dry-run"
    if not store.get("live_enabled", False):
        return False, "live publishing not switched on (`clipwarden publish enable-live`)"
    if not store.get("dry_runs_completed", 0):
        return False, "complete at least one dry run first"
    ok, bad = store.audit.verify()
    if not ok:
        return False, f"audit log chain broken at entry {bad}"
    return True, "ok"


def publish_approved(store: Store, settings: Settings, *, uploader: Callable[[dict[str, Any], Path], str] | None = None,
                     actor: str = "publisher") -> list[dict[str, Any]]:
    results = []
    for row in store.rows("clips", "status='approved'", order="created"):
        cid = row["id"]
        if store.kill_switch:
            store.audit.append(actor, "publish_halted", cid, {"reason": "kill switch"})
            break
        try:
            results.append(_publish_one(store, settings, row, uploader, actor))
        except QuotaError as exc:
            store.upsert("clips", cid, status="deferred_quota")
            store.audit.append(actor, "publish_deferred", cid, {"reason": f"quota: {exc}"})
            results.append({"clip": cid, "status": "deferred_quota"})
            break  # quota is per project, so stop for today
        except PublishError as exc:
            store.upsert("clips", cid, status="failed")
            store.audit.append(actor, "publish_failed", cid, {"reason": str(exc)[:500]})
            results.append({"clip": cid, "status": "failed", "error": str(exc)[:200]})
    return results


def _publish_one(store: Store, settings: Settings, row: dict[str, Any], uploader, actor: str) -> dict[str, Any]:
    cid = row["id"]
    dec = Decision.model_validate({k: v for k, v in row["decision"].items() if k in Decision.model_fields})
    gate = GateReport.model_validate(row["gate"])
    plan = ClipPlan.model_validate(row["plan"])
    src = store.row("sources", plan.source_id) or {}
    rec = RightsRecord.model_validate(src["rights"])
    md = Metadata.model_validate(row["metadata"])

    path = store.find_media(row["clip_sha"])
    actual = sha256_file(path)
    if actual != gate.clip_sha256 or actual != row["decision"].get("clip_sha256"):
        raise PublishError("clip file changed after approval; re-render and re-approve")
    body = build_body(md, plan, rec)
    if body_hash(body) != gate.metadata_sha256 or body_hash(body) != row["decision"].get("metadata_sha256"):
        raise PublishError("metadata changed after approval; re-gate and re-approve")
    final = apply_decision(body, dec)

    n = uploads_today(store)
    if n >= min(settings.channel.daily_cap, DEFAULT_DAILY_UPLOAD_QUOTA):
        raise QuotaError(f"{n} uploads today; cap reached")

    ready, why = live_ready(store, settings)
    if not ready:
        out = store.root / "dry-run"
        out.mkdir(exist_ok=True)
        (out / f"{cid}.json").write_text(json.dumps({"would_call": "youtube.videos.insert", "part": "snippet,status",
                                                     "body": final, "media": path.name, "why_dry_run": why}, indent=2))
        store.upsert("clips", cid, status="dry_run_published", upload={"at": datetime.now(UTC).isoformat(), "dry_run": True, "why": why})
        store.set("dry_runs_completed", int(store.get("dry_runs_completed", 0)) + 1)
        store.audit.append(actor, "dry_run_publish", cid, {"body_sha256": body_hash(body), "privacy": final["status"]["privacyStatus"],
                                                           "publishAt": final["status"].get("publishAt"), "why": why})
        store.stage(plan.source_id, "publish", "dry-run", f"{cid}: request written to data/dry-run/{cid}.json")
        return {"clip": cid, "status": "dry_run_published"}

    if uploader is None:
        raise PublishError("no uploader configured")
    store.upsert("clips", cid, status="uploading")
    store.audit.append(actor, "upload_start", cid, {"privacy": final["status"]["privacyStatus"], "publishAt": final["status"].get("publishAt")})
    video_id = uploader(final, path)
    store.upsert("clips", cid, status="published", upload={"at": datetime.now(UTC).isoformat(), "video_id": video_id,
                                                         "privacy": final["status"]["privacyStatus"], "publishAt": final["status"].get("publishAt")})
    store.audit.append(actor, "upload_done", cid, {"video_id": video_id})
    store.stage(plan.source_id, "publish", "done", f"{cid}: uploaded as {video_id} ({final['status']['privacyStatus']})")
    return {"clip": cid, "status": "published", "video_id": video_id}
