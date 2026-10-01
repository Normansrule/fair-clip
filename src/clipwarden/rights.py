"""Stage 1: rights intake. No valid rights record, no processing. Ever."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from .models import CCLicense, RightsBasis, RightsRecord


class RightsError(Exception):
    """Raised when a source may not be processed. The message says why, in plain words."""


def load_rights(path: Path) -> RightsRecord:
    if not path.exists():
        raise RightsError(f"no rights record at {path}: the pipeline refuses to process this source")
    if path.stat().st_size > 64 * 1024:
        raise RightsError("rights record is larger than 64 KB; refusing to parse")
    try:
        return RightsRecord.model_validate(json.loads(path.read_text("utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RightsError(f"rights record is invalid: {exc}") from exc


def rights_problems(r: RightsRecord, *, media_sha256: str | None, monetized: bool, today: date | None = None) -> list[tuple[str, str]]:
    """Every reason this record does not cover clipping + YouTube publishing.

    Returned as (code, message) pairs so the compliance gate can reuse the same logic.
    """
    today = today or date.today()
    out: list[tuple[str, str]] = []
    if r.expires and r.expires < today:
        out.append(("RIGHTS_EXPIRED", f"rights record {r.record_id} expired on {r.expires}"))
    if not r.allows_youtube:
        out.append(("RIGHTS_SCOPE", "rights record does not cover publishing on YouTube"))
    if not r.allows_modification or r.cc_license in {CCLicense.BY_ND, CCLicense.BY_NC_ND}:
        out.append(("RIGHTS_NO_DERIVATIVES", "license forbids modification; clipping creates a derivative"))
    if monetized and (not r.allows_monetization or r.cc_license in {CCLicense.BY_NC, CCLicense.BY_NC_SA, CCLicense.BY_NC_ND}):
        out.append(("RIGHTS_NONCOMMERCIAL", "license is non-commercial but the channel is monetized"))
    if r.source_sha256 and media_sha256 and r.source_sha256 != media_sha256:
        out.append(("RIGHTS_FILE_MISMATCH", "file hash differs from the one named in the rights record"))
    if r.basis in {RightsBasis.WRITTEN_PERMISSION, RightsBasis.LICENSE} and len(r.evidence) < 3:
        out.append(("RIGHTS_NO_EVIDENCE", "permission/license basis needs a pointer to the evidence"))
    return out


def check_intake(r: RightsRecord, media_sha256: str, monetized: bool) -> None:
    problems = rights_problems(r, media_sha256=media_sha256, monetized=monetized)
    if problems:
        raise RightsError("; ".join(m for _, m in problems))


def attribution_line(r: RightsRecord) -> str:
    if r.basis is RightsBasis.OWN_CONTENT:
        return ""
    if r.cc_license:
        return f"Source: {r.attribution} ({r.cc_license.value})"
    return f"Source: {r.source_title}, used with permission of {r.rights_holder}"
