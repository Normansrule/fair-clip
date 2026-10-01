"""Signal extraction that needs a decoder (so it runs in the sandbox), then pure-NumPy math."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .sandbox import MediaRunner

SR = 16_000  # audio sample rate for analysis, Hz
WIN = 0.05  # analysis window, s


def job_dirs(work_root: Path, inputs: list[Path]) -> tuple[Path, Path, list[Path]]:
    """Make an isolated job folder: in/ holds links to exactly the inputs, out/ is empty."""
    job = work_root / f"job-{uuid.uuid4().hex[:12]}"
    in_dir, out_dir = job / "in", job / "out"
    in_dir.mkdir(parents=True)
    out_dir.mkdir()
    staged = []
    for i, p in enumerate(inputs):
        dst = in_dir / f"input{i}{p.suffix.lower()}"
        try:
            os.link(p, dst)
        except OSError:
            shutil.copyfile(p, dst)
        staged.append(dst)
    return in_dir, out_dir, staged


@dataclass
class AudioSignal:
    rms_db: np.ndarray  # one value per WIN seconds, dBFS
    win: float = WIN

    def slice(self, start: float, end: float) -> np.ndarray:
        a, b = int(start / self.win), max(int(end / self.win), int(start / self.win) + 1)
        return self.rms_db[a:b]


def rms_db_from_pcm(pcm: np.ndarray, sr: int = SR, win: float = WIN) -> np.ndarray:
    """Root-mean-square level per window in dBFS (0 dBFS = full-scale sine peak... ~ -3 dB RMS)."""
    x = pcm.astype(np.float64) / 32768.0
    n = int(sr * win)
    frames = len(x) // n
    if frames == 0:
        return np.array([-120.0])
    x = x[: frames * n].reshape(frames, n)
    rms = np.sqrt(np.mean(x * x, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-6))


def extract_audio(runner: MediaRunner, media: Path, work: Path) -> AudioSignal:
    in_dir, out_dir, (src,) = job_dirs(work, [media])
    runner.run("ffmpeg", ["-protocol_whitelist", "file", "-i", f"@in/{src.name}", "-vn", "-ac", "1",
                          "-ar", str(SR), "-f", "s16le", "-acodec", "pcm_s16le", "@out/audio.raw"],
               in_dir=in_dir, out_dir=out_dir, inputs=[src])
    pcm = np.fromfile(out_dir / "audio.raw", dtype="<i2")
    return AudioSignal(rms_db_from_pcm(pcm))


_PTS = re.compile(r"pts_time:([0-9.]+)")


def scene_cuts(runner: MediaRunner, media: Path, work: Path, threshold: float = 0.3) -> list[float]:
    """Times (s) where the picture changes sharply. Same idea as PySceneDetect's content detector."""
    in_dir, out_dir, (src,) = job_dirs(work, [media])
    r = runner.run("ffmpeg", ["-protocol_whitelist", "file", "-i", f"@in/{src.name}", "-an",
                              "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
                              "-f", "null", "-"],
                   in_dir=in_dir, out_dir=out_dir, inputs=[src])
    return [float(t) for t in _PTS.findall(r.stderr or "")]


def motion_centroid_x(runner: MediaRunner, media: Path, work: Path, start: float, end: float) -> float:
    """Horizontal centre (0..1) of on-screen motion: a cheap 'where is the speaker' estimate.

    Frames are decoded tiny (64x36 grey, 4 fps) inside the sandbox; we then average the absolute
    frame-to-frame difference per column and take its centre of mass.
    """
    in_dir, out_dir, (src,) = job_dirs(work, [media])
    runner.run("ffmpeg", ["-protocol_whitelist", "file", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                          "-i", f"@in/{src.name}", "-an", "-vf", "fps=4,scale=64:36,format=gray",
                          "-f", "rawvideo", "@out/frames.gray"],
               in_dir=in_dir, out_dir=out_dir, inputs=[src])
    raw = np.fromfile(out_dir / "frames.gray", dtype=np.uint8)
    n = raw.size // (64 * 36)
    if n < 2:
        return 0.5
    f = raw[: n * 64 * 36].reshape(n, 36, 64).astype(np.float32)
    col = np.abs(np.diff(f, axis=0)).mean(axis=(0, 1))
    if col.sum() < 1e-3:
        return 0.5
    return float((col * (np.arange(64) + 0.5)).sum() / col.sum() / 64)


def frame_dhashes(runner: MediaRunner, media: Path, work: Path, n: int = 8) -> list[int]:
    """64-bit difference hashes of n evenly spaced frames (for near-duplicate detection)."""
    in_dir, out_dir, (src,) = job_dirs(work, [media])
    runner.run("ffmpeg", ["-protocol_whitelist", "file", "-i", f"@in/{src.name}", "-an",
                          "-vf", "fps=1,scale=9:8,format=gray", "-f", "rawvideo", "@out/h.gray"],
               in_dir=in_dir, out_dir=out_dir, inputs=[src])
    raw = np.fromfile(out_dir / "h.gray", dtype=np.uint8)
    k = raw.size // 72
    if k == 0:
        return []
    frames = raw[: k * 72].reshape(k, 8, 9).astype(np.int16)
    idx = np.linspace(0, k - 1, num=min(n, k)).round().astype(int)
    return [dhash(frames[i]) for i in idx]


def dhash(gray_8x9: np.ndarray) -> int:
    bits = (gray_8x9[:, 1:] > gray_8x9[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
