"""File-type validation before any decoder sees the file.

Two layers:
1. Magic bytes on the host (pure Python, no decoder): only MP4/MOV, Matroska/WebM and WAV
   containers pass. Playlists (M3U8), concat lists, images and anything else are refused.
2. FFprobe inside the sandbox, whose JSON output is then checked against codec, duration and
   resolution allowlists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .sandbox import MediaRunner

ALLOWED_VIDEO_CODECS = {"h264", "hevc", "vp9", "av1", "mpeg4", "prores"}
ALLOWED_AUDIO_CODECS = {"aac", "opus", "mp3", "pcm_s16le", "pcm_s24le", "flac", "vorbis"}
ALLOWED_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".wav"}


class MediaRejected(ValueError):
    pass


def sniff_container(path: Path) -> str:
    """Identify the container from its first bytes. Raises MediaRejected for anything else."""
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise MediaRejected(f"file extension {path.suffix!r} is not on the allowlist")
    with path.open("rb") as fh:
        head = fh.read(16)
    if len(head) < 12:
        raise MediaRejected("file too small to be a video")
    if head[4:8] == b"ftyp":
        return "isobmff"  # MP4 / MOV / M4V
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"  # MKV / WebM
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    raise MediaRejected("unrecognised container signature (not MP4/MOV/MKV/WebM/WAV)")


@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str | None
    acodec: str | None
    container: str

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def probe(runner: MediaRunner, path: Path, *, work: Path, max_bytes: int, max_duration: float,
          max_w: int, max_h: int) -> ProbeResult:
    from .analyze import job_dirs

    size = path.stat().st_size
    if size > max_bytes:
        raise MediaRejected(f"file is {size} bytes; limit is {max_bytes}")
    container = sniff_container(path)
    in_dir, out_dir, (src,) = job_dirs(work, [path])
    r = runner.run(
        "ffprobe",
        ["-v", "error", "-protocol_whitelist", "file", "-probesize", "50M", "-analyzeduration", "50M",
         "-print_format", "json", "-show_format", "-show_streams", f"@in/{src.name}"],
        in_dir=in_dir, out_dir=out_dir, inputs=[src],
    )
    return parse_probe(r.stdout, container, max_duration=max_duration, max_w=max_w, max_h=max_h)


def parse_probe(stdout: str, container: str, *, max_duration: float, max_w: int, max_h: int) -> ProbeResult:
    if len(stdout) > 2_000_000:
        raise MediaRejected("ffprobe output unreasonably large")
    info = json.loads(stdout)
    streams = info.get("streams", [])
    if len(streams) > 16:
        raise MediaRejected("too many streams")
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if a is None:
        raise MediaRejected("no audio stream: highlight detection needs speech")
    if a.get("codec_name") not in ALLOWED_AUDIO_CODECS:
        raise MediaRejected(f"audio codec {a.get('codec_name')!r} not allowed")
    if v is not None and v.get("codec_name") not in ALLOWED_VIDEO_CODECS:
        raise MediaRejected(f"video codec {v.get('codec_name')!r} not allowed")
    dur = float(info.get("format", {}).get("duration") or 0)
    if not 1.0 <= dur <= max_duration:
        raise MediaRejected(f"duration {dur:.1f}s outside 1s..{max_duration:.0f}s")
    w = int(v.get("width", 0)) if v else 0
    h = int(v.get("height", 0)) if v else 0
    if v and not (16 <= w <= max_w and 16 <= h <= max_h):
        raise MediaRejected(f"resolution {w}x{h} outside limits")
    fps = 0.0
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    return ProbeResult(dur, w, h, fps, v.get("codec_name") if v else None, a.get("codec_name"), container)
