"""Media worker for the all-in-Docker deployment (docker-compose.yml). Standard library only.

Runs inside the `media` container, which has no network, a read-only root filesystem, no Linux
capabilities and tight resource limits. The app container never gets the Docker socket (which
would be root on the host); instead the two containers share one volume:

    /jobs/<id>/in/       inputs (written by the app)
    /jobs/<id>/job.json  {"tool": "ffmpeg"|"ffprobe", "args": [...]}   (written last = ready)
    /jobs/<id>/out/      outputs (written here)
    /jobs/<id>/result.json {"returncode", "stdout", "stderr"}         (written by the worker)

Only ffmpeg/ffprobe can run, argv is validated, each job has a timeout and memory/CPU limits.
"""

from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

JOBS = Path(os.environ.get("FAIRCLIP_JOBS", "/jobs"))
TOOLS = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}
TIMEOUT = int(os.environ.get("FAIRCLIP_JOB_TIMEOUT", "900"))
FORBIDDEN = ("http:", "https:", "tcp:", "udp:", "rtmp", "rtsp", "ftp:", "concat:", "subfile:", "data:", "pipe:", "unix:",
             "async:", "cache:", "crypto:", "gopher", "hls:", "srt:", "tls:", "sftp:", "smb:")
# Every absolute path anywhere in an argument, including inside filter options ("movie=/x",
# "subtitles=filename=/x:fontsdir=/y") and protocol prefixes ("file:/x").
_PATHS = re.compile(r"(?:^|[=:,'\s;\[])(/[^:,;'\s\[\]]*)")


def valid(job: dict, job_dir: Path) -> list[str] | None:
    tool, args = job.get("tool"), job.get("args")
    if tool not in TOOLS or not isinstance(args, list) or len(args) > 400 or not all(isinstance(a, str) and len(a) < 20000 for a in args):
        return None
    allowed = (str(job_dir) + "/", "/fonts/")
    for a in args:
        low = a.lower()
        if low.startswith(FORBIDDEN) or any(f"={p}" in low or f":{p}" in low.replace("file:", "") for p in FORBIDDEN if p != "file:"):
            return None
        if ".." in a:
            return None
        for path in _PATHS.findall(a):
            if not path.startswith(allowed):
                return None  # absolute paths must stay inside this job (or the bundled fonts)
    return [TOOLS[tool], "-hide_banner", *(["-nostdin"] if tool == "ffmpeg" else []), *args]


def limits() -> None:  # pragma: no cover - child process
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT * 2, TIMEOUT * 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**3, 4 * 1024**3))


def run_job(job_dir: Path) -> None:
    result_path = job_dir / "result.json"
    try:
        job = json.loads((job_dir / "job.json").read_text())
    except (OSError, json.JSONDecodeError):
        result_path.write_text(json.dumps({"returncode": 97, "stdout": "", "stderr": "unreadable job"}))
        return
    cmd = valid(job, job_dir)
    if cmd is None:
        result_path.write_text(json.dumps({"returncode": 98, "stdout": "", "stderr": "job rejected by worker policy"}))
        return
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False, cwd=job_dir,
                           env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, preexec_fn=limits)
        out = {"returncode": r.returncode, "stdout": r.stdout[-2_000_000:], "stderr": r.stderr[-200_000:]}
    except subprocess.TimeoutExpired:
        out = {"returncode": 124, "stdout": "", "stderr": f"timed out after {TIMEOUT}s"}
    tmp = job_dir / "result.json.tmp"
    tmp.write_text(json.dumps(out))
    tmp.replace(result_path)


def main() -> int:  # pragma: no cover - long-running service
    print(f"fair-clip media worker watching {JOBS}", flush=True)
    while True:
        for job_dir in sorted(JOBS.glob("job-*")):
            if (job_dir / "job.json").exists() and not (job_dir / "result.json").exists():
                run_job(job_dir)
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
