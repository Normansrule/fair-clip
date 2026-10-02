#!/usr/bin/env python3
"""Stills and animated previews of every value-add template (docs/TEMPLATES.md, README, site).

    python scripts/render_templates.py            # assets/templates/<name>.png + .gif, gallery.png
    python scripts/render_templates.py --only bar

Draws each example from fairclip.media.templates.EXAMPLES over a real 9:16 frame of the bundled
CC0 sample talk, with the same ASS renderer the pipeline uses. The sample is our own pinned file,
so it runs locally without the sandbox (like `make demo`).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fairclip.media import overlay  # noqa: E402
from fairclip.media.templates import EXAMPLES  # noqa: E402
from fairclip.models import ClipPlan  # noqa: E402

SRC = ROOT / "examples" / "demo" / "source.mp4"
OUT = ROOT / "assets" / "templates"
FONTS = ROOT / "src" / "fairclip" / "data" / "fonts"
SEEK = 94.0  # the "Tacoma Narrows" slide
SECONDS = 9.0
W, H = 1080, 1920
VF = (f"[0:v]split[a][b];[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
      f"boxblur=8:2,eq=brightness=-0.18,scale={W}:{H}[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,"
      "ass=overlay.ass:fontsdir={fonts}")


def ass_for(name: str) -> str:
    va = dict(EXAMPLES[name]) | {"start": 0.0, "end": SECONDS - 0.3}
    plan = ClipPlan.model_validate({"clip_id": f"tpl-{name}", "source_id": "src-demo", "start": 0, "end": SECONDS,
                                    "hook_seconds": 0, "value_add": [va]})

    class _Map:  # identity timeline: no jump cuts in a preview
        duration = SECONDS

    return overlay.build_ass(plan, [], _Map(), hook_text="", channel="", attribution="")


def render(name: str, work: Path) -> tuple[Path, Path]:
    (work / "overlay.ass").write_text(ass_for(name))
    png, gif = OUT / f"{name}.png", OUT / f"{name}.gif"
    vf = VF.format(fonts=FONTS)
    base = ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{SEEK}", "-t", f"{SECONDS}", "-i", str(SRC)]
    # GIF starts 0.25 s in: the panel has faded in (a good first frame for thumbnails and
    # reduced-motion previews) while the count-up, steps and bars are still mid-animation.
    # Still: the moment every element is on screen.
    subprocess.run([*base, "-filter_complex", vf + f",trim=start={SECONDS * 0.8:.2f},setpts=PTS-STARTPTS,scale=540:-2",
                    "-frames:v", "1", str(png)], cwd=work, check=True)
    # Animated: just the top band, small and short, for the README gallery.
    subprocess.run([*base, "-filter_complex", vf + ",trim=start=0.25,setpts=PTS-STARTPTS,crop=1080:700:0:40,fps=12,scale=360:-2:flags=lanczos,split[x][y];"
                    "[x]palettegen=max_colors=96:stats_mode=diff[p];[y][p]paletteuse=dither=bayer:bayer_scale=4",
                    "-t", "5.5", "-loop", "0", str(gif)], cwd=work, check=True)
    return png, gif


def gallery(names: list[str]) -> None:
    f = ImageFont.truetype(str(FONTS / "DejaVuSans-Bold.ttf"), 26)
    ims = [Image.open(OUT / f"{n}.png").convert("RGB").crop((0, 0, 540, 360)) for n in names]
    pad, label = 24, 44
    sheet = Image.new("RGB", (len(ims) * (540 + pad) + pad, 360 + label + 2 * pad), "#0B1020")
    d = ImageDraw.Draw(sheet)
    for i, (n, im) in enumerate(zip(names, ims, strict=True)):
        x = pad + i * (540 + pad)
        d.text((x, pad), n.replace("_", " "), font=f, fill="#93C5FD")
        sheet.paste(im, (x, pad + label))
    sheet.save(OUT / "gallery.png", optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(EXAMPLES))
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    names = [a.only] if a.only else list(EXAMPLES)
    with tempfile.TemporaryDirectory() as tmp:
        for n in names:
            png, gif = render(n, Path(tmp))
            print(f"wrote {png.relative_to(ROOT)} ({png.stat().st_size // 1024} KB), {gif.relative_to(ROOT)} ({gif.stat().st_size // 1024} KB)")
    if not a.only:
        gallery(names)
        print("wrote assets/templates/gallery.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
