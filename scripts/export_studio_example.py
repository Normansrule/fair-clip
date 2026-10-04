#!/usr/bin/env python3
"""Run the Studio on the bundled example exactly as a user would (examples/studio/project.json
holds the owner's inputs: brand, rights, a take and a title per moment) and export the results:

* dist/fair-clip-example-clips.zip      the same download the Studio gives you (release asset)
* site/media/studio-0N.mp4 + .jpg        web-sized copies for the Pages site
* site/data/studio-example.json          titles, descriptions, tags and checklists for the site
* assets/studio-example-clips.png        one frame from each clip, for the README

Run: python scripts/export_studio_example.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fairclip import studio  # noqa: E402
from fairclip.config import Settings  # noqa: E402
from fairclip.pipeline import Pipeline  # noqa: E402
from fairclip.studio_run import (  # noqa: E402
    analyze_source,
    gate_inputs,
    make_clip,
    owner_settings,
    write_package,
)

EX = ROOT / "examples" / "studio"


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def main() -> None:
    project = json.loads((EX / "project.json").read_text())
    brand = studio.Brand.model_validate(project["brand"])
    choice = studio.RightsChoice.model_validate(project["rights"])
    with tempfile.TemporaryDirectory() as tmp:
        p = Pipeline(Settings(data_dir=tmp))
        work = Path(tmp) / "work"
        sess = analyze_source(EX / "source.mp4", (EX / "captions.srt").read_text(), brand, choice, runner=p.runner, work=work,
                              settings=owner_settings(p.s, brand))
        made = []
        for n, (c, cand) in enumerate(zip(project["clips"], sess.candidates, strict=True), 1):
            if c["moment"] != {"start": round(cand.start, 2), "end": round(cand.end, 2)}:
                sys.exit(f"moment {n} moved: re-run `fair-clip studio` to refresh examples/studio/project.json")
            take = studio.Take(text=c["take"], template=c["template"], data=c.get("data"))
            made.append(make_clip(sess, cand, take, c.get("title", ""), n, runner=p.runner, work=work, quality="high",
                                  gate=gate_inputs(p)))
        blocked = [m.finished.plan.clip_id for m in made if not m.finished.passed]
        if blocked:
            sys.exit(f"example clips blocked by the gate: {blocked}")
        out = Path(tmp) / "out"
        zpath = write_package(made, brand, out)
        (ROOT / "dist").mkdir(exist_ok=True)
        shutil.copy2(zpath, ROOT / "dist" / "fair-clip-example-clips.zip")
        media, cards, frames = ROOT / "site" / "media", [], []
        for k, m in enumerate(made, 1):
            f = m.finished
            web = media / f"studio-{k:02d}.mp4"
            ff("-i", str(m.clip), "-c:v", "libx264", "-preset", "slow", "-crf", "27", "-vf", "scale=540:-2", "-c:a", "aac", "-b:a", "96k",
               "-movflags", "+faststart", "-map_metadata", "-1", str(web))
            ff("-ss", "3.5", "-i", str(m.clip), "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(web.with_suffix(".jpg")))
            frames.append(Path(tmp) / f"f{k}.png")
            ff("-ss", "9", "-i", str(m.clip), "-frames:v", "1", "-vf", "scale=360:-2", str(frames[-1]))
            cards.append({"video": f"media/{web.name}", "poster": f"media/{web.with_suffix('.jpg').name}", "file": f.filename,
                          "title": f.metadata.title, "description": f.metadata.description, "tags": f.metadata.tags,
                          "checklist": studio.studio_checklist(f.body, f.plan), "take": f.plan.value_add[0].text,
                          "seconds": round(f.duration, 1)})
        (ROOT / "site" / "data" / "studio-example.json").write_text(json.dumps({"brand": brand.channel, "clips": cards},
                                                                                indent=1, ensure_ascii=False) + "\n")
        inputs = [x for f in frames for x in ("-i", str(f))]
        ff(*inputs, "-filter_complex", f"{''.join(f'[{i}:v]pad=iw+24:ih:12:0:color=0x0B1020[p{i}];' for i in range(len(frames)))}"
           f"{''.join(f'[p{i}]' for i in range(len(frames)))}hstack={len(frames)},pad=iw+24:ih+24:12:12:color=0x0B1020",
           str(ROOT / "assets" / "studio-example-clips.png"))
    print("exported: dist/fair-clip-example-clips.zip, site/media/studio-*.mp4, site/data/studio-example.json, "
          "assets/studio-example-clips.png", file=sys.stderr)


if __name__ == "__main__":
    main()
