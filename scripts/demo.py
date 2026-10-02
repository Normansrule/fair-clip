#!/usr/bin/env python3
"""`make demo`: run the whole pipeline on the bundled CC0 sample in under a minute.

Creates ./demo-data with:
  clip A  top-ranked moment + owner commentary      -> passes the gate, awaits YOUR approval
  clip B  second moment, no value-add               -> BLOCKED (VALUE_ADD_MISSING)
  clip C  affiliate example with full disclosures   -> passes, awaits approval (human-only)
Then open the dashboard to approve or reject:  clipwarden --config demo-data/clipwarden.toml dashboard
Nothing is uploaded: the demo config is dry-run and has no OAuth token.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clipwarden.config import load_settings  # noqa: E402
from clipwarden.pipeline import Pipeline  # noqa: E402

DEMO = ROOT / "examples" / "demo"
DATA = ROOT / "demo-data"
CONFIG = """data_dir = "."
link_allowlist = ["https://example.com/kits/bridge-kit"]

[channel]
name = "Bridge Basics"
topics = ["bridge", "cable", "tension", "compression", "tower", "wind", "anchorage", "load", "weight", "deck"]
daily_cap = 3

[publish]
mode = "dry-run"

[dashboard]
host = "127.0.0.1"
port = 8765
"""

AFFILIATE_PLAN = {
    "template": "product-note",
    "tags": ["affiliate"],
    "lower_third": "Bridge Basics · build it yourself",
    "value_add": [
        {"kind": "analysis", "template": "steps", "start": 6.0, "end": 16.0, "author": "owner",
         "text": "Try it at home: the flatter the string, the harder you pull. Real main cables keep a deliberate sag.",
         "data": {"items": ["Hang a paper deck from string", "Pull the string ends apart", "Watch the sag shrink as you pull"]}},
    ],
    "affiliate": {
        "links": [{"program_id": "example-kits", "url": "https://example.com/kits/bridge-kit", "product": "Example bridge-building kit"}],
        "products_evaluated": ["Example bridge-building kit"],
        "start_card": [0.3, 5.0],
        "recommendation_times": [17.0],
        "recommendation_cards": [[16.2, 20.5]],
        "disclosure_text": "I earn a commission if you buy the kit through my link.",
    },
}


def illustrative_retention(n: int = 41) -> dict:
    """A made-up but plausible curve, clearly labelled as illustrative in the UI."""
    tau = [i / (n - 1) for i in range(n)]
    ratio = []
    for t in tau:
        r = 1.0 - 0.16 * (1 - math.exp(-t * 18))  # early swipe-away after the hook
        r -= 0.22 * t  # slow decay
        r -= 0.10 / (1 + math.exp(-(t - 0.55) * 40))  # a sharp drop just past the middle
        r += 0.07 * math.exp(-((t - 0.3) ** 2) / 0.002)  # rewatch bump
        ratio.append(round(r, 4))
    return {"tau": tau, "ratio": ratio, "illustrative": True,
            "notes": [[0.3, "rewatch bump: 'it is wind'"]],
            "stats": {"views": "illustrative", "CTR": "n/a (Shorts feed)"}}


def main() -> int:
    t0 = time.time()
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir()
    (DATA / "clipwarden.toml").write_text(CONFIG)
    p = Pipeline(load_settings(DATA / "clipwarden.toml"))
    sid = p.ingest(DEMO / "source.mp4", DEMO / "rights.json", DEMO / "transcript.json", actor="demo")
    ranked = p.store.rows("candidates", "source_id=?", (sid,), order="rank")
    print(f"[1-3] ingested {sid}: {len(ranked)} ranked candidates (backend: {p.runner.last_backend})")
    for r in ranked:
        c = r["data"]
        print(f"      #{c['rank']} {c['start']:5.1f}-{c['end']:5.1f}s S={c['score']:+.3f} {c['text'][:60]}…")

    plan_a = json.loads((DEMO / "plan.json").read_text())
    a = p.plan_from_candidate(ranked[0]["id"], actor="demo", **plan_a)
    b = p.plan_from_candidate(ranked[1]["id"], actor="demo", template="bare")
    c = p.plan_from_candidate(ranked[2]["id"], actor="demo", **AFFILIATE_PLAN)
    for plan, label in ((a, "A"), (b, "B"), (c, "C")):
        rep = p.render_and_gate(plan.clip_id, actor="demo")
        verdict = "PASSED -> awaiting approval" if rep.passed else "BLOCKED: " + ", ".join(x.code for x in rep.blocks)
        print(f"[4-7] clip {label} {plan.clip_id}: {verdict}")

    p.store.set("analytics", {a.clip_id: illustrative_retention()})
    site = ROOT / "site" / "data"
    site.mkdir(parents=True, exist_ok=True)
    p.export_demo(sid, site / "demo.json")
    ok, _ = p.store.audit.verify()
    print(f"[audit] {len(p.store.audit.entries(limit=1000))} entries, chain {'intact' if ok else 'BROKEN'}")
    print(f"done in {time.time() - t0:.0f}s. Review and approve at the dashboard:\n"
          f"  clipwarden --config demo-data/clipwarden.toml dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
