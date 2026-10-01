"""Stage 4: turn a clip plan into a finished MP4 (inside the sandbox)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..models import ClipPlan, ReframeMode
from .analyze import job_dirs
from .sandbox import MediaRunner
from .timeline import Interval, TimelineMap

ENCODE_V = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", "30"]
ENCODE_A = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]


@dataclass
class RenderResult:
    clip: Path
    preview: Path
    duration: float
    removed_s: float
    backend: str


def filtergraph(intervals: list[Interval], seek0: float, plan: ClipPlan, crop_cx: float, ass_name: str) -> str:
    parts = []
    n = len(intervals)
    for k, iv in enumerate(intervals):
        a, b = iv.a - seek0, iv.b - seek0
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{k}]")
        fade_out = max(0.0, iv.dur - 0.01)
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.01,afade=t=out:st={fade_out:.3f}:d=0.01[a{k}]")
    parts.append("".join(f"[v{k}][a{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    if plan.aspect == "9:16":
        W, H = 1080, 1920
        if plan.reframe is ReframeMode.FIT:
            parts.append(
                # Blur a quarter-size copy, then upscale: same look, ~10x cheaper than blurring 1080x1920.
                f"[vc]split[bgs][fgs];[bgs]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
                f"boxblur=8:2,eq=brightness=-0.18,scale={W}:{H}[bg];[fgs]scale={W}:-2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vr]"
            )
        else:  # smart crop around the detected speaker position
            parts.append(f"[vc]crop=ih*9/16:ih:(iw-ow)*{crop_cx:.4f}:0,scale={W}:{H},setsar=1[vr]")
    else:
        parts.append("[vc]scale=1920:1080:force_original_aspect_ratio=decrease,"
                     "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[vr]")
    parts.append(f"[vr]subtitles=filename=@in/{ass_name}:fontsdir=@fonts/[vout]")
    parts.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    return ";".join(parts)


def crop_center(motion_cx: float) -> float:
    """Map the motion centroid (0..1 of frame width) to crop offset (0..1 of free width)."""
    # For a 16:9 frame and a 9:16 crop, the crop covers 0.316 of the width; centre it on the
    # speaker, clamped so the crop window stays inside the frame.
    frac = (9 / 16) / (16 / 9)
    left = min(max(motion_cx - frac / 2, 0.0), 1.0 - frac)
    return left / (1.0 - frac)


def render(runner: MediaRunner, work: Path, source: Path, plan: ClipPlan, tmap: TimelineMap,
           ass_text: str, crop_cx: float = 0.5) -> RenderResult:
    in_dir, out_dir, (src,) = job_dirs(work, [source])
    ass = in_dir / "overlay.ass"
    ass.write_text(ass_text, "utf-8")
    (in_dir / "cutlist.json").write_text(json.dumps([[i.a, i.b] for i in tmap.iv]))
    seek0 = max(0.0, plan.start - 1.0)
    graph = filtergraph(tmap.iv, seek0, plan, crop_center(crop_cx), ass.name)
    runner.run("ffmpeg", [
        "-protocol_whitelist", "file", "-ss", f"{seek0:.3f}", "-to", f"{plan.end + 0.5:.3f}",
        "-i", f"@in/{src.name}", "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
        *ENCODE_V, *ENCODE_A, "-movflags", "+faststart", "-map_metadata", "-1", "-y", "@out/clip.mp4",
    ], in_dir=in_dir, out_dir=out_dir, inputs=[src])  # the .ass file is our own text, not media
    # "Before" preview for the side-by-side review: the raw source segment, untouched.
    clip_out = out_dir / "clip.mp4"
    in_dir, out_dir, (src,) = job_dirs(work, [source])  # one job per folder (worker protocol)
    runner.run("ffmpeg", [
        "-protocol_whitelist", "file", "-ss", f"{plan.start:.3f}", "-to", f"{plan.end:.3f}",
        "-i", f"@in/{src.name}", "-vf", "scale=640:-2", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        "-map_metadata", "-1", "-y", "@out/before.mp4",
    ], in_dir=in_dir, out_dir=out_dir, inputs=[src])
    return RenderResult(clip_out, out_dir / "before.mp4", tmap.duration,
                        (plan.end - plan.start) - tmap.duration, runner.last_backend)


def extract_frame(runner: MediaRunner, work: Path, media: Path, t: float, name: str = "frame.png") -> Path:
    in_dir, out_dir, (src,) = job_dirs(work, [media])
    runner.run("ffmpeg", ["-protocol_whitelist", "file", "-ss", f"{t:.3f}", "-i", f"@in/{src.name}",
                          "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", "-y", f"@out/{name}"],
               in_dir=in_dir, out_dir=out_dir, inputs=[src])
    return out_dir / name
