"""The exact YouTube Data API v3 ``videos.insert`` request body, built in one place.

The compliance gate checks *this* dict (not a separate description of it), and the publisher
re-builds it at upload time and refuses to upload if its hash differs from what was approved.
Privacy and schedule are the only fields added after approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Any

from ..models import ClipPlan, Decision, Metadata, RightsRecord, Visibility


def synthetic_required(plan: ClipPlan, rights: RightsRecord) -> bool:
    """Disclose realistic altered/synthetic content. When unsure, disclose (policy default)."""
    return bool(rights.synthetic_or_altered or plan.synthetic_or_altered)


def paid_promotion_required(plan: ClipPlan) -> bool:
    return bool(plan.paid_promotion or "sponsored" in plan.tags or (plan.markets and plan.markets.paid_by))


def build_body(md: Metadata, plan: ClipPlan, rights: RightsRecord, category_id: str = "27") -> dict[str, Any]:
    return {
        "snippet": {
            "title": md.title,
            "description": md.description,
            "tags": md.tags,
            "categoryId": category_id,  # 27 = Education
        },
        "status": {
            "privacyStatus": "private",  # always starts private; see apply_decision
            "selfDeclaredMadeForKids": plan.made_for_kids,
            "containsSyntheticMedia": synthetic_required(plan, rights),
            "embeddable": True,
        },
    }
    # Note: paidProductPlacementDetails is readable but NOT settable through videos.insert or
    # videos.update (checked against the API reference, 2026-09-30). Paid-promotion clips
    # therefore upload as private only, and the owner ticks "includes paid promotion" in
    # YouTube Studio before publishing. See approval.py and docs/POLICY.md.


def body_hash(body: dict[str, Any]) -> str:
    """Hash of everything except the post-approval privacy/schedule fields."""
    b = json.loads(json.dumps(body))
    b["status"].pop("privacyStatus", None)
    b["status"].pop("publishAt", None)
    return hashlib.sha256(json.dumps(b, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def apply_decision(body: dict[str, Any], d: Decision) -> dict[str, Any]:
    b = json.loads(json.dumps(body))
    if d.visibility is Visibility.UNLISTED:
        b["status"]["privacyStatus"] = "unlisted"
    elif d.visibility is Visibility.SCHEDULED_PUBLIC:
        assert d.publish_at is not None
        b["status"]["privacyStatus"] = "private"  # publishAt requires private; YouTube flips it
        b["status"]["publishAt"] = d.publish_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        b["status"]["privacyStatus"] = "private"
    return b
