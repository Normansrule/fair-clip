r"""Stage 10: clip metrics (docs/EQUATIONS.md, Equations 2–3).

CTR   = clicks / impressions                         (thumbnail click-through rate)
AVD   = total watch time / views                     (average view duration, s)
APV   = AVD / clip duration                          (average percentage viewed)
r(τ)  = viewers still watching at fraction τ / views (audience retention curve, YouTube's
        audienceWatchRatio at elapsedVideoTimeRatio τ; can exceed 1 when people rewatch)
APV  ≈ ∫₀¹ r(τ) dτ                                   (area under the retention curve)
CR    = conversions / affiliate clicks               (conversion rate)
EPC   = affiliate earnings / affiliate clicks        (earnings per click)

Pulling live numbers uses the YouTube Analytics API with the read-only scope
``yt-analytics.readonly``, requested separately (incremental authorization) only when the owner
turns analytics on. Nothing here can change a video.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


def ctr(clicks: int, impressions: int) -> float:
    return clicks / impressions if impressions else 0.0


def avd(watch_seconds: float, views: int) -> float:
    return watch_seconds / views if views else 0.0


def apv(avd_s: float, duration_s: float) -> float:
    return avd_s / duration_s if duration_s else 0.0


def conversion_rate(conversions: int, clicks: int) -> float:
    return conversions / clicks if clicks else 0.0


def epc(earnings: float, clicks: int) -> float:
    return earnings / clicks if clicks else 0.0


def area_under_retention(tau: list[float], ratio: list[float]) -> float:
    """Trapezoid rule over the retention curve; ≈ average percentage viewed."""
    t, r = np.asarray(tau, float), np.asarray(ratio, float)
    return float(np.sum((t[1:] - t[:-1]) * (r[1:] + r[:-1]) / 2.0))


def biggest_drop(tau: list[float], ratio: list[float], skip: float = 0.0) -> tuple[float, float]:
    """Where the curve falls fastest (τ, drop per step): the moment to look at in the edit.

    `skip` ignores the first part of the clip (some swipe-away right after the hook is normal).
    """
    r = np.asarray(ratio, float)
    t = np.asarray(tau, float)
    d = np.diff(r)
    d[t[1:] <= skip] = 0.0
    i = int(np.argmin(d))
    return float(t[i + 1]), float(-d[i])


@dataclass
class ClipStats:
    views: int
    watch_seconds: float
    impressions: int
    clicks: int
    duration_s: float
    affiliate_clicks: int = 0
    conversions: int = 0
    earnings: float = 0.0

    def summary(self) -> dict[str, float]:
        a = avd(self.watch_seconds, self.views)
        return {"ctr": ctr(self.clicks, self.impressions), "avd_s": a, "apv": apv(a, self.duration_s),
                "conversion_rate": conversion_rate(self.conversions, self.affiliate_clicks),
                "epc": epc(self.earnings, self.affiliate_clicks)}


def retention_query(video_id: str, start: str, end: str) -> dict[str, Any]:
    """Parameters for youtubeAnalytics.reports.query (audience retention report)."""
    return {"ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": "audienceWatchRatio,relativeRetentionPerformance",
            "dimensions": "elapsedVideoTimeRatio", "filters": f"video=={video_id}"}


def parse_report(resp: dict[str, Any]) -> dict[str, list[float]]:
    cols = [h["name"] for h in resp.get("columnHeaders", [])]
    out: dict[str, list[float]] = {c: [] for c in cols}
    for row in resp.get("rows", []) or []:
        for c, v in zip(cols, row, strict=True):
            out[c].append(float(v))
    return out
