"""Small helpers with no heavy imports (they also run in the browser build, under Pyodide)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
