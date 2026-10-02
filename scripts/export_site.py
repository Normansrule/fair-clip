#!/usr/bin/env python3
"""Copy small web versions of the demo clips + their gate reports into site/ for GitHub Pages.

Run after `make demo` (and `make media` if you want the approved state). Re-encodes at
540/640 px wide so the Pages site stays light (~1.5 MB of video total).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fairclip.config import load_settings  # noqa: E402
from fairclip.pipeline import Pipeline  # noqa: E402
from fairclip.store import Store  # noqa: E402

SITE = ROOT / "site"


def enc(src: Path, dst: Path, width: int) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vf", f"scale={width}:-2", "-c:v", "libx264",
                    "-preset", "slow", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                    "-movflags", "+faststart", "-map_metadata", "-1", str(dst)], check=True)


def main() -> int:
    s = load_settings(ROOT / "demo-data" / "fairclip.toml")
    st = Store(s.data_path)
    clips = {r["id"]: r for r in st.rows("clips", order="created")}
    ids = list(clips)
    a, c = clips[ids[0]], clips[ids[2]]
    (SITE / "media").mkdir(parents=True, exist_ok=True)
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    enc(st.find_media(a["clip_sha"]), SITE / "media" / "clip-a.mp4", 540)
    enc(st.find_media(a["preview_sha"]), SITE / "media" / "before-a.mp4", 640)
    enc(st.find_media(c["clip_sha"]), SITE / "media" / "clip-c-affiliate.mp4", 540)
    for sha, name in ((a["posters"]["clip"], "clip-a.jpg"), (a["posters"]["before"], "before-a.jpg"), (c["posters"]["clip"], "clip-c-affiliate.jpg")):
        (SITE / "media" / name).write_bytes(st.find_media(sha).read_bytes())
    gates = {cid: {"passed": r["gate"]["passed"], "results": r["gate"]["results"], "title": r["metadata"]["title"],
                   "plan_tags": r["plan"]["tags"]} for cid, r in clips.items()}
    (SITE / "data" / "gates.json").write_text(json.dumps(gates, indent=1))
    src = st.rows("sources")[0]["id"]
    d = Pipeline(s, st).export_demo(src, SITE / "data" / "demo.json")
    d.pop("exported", None)
    for cand in d["candidates"]:
        cand["text"] = cand["text"][:400]
    (SITE / "data" / "demo.json").write_text(json.dumps(d, indent=1))
    if "--freeze-eq1" in sys.argv:  # refresh the frozen data behind the Eq. 1 charts
        frozen = {"_doc": "Top-5 candidates from the bundled sample (make demo), frozen so the Eq. 1 charts are reproducible. "
                          "Regenerate with: python scripts/export_site.py --freeze-eq1 && python scripts/render_assets.py",
                  "weights": d["weights"],
                  "candidates": [{k: c[k] for k in ("candidate_id", "rank", "start", "end", "score", "z")} for c in d["candidates"]]}
        (ROOT / "assets" / "src" / "eq1-candidates.json").write_text(json.dumps(frozen, indent=1))
    print("site/media and site/data updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
