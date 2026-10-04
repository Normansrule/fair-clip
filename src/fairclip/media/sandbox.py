"""Run FFmpeg/FFprobe where a malicious media file can't hurt the host.

Media parsers are a classic attack surface: a crafted video can exploit a decoder bug. So by
default every FFmpeg call runs in a throwaway Docker container with:

* no network (``--network none``), a read-only root filesystem, all Linux capabilities
  dropped, ``no-new-privileges``, a non-root user;
* memory, CPU and process-count limits plus a wall-clock timeout;
* the input folder mounted read-only and exactly one writable output folder;
* FFmpeg itself restricted to the ``file`` protocol, so a file can't make it fetch URLs.

The *only* exception: files whose SHA-256 is in the trust manifest (the project's own sample
footage, pinned in ``examples/demo/SHA256SUMS``, and outputs derived purely from trusted files)
may run on the host when Docker is unavailable. Anything else with no Docker is a hard stop.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..util import sha256_file

Tool = Literal["ffmpeg", "ffprobe"]
FONTS_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"


class SandboxUnavailable(RuntimeError):
    pass


class MediaToolError(RuntimeError):
    pass


@dataclass
class TrustManifest:
    """SHA-256 hashes of media we created ourselves (never user-supplied)."""

    path: Path

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        return set(json.loads(self.path.read_text()))

    def contains(self, sha: str) -> bool:
        return sha in self._load()

    def add(self, shas: Iterable[str]) -> None:
        s = self._load() | set(shas)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(s)))

    def seed_from_sums(self, sums_file: Path) -> None:
        if sums_file.exists():
            self.add(line.split()[0] for line in sums_file.read_text().splitlines() if line.strip())


def docker_available(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _translate(args: Sequence[str], in_dir: Path, out_dir: Path, container: bool, fonts: str | None = None) -> list[str]:
    """Replace @in/ @out/ @fonts/ tokens with container or host paths."""
    m = {
        "@in/": "/in/" if container else f"{in_dir}/",
        "@out/": "/out/" if container else f"{out_dir}/",
        "@fonts/": fonts or ("/fonts/" if container else f"{FONTS_DIR}/"),
    }
    out = []
    for a in args:
        for k, v in m.items():
            a = a.replace(k, v)
        out.append(a)
    return out


class MediaRunner:
    def __init__(self, *, image: str, mode: Literal["docker", "auto", "worker"], trust: TrustManifest,
                 memory: str = "2g", cpus: str = "2", pids: int = 256, timeout_s: int = 900) -> None:
        self.image, self.mode, self.trust = image, mode, trust
        self.memory, self.cpus, self.pids, self.timeout_s = memory, cpus, pids, timeout_s
        self._docker = mode != "worker" and docker_available(image)
        self.last_backend: str = ""

    def backend_for(self, inputs: Sequence[Path]) -> str:
        if self.mode == "worker":
            return "worker"
        if self._docker:
            return "docker"
        if self.mode == "docker":
            raise SandboxUnavailable(
                f"Docker image {self.image} not found. Build it with `make sandbox` (see README)."
            )
        untrusted = [p.name for p in inputs if not self.trust.contains(sha256_file(p))]
        if untrusted:
            raise SandboxUnavailable(
                "refusing to process untrusted media on the host without the Docker sandbox: "
                + ", ".join(untrusted) + ". Build the sandbox with `make sandbox`."
            )
        return "local-trusted"

    def run(self, tool: Tool, args: Sequence[str], *, in_dir: Path, out_dir: Path,
            inputs: Sequence[Path], capture: bool = True) -> subprocess.CompletedProcess[str]:
        if tool not in ("ffmpeg", "ffprobe"):
            raise ValueError("only ffmpeg/ffprobe may run in the media sandbox")
        backend = self.backend_for(inputs)
        self.last_backend = backend
        out_dir.mkdir(parents=True, exist_ok=True)
        if backend == "worker":
            return self._run_worker(tool, args, in_dir, out_dir)
        base = [tool, "-hide_banner", "-nostdin"] if tool == "ffmpeg" else [tool, "-hide_banner"]
        if backend == "docker":
            cmd = [
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", str(self.pids), "--memory", self.memory, "--memory-swap", self.memory,
                "--cpus", self.cpus, "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",  # noqa: S108 - tmpfs inside the container
                "-v", f"{in_dir.resolve()}:/in:ro", "-v", f"{out_dir.resolve()}:/out:rw",
                "--entrypoint", tool, self.image,
                *base[1:], *_translate(args, in_dir, out_dir, container=True),
            ]
            preexec = None
        else:
            cmd = [shutil.which(tool) or tool, *base[1:], *_translate(args, in_dir, out_dir, container=False)]
            preexec = _limit_local
        try:
            r = subprocess.run(
                cmd, capture_output=capture, text=True, timeout=self.timeout_s, check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, preexec_fn=preexec,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaToolError(f"{tool} timed out after {self.timeout_s}s") from exc
        if r.returncode != 0:
            tail = (r.stderr or "")[-1500:]
            raise MediaToolError(f"{tool} failed ({r.returncode}) via {backend}: {tail}")
        if backend == "local-trusted":
            # Outputs made only from trusted inputs are trusted too.
            self.trust.add(sha256_file(p) for p in out_dir.iterdir() if p.is_file())
        return r


    def _run_worker(self, tool: Tool, args: Sequence[str], in_dir: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
        """Hand the job to the isolated media-worker container through the shared /jobs volume."""
        import time

        job_dir = in_dir.parent
        if in_dir.parent != out_dir.parent:
            raise ValueError("worker jobs need in/ and out/ in the same job folder")
        if (job_dir / "job.json").exists():
            raise ValueError("one job per job folder: create a new one with job_dirs()")
        out_dir.chmod(0o777)  # the worker runs as a different unprivileged user
        job_dir.chmod(0o777)  # ...and writes result.json next to job.json
        argv = _translate(args, in_dir, out_dir, container=False, fonts="/fonts/")
        tmp = job_dir / "job.json.tmp"
        tmp.write_text(json.dumps({"tool": tool, "args": argv}))
        tmp.replace(job_dir / "job.json")  # atomic: the worker never sees half a job
        deadline = time.monotonic() + self.timeout_s + 30
        result = job_dir / "result.json"
        while not result.exists():
            if time.monotonic() > deadline:
                raise MediaToolError(f"media worker did not answer within {self.timeout_s}s (is the media service running?)")
            time.sleep(0.1)
        res = json.loads(result.read_text())
        if res["returncode"] != 0:
            raise MediaToolError(f"{tool} failed ({res['returncode']}) via worker: {res['stderr'][-1500:]}")
        return subprocess.CompletedProcess([tool, *argv], 0, res["stdout"], res["stderr"])


def _limit_local() -> None:  # pragma: no cover - runs in the child process
    import resource  # POSIX-only; imported here so the module also loads in the browser build

    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (1800, 1800))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**3, 4 * 1024**3))
