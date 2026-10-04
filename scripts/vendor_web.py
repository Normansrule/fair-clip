#!/usr/bin/env python3
"""Fetch the browser Studio's engines, check them against pinned SHA-256 hashes, and lay them out
under site/studio/vendor/ (git-ignored; the Pages workflow runs this before deploying).

* Pyodide 0.28.3 (Python 3.13 in WebAssembly) + the numpy and pydantic wheels it ships with;
* ffmpeg.wasm: @ffmpeg/ffmpeg 0.12.15 (the JS wrapper) and @ffmpeg/core 0.12.10 (single-thread
  FFmpeg with libx264 and libass; GitHub Pages can't send the COOP/COEP headers multithreading needs).

Everything is served from the site's own origin: no CDN at runtime, and the page's Content
Security Policy allows nothing else. Wheels are additionally hash-checked by Pyodide itself
against pyodide-lock.json when they load.

Run: python scripts/vendor_web.py            (cached in ~/.cache/fair-clip/vendor)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "site" / "studio" / "vendor"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fair-clip" / "vendor"

PINS = {
    "pyodide-0.28.3.tar.bz2": ("https://github.com/pyodide/pyodide/releases/download/0.28.3/pyodide-0.28.3.tar.bz2",
                               "7dcaae6d3f15986a09f0f9e6c4713a0c0f24bff0c90c74e85a81723f1bc60940"),
    "ffmpeg-0.12.15.tgz": ("https://registry.npmjs.org/@ffmpeg/ffmpeg/-/ffmpeg-0.12.15.tgz",
                           "c8a23365fb39b46d3d1d9baa2e74b522d00ce5d57e8b20471ad2665eaad38e3e"),
    "core-0.12.10.tgz": ("https://registry.npmjs.org/@ffmpeg/core/-/core-0.12.10.tgz",
                         "d00089ce82e1bdf637ddbe42e0c3d41a1ba8cf4c9e825e7fa4d0bb970e844bd4"),
}
PYODIDE_CORE = ["pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]
PY_PACKAGES = ["numpy", "pydantic"]
FFMPEG_JS = ["index.js", "classes.js", "const.js", "errors.js", "utils.js", "worker.js", "types.js"]


def fetch(name: str) -> bytes:
    url, sha = PINS[name]
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / name
    if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != sha:
        print(f"downloading {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=600) as r:  # noqa: S310 - pinned https URL, hash-checked below
            data = r.read()
        if hashlib.sha256(data).hexdigest() != sha:
            sys.exit(f"{name}: SHA-256 mismatch, refusing to use it")
        p.write_bytes(data)
    return p.read_bytes()


def write(rel: str, data: bytes) -> None:
    out = DEST / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)


def pyodide() -> None:
    blob = fetch("pyodide-0.28.3.tar.bz2")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:bz2") as t:  # streaming: one pass per read
        f = t.extractfile("pyodide/pyodide-lock.json")
        if f is None:
            sys.exit("pyodide-lock.json missing from the release")
        lock = json.loads(f.read())
    key = {k.lower().replace("_", "-"): k for k in lock["packages"]}
    need, todo = set(), list(PY_PACKAGES)
    while todo:
        pkg = key[todo.pop().lower().replace("_", "-")]
        if pkg not in need:
            need.add(pkg)
            todo += lock["packages"][pkg]["depends"]
    wheels = {lock["packages"][p]["file_name"]: lock["packages"][p]["sha256"] for p in need}
    wanted = {f"pyodide/{n}" for n in [*PYODIDE_CORE, *wheels]}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r|bz2") as t:
        for m in t:
            if m.name in wanted:
                data = t.extractfile(m).read()  # type: ignore[union-attr]
                name = m.name.removeprefix("pyodide/")
                if name in wheels and hashlib.sha256(data).hexdigest() != wheels[name]:
                    sys.exit(f"{name}: wheel hash differs from pyodide-lock.json")
                write(f"pyodide/{name}", data)
                wanted.discard(m.name)
    if wanted:
        sys.exit(f"missing from the Pyodide release: {sorted(wanted)}")
    print(f"pyodide: core + {', '.join(sorted(need))}", file=sys.stderr)


def npm(tgz: str, files: list[str], sub: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(fetch(tgz)), mode="r:gz") as t:
        for name in files:
            f = t.extractfile(f"package/dist/esm/{name}")
            if f is None:
                sys.exit(f"{tgz}: dist/esm/{name} missing")
            write(f"{sub}/{name}", f.read())
        lic = t.extractfile("package/LICENSE") if "package/LICENSE" in t.getnames() else None
        if lic:
            write(f"{sub}/LICENSE", lic.read())


def main() -> None:
    if DEST.exists():
        shutil.rmtree(DEST)
    pyodide()
    npm("ffmpeg-0.12.15.tgz", FFMPEG_JS, "ffmpeg")
    npm("core-0.12.10.tgz", ["ffmpeg-core.js", "ffmpeg-core.wasm"], "ffmpeg-core")
    total = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    print(f"vendored into {DEST} ({total / 1e6:.0f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
