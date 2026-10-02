"""Stage 8: human approval. Automate the labor, never the judgment."""

from __future__ import annotations

from datetime import UTC, datetime

from .affiliate import load_registry, registry_problems
from .config import Settings
from .models import ClipPlan, Decision, GateReport, RightsBasis, RightsRecord, Visibility
from .publish.body import paid_promotion_required
from .rights import rights_problems
from .store import Store

HUMAN_ONLY_TAGS = {"markets", "affiliate", "sponsored"}


class ApprovalError(ValueError):
    pass


def human_required(plan: ClipPlan, settings: Settings) -> bool:
    """Markets, affiliate and sponsored clips always need a human, whatever the config says."""
    return settings.publish.require_human_approval or bool(HUMAN_ONLY_TAGS & set(plan.tags)) \
        or plan.affiliate is not None or plan.markets is not None


def scheduled_on(store: Store, day: str) -> int:
    n = 0
    for c in store.rows("clips", "status IN ('approved','uploading','published','dry_run_published')"):
        d = c.get("decision") or {}
        when = d.get("publish_at") or d.get("decided_at") or ""
        if str(when)[:10] == day:
            n += 1
    return n


def decide(store: Store, settings: Settings, d: Decision) -> dict:
    row = store.row("clips", d.clip_id)
    if row is None:
        raise ApprovalError("unknown clip")
    if row["status"] != "awaiting_approval":
        raise ApprovalError(f"clip is '{row['status']}', not awaiting approval")
    gate = GateReport.model_validate(row["gate"])
    plan = ClipPlan.model_validate(row["plan"])
    if d.approved:
        if not gate.passed:
            raise ApprovalError("the compliance gate blocked this clip; it cannot be approved")
        if gate.warnings and not d.warnings_acknowledged:
            raise ApprovalError("acknowledge the gate warnings before approving")
        src = store.row("sources", plan.source_id) or {}
        if src.get("rights"):
            rec = RightsRecord.model_validate(src["rights"])
            day = d.publish_at.astimezone(UTC).date() if d.publish_at else datetime.now(UTC).date()
            probs = rights_problems(rec, media_sha256=src.get("media_sha"), monetized=settings.channel.monetized, today=day)
            if probs:
                raise ApprovalError(f"rights won't cover this clip on {day}: " + "; ".join(m for _, m in probs))
        if plan.affiliate:  # the registry may have changed since the gate ran (program paused, ...)
            reg_probs = registry_problems(plan.affiliate, load_registry(store))
            if reg_probs:
                raise ApprovalError("affiliate registry changed since the gate ran: " + "; ".join(reg_probs) + ". Re-render the clip.")
        if paid_promotion_required(plan) and d.visibility is not Visibility.PRIVATE:
            raise ApprovalError("paid-promotion clips upload private: tick 'includes paid promotion' in "
                                "YouTube Studio, then publish from Studio")
        if d.visibility is Visibility.SCHEDULED_PUBLIC:
            assert d.publish_at is not None
            if d.publish_at.astimezone(UTC) <= datetime.now(UTC):
                raise ApprovalError("publish time must be in the future")
            day = d.publish_at.astimezone(UTC).date().isoformat()
            if scheduled_on(store, day) >= settings.channel.daily_cap:
                raise ApprovalError(f"daily cap of {settings.channel.daily_cap} reached for {day}")
    record = d.model_dump(mode="json") | {
        "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "clip_sha256": gate.clip_sha256,
        "metadata_sha256": gate.metadata_sha256,
    }
    status = "approved" if d.approved else "rejected"
    if not store.set_if_status("clips", d.clip_id, {"awaiting_approval"}, status=status, decision=record):
        raise ApprovalError("the clip changed while you were deciding (re-rendered or decided elsewhere); reload and review again")
    store.audit.append(d.reviewer, "approve" if d.approved else "reject", d.clip_id, {
        "reason": d.reason, "visibility": d.visibility.value, "publish_at": record["publish_at"],
        "clip_sha256": gate.clip_sha256, "metadata_sha256": gate.metadata_sha256,
        "rights_record": (store.row("sources", plan.source_id) or {}).get("rights", {}).get("record_id"),
        "disclosures": {"affiliate": plan.affiliate is not None, "markets": plan.markets is not None,
                        "paid_promotion": paid_promotion_required(plan), "synthetic": (row.get("body") or {}).get("status", {}).get("containsSyntheticMedia")},
        "warnings_acknowledged": [w.code for w in gate.warnings] if d.approved else [],
    })
    store.clip_log(d.clip_id, f"{status} by {d.reviewer}: {d.reason}")
    return record


def maybe_auto_approve(store: Store, settings: Settings, clip_id: str) -> bool:
    """Only when the owner turned human approval off, and only for plain own-content clips
    that passed with zero warnings. Result: a *private* upload, never public."""
    row = store.row("clips", clip_id)
    if row is None or row["status"] != "awaiting_approval":
        return False
    plan = ClipPlan.model_validate(row["plan"])
    gate = GateReport.model_validate(row["gate"])
    src = store.row("sources", plan.source_id) or {}
    own = (src.get("rights") or {}).get("basis") == RightsBasis.OWN_CONTENT.value
    if human_required(plan, settings) or not gate.passed or gate.warnings or not own:
        return False
    decide(store, settings, Decision(clip_id=clip_id, approved=True, reviewer="policy:auto-approve",
                                     reason="human approval disabled for plain own-content clips; uploads private",
                                     visibility=Visibility.PRIVATE))
    return True
