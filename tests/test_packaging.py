"""Files the app needs at runtime must be tracked by git (a broad .gitignore rule once hid them)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NEEDED = [
    "src/fairclip/data/screen_terms.txt",
    "src/fairclip/data/fonts/Inter-Bold.ttf",
    "src/fairclip/data/fonts/InterDisplay-Black.ttf",
    "src/fairclip/data/fonts/DejaVuSans.ttf",
    "src/fairclip/data/fonts/LICENSE-Inter.txt",
    "src/fairclip/web/static/studio/index.html",
    "src/fairclip/web/static/studio/studio.js",
    "src/fairclip/web/static/studio/backend-web.js",
    "src/fairclip/web/static/studio/backend-app.js",
    "site/data/demo.json",
    "site/data/studio-example.json",
    "examples/studio/source.mp4",
    "examples/studio/captions.srt",
    "examples/studio/project.json",
    "examples/studio/SHA256SUMS",
]


@pytest.mark.skipif(not (ROOT / ".git").exists() or not shutil.which("git"), reason="needs the git checkout")
def test_runtime_files_are_tracked():
    tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines())
    ignored = subprocess.run(["git", "check-ignore", *NEEDED], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not ignored, f".gitignore hides runtime files: {ignored}"
    missing = [f for f in NEEDED if f not in tracked and (ROOT / f).exists()]
    assert not missing, f"not added to git: {missing}"


def test_package_data_covers_the_studio():
    text = (ROOT / "pyproject.toml").read_text()
    assert '"web/static/studio/*"' in text and '"data/fonts/*"' in text


def test_example_checksums_match():
    import hashlib

    for line in (ROOT / "examples" / "studio" / "SHA256SUMS").read_text().splitlines():
        sha, name = line.split()
        assert hashlib.sha256((ROOT / "examples" / "studio" / name).read_bytes()).hexdigest() == sha, name
