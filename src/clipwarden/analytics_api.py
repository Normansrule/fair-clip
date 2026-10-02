"""Stage 10, live: pull per-clip metrics from the YouTube Analytics API (read-only).

* Scope: ``yt-analytics.readonly`` only, in its OWN vault entry, separate from the upload token.
  Connecting analytics never widens what the upload token can do, and revoking one leaves the
  other alone.
* Endpoint: ``GET https://youtubeanalytics.googleapis.com/v2/reports`` (reports.query), one
  video at a time, ``ids=channel==MINE``. Two queries per clip: totals (views, minutes watched,
  average view duration and percentage) and the audience-retention curve
  (``audienceWatchRatio`` by ``elapsedVideoTimeRatio``).
* Impressions and thumbnail click-through rate are not pulled here: they come from YouTube
  Studio (import them with ``clipwarden analytics import``). Shorts in the Shorts feed have no
  thumbnail click anyway.
* Everything the API returns is treated as untrusted numbers: parsed to float, range-checked,
  capped in size. Nothing here can change a video.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any

import httpx

from .analytics import parse_report, retention_query
from .redact import redact
from .scoring import FEATURES
from .store import Store

ANALYTICS_TOKEN = "youtube-analytics-oauth"  # noqa: S105 - vault entry name, not a secret
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
TOTALS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage"
MAX_POINTS = 200


class AnalyticsError(RuntimeError):
    pass


def totals_query(video_id: str, start: str, end: str) -> dict[str, Any]:
    return {"ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": TOTALS, "filters": f"video=={video_id}"}


def _get(http: httpx.Client, token: str, params: dict[str, Any]) -> dict[str, Any]:
    r = http.get(REPORTS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 429 or (r.status_code == 403 and "quota" in r.text.lower()):
        raise AnalyticsError("YouTube Analytics quota reached; try again tomorrow")
    if r.status_code in (401, 403):
        raise AnalyticsError(f"YouTube Analytics refused the request ({r.status_code}): reconnect with "
                             "`clipwarden analytics connect`")
    if r.status_code != 200:
        raise AnalyticsError(f"YouTube Analytics error {r.status_code}: {redact(r.text[:200])}")
    try:
        data = r.json()
    except ValueError as exc:
        raise AnalyticsError("YouTube Analytics sent a response that isn't JSON") from exc
    if not isinstance(data, dict):
        raise AnalyticsError("unexpected response shape")
    return data


def _parse(resp: dict[str, Any]) -> dict[str, list[float]]:
    """parse_report, but any odd shape (missing names, text cells, ragged rows) is one clip's error."""
    try:
        return parse_report(resp)
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyticsError(f"unexpected report format ({type(exc).__name__})") from exc


def _finite(x: float, lo: float, hi: float) -> bool:
    return math.isfinite(x) and lo <= x <= hi


def fetch_clip(http: httpx.Client, token: str, video_id: str, start: str, end: str) -> dict[str, Any]:
    """One clip's totals + retention, validated. Retention may be empty for low-view videos."""
    tot = _parse(_get(http, token, totals_query(video_id, start, end)))
    stats: dict[str, float] = {}
    for k in TOTALS.split(","):
        vals = tot.get(k) or []
        if vals and _finite(vals[0], 0, 1e12):
            stats[k] = vals[0]
    ret = _parse(_get(http, token, retention_query(video_id, start, end)))
    tau, ratio = ret.get("elapsedVideoTimeRatio", []), ret.get("audienceWatchRatio", [])
    if len(tau) != len(ratio):  # columns come from the same rows, so this means a broken report
        raise AnalyticsError("retention report columns have different lengths")
    pairs = [(t, r) for t, r in zip(tau, ratio, strict=True) if _finite(t, 0, 1) and _finite(r, 0, 20)][:MAX_POINTS]
    pairs.sort()
    return {"tau": [p[0] for p in pairs], "ratio": [p[1] for p in pairs], "stats": stats}


def pull(store: Store, http: httpx.Client, token: str, *, today: date | None = None, actor: str = "analytics") -> dict[str, Any]:
    """Pull every published clip that has a YouTube video id. Returns a summary for the CLI/UI."""
    today = today or datetime.now(UTC).date()
    fresh: dict[str, dict[str, Any]] = {}
    done, empty, errors = 0, 0, []
    for row in store.rows("clips", "status='published'"):
        up = row.get("upload") or {}
        vid = str(up.get("video_id") or "")
        if not vid or not vid.replace("-", "").replace("_", "").isalnum() or len(vid) > 20:
            continue
        start = str(up.get("at", ""))[:10] or today.isoformat()
        try:
            got = fetch_clip(http, token, vid, start, today.isoformat())
        except AnalyticsError as exc:
            errors.append(f"{row['id']}: {exc}")
            if "quota" in str(exc):
                break
            continue
        if len(got["tau"]) < 2:
            empty += 1
        got |= {"illustrative": False, "video_id": vid, "pulled_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "notes": []}
        fresh[row["id"]] = got
        done += 1
    # Atomic merge (no network inside it): a concurrent pull or import, even from another process,
    # can't lose clips, and an empty curve never replaces a curve we already had.
    def merge(current: Any) -> dict[str, Any]:
        data = dict(current or {})
        for cid, got in fresh.items():
            old = data.get(cid) or {}
            if len(got["tau"]) < 2 and len(old.get("tau") or []) >= 2 and not old.get("illustrative"):
                got["tau"], got["ratio"] = old["tau"], old["ratio"]
            data[cid] = got
        return data

    store.update("analytics", merge)
    store.audit.append(actor, "analytics_pull", "analytics", {"clips": done, "no_retention_yet": empty, "errors": len(errors)})
    return {"clips": done, "no_retention_yet": empty, "errors": errors}


# ----------------------------------------------------------------------------- feedback loop input


def _iou(a0: float, a1: float, b0: float, b1: float) -> float:
    """Intersection over union of two time windows: 1.0 only for the same window (a candidate nested
    inside the clip scores low, so overlapping candidates can't be mixed up)."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return inter / union if union > 0 else 0.0


def training_rows(store: Store, min_views: int = 50) -> list[dict[str, Any]]:
    """Join live analytics with the scores the clip had when it was picked (Equation 4 input).

    Outcome = average percentage viewed (APV, 0-1). Only real (non-illustrative) data with at
    least `min_views` views counts, so a handful of early views can't steer the weights.
    """
    analytics = store.get("analytics") or {}
    rows = []
    for cid, a in analytics.items():
        if a.get("illustrative"):
            continue
        st = a.get("stats") or {}
        views, pct = st.get("views"), st.get("averageViewPercentage")
        if not isinstance(views, int | float) or views < min_views or not isinstance(pct, int | float):
            continue
        clip = store.row("clips", cid)
        if not clip:
            continue
        plan = clip.get("plan") or {}
        best, score = None, 0.0
        for c in store.rows("candidates", "source_id=?", (plan.get("source_id"),), order="rank"):
            d = c["data"]
            o = _iou(float(plan.get("start", 0)), float(plan.get("end", 0)), float(d["start"]), float(d["end"]))
            if o > score:  # strict: on a tie the better-ranked candidate (seen first) wins
                best, score = d, o
        if best is None or score < 0.8 or not all(k in (best.get("z") or {}) for k in FEATURES):
            continue
        rows.append({"clip": cid, "z": {k: float(best["z"][k]) for k in FEATURES}, "outcome": float(pct) / 100.0,
                     "views": views})
    return rows


def save_proposal(store: Store, current: dict[str, float], proposed: dict[str, float], evidence: dict[str, Any], actor: str) -> int:
    """Store a weight proposal as 'pending'. Only a person on the Settings page can apply it."""
    import json

    with store.lock, store.conn:
        cur = store.conn.execute(
            "INSERT INTO weight_proposals (created, current, proposed, evidence, status) VALUES (datetime('now'), ?, ?, ?, 'pending')",
            (json.dumps(current), json.dumps(proposed), json.dumps(evidence)))
    store.audit.append(actor, "weights_proposed", "scoring", {"proposed": proposed, "n": evidence.get("n"),
                                                               "from": evidence.get("from", "file")})
    return int(cur.lastrowid or 0)


def propose_from_analytics(store: Store, current: dict[str, float], actor: str) -> tuple[int, dict[str, float]]:
    from .scoring import propose_weights

    rows = training_rows(store)
    proposed, raw = propose_weights(current, [r["z"] for r in rows], [r["outcome"] for r in rows])
    pid = save_proposal(store, current, proposed, {"n": len(rows), "raw_fit": raw, "from": "youtube-analytics",
                                                   "clips": [r["clip"] for r in rows][:200]}, actor)
    return pid, proposed
