#!/usr/bin/env python3
"""Assemble the browser Studio into site/studio/ (git-ignored; built by the Pages workflow).

    site/studio/
      index.html, studio.js, studio.css, backend-*.js, zip.js, icon.png   <- src/fairclip/web/static/studio
      fairclip.zip       the fair-clip Python package, run in the page by Pyodide
      fonts/             the fonts the captions and cards are drawn with (+ licenses)
      example/           the CC0 example video, its captions and the example takes
      vendor/            Pyodide + ffmpeg.wasm (scripts/vendor_web.py, pinned and hash-checked)

Standard library only, so CI needs no installs. Run: python scripts/build_studio.py
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "fairclip"
UI = PKG / "web" / "static" / "studio"
OUT = ROOT / "site" / "studio"
EXAMPLE = ROOT / "examples" / "studio"
SKIP_DIRS = {"web", "__pycache__"}  # the dashboard isn't needed in the browser


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in UI.iterdir():
        if f.is_file():
            shutil.copy2(f, OUT / f.name)
    with zipfile.ZipFile(OUT / "fairclip.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(PKG.rglob("*.py")):
            rel = f.relative_to(PKG)
            if not SKIP_DIRS & set(rel.parts[:-1]):
                z.write(f, f"fairclip/{rel.as_posix()}")
        z.write(PKG / "data" / "screen_terms.txt", "fairclip/data/screen_terms.txt")
    fonts = OUT / "fonts"
    if fonts.exists():
        shutil.rmtree(fonts)
    fonts.mkdir()
    names = []
    for f in sorted((PKG / "data" / "fonts").iterdir()):
        shutil.copy2(f, fonts / f.name)
        if f.suffix == ".ttf":
            names.append(f.name)
    (fonts / "fonts.json").write_text(json.dumps(names))
    ex = OUT / "example"
    ex.mkdir(exist_ok=True)
    for name in ("source.mp4", "captions.srt", "project.json", "LICENSE-EXAMPLE.txt"):
        shutil.copy2(EXAMPLE / name, ex / name)
    if not (OUT / "vendor" / "ffmpeg-core" / "ffmpeg-core.wasm").exists():
        print("note: run scripts/vendor_web.py too (Pyodide + ffmpeg.wasm)", file=sys.stderr)
    print(f"built {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
