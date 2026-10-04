"""Stage 4: turn a clip plan into a finished MP4 (inside the sandbox)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..models import ClipPlan, ReframeMode
from .analyze import job_dirs
from .sandbox import MediaRunner
from .timeline import Interval, TimelineMap

# Encoding presets. "high" (the app): CRF 18, x264 "medium", 2-second keyframes, High profile;
# visually transparent for Shorts and well inside YouTube's recommended upload settings.
# "fast" (the browser build, where x264 runs in WebAssembly): same look, quicker preset.
ENCODE = {
    "high": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-r", "30", "-g", "60", "-bf", "2"],
    "fast": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-r", "30", "-g", "60"],
}
ENCODE_V = ENCODE["high"]
ENCODE_A = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
PUNCH_IN = 1.07  # every other jump-cut segment is pushed in 7 %: the "edited" look, no new pixels invented
PUNCH_IN_FIT = 1.04  # gentler when the whole frame is shown (slides keep their edges)
GRADE = "eq=contrast=1.04:saturation=1.06,unsharp=5:5:0.35:5:5:0"


@dataclass
class RenderResult:
    clip: Path
    preview: Path
    duration: float
    removed_s: float
    backend: str


def _vertical(label_in: str, label_out: str, plan: ClipPlan, crop_cx: float, z: float) -> list[str]:
    W, H = 1080, 1920
    if plan.reframe is ReframeMode.FIT:
        fg_w = int(round(W * z / 2) * 2)
        return [
            # Blur a quarter-size copy, then upscale: same look, ~10x cheaper than blurring 1080x1920.
            f"[{label_in}]split[{label_in}b][{label_in}f];"
            f"[{label_in}b]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=8:2,eq=brightness=-0.18,scale={W}:{H}[{label_in}bg];[{label_in}f]scale={fg_w}:-2[{label_in}fg];"
            f"[{label_in}bg][{label_in}fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{label_out}]"
        ]
    # smart crop around the detected speaker position; a smaller window = punch-in
    return [f"[{label_in}]crop=ih*9/16/{z:.4f}:ih/{z:.4f}:(iw-ow)*{crop_cx:.4f}:(ih-oh)/2,scale={W}:{H},setsar=1[{label_out}]"]


def _horizontal(label_in: str, label_out: str, z: float) -> list[str]:
    tail = f",scale={int(round(1920 * z / 2) * 2)}:-2,crop=1920:1080" if z > 1.0 else ""
    return [f"[{label_in}]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2{tail},setsar=1[{label_out}]"]


def filtergraph(intervals: list[Interval], seek0: float, plan: ClipPlan, crop_cx: float, ass_name: str) -> str:
    """Each kept segment is framed on its own (so alternate segments can punch in), then the
    segments are joined, graded, captioned (ASS) and the audio is cleaned and loudness-normalised."""
    parts = []
    n = len(intervals)
    duration = sum(iv.dur for iv in intervals)
    zoom = PUNCH_IN_FIT if plan.reframe is ReframeMode.FIT else PUNCH_IN
    for k, iv in enumerate(intervals):
        a, b = iv.a - seek0, iv.b - seek0
        z = zoom if (plan.punch_in and n > 1 and k % 2 == 1) else 1.0
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[s{k}]")
        parts += _vertical(f"s{k}", f"v{k}", plan, crop_cx, z) if plan.aspect == "9:16" else _horizontal(f"s{k}", f"v{k}", z)
        fade_out = max(0.0, iv.dur - 0.01)
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.01,afade=t=out:st={fade_out:.3f}:d=0.01[a{k}]")
    parts.append("".join(f"[v{k}][a{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]{GRADE}[vr]")
    parts.append(f"[vr]subtitles=filename=@in/{ass_name}:fontsdir=@fonts/[vout]")
    # Speech chain: cut rumble, even out levels, then normalise to -14 LUFS (where YouTube plays
    # most music and speech), with a soft fade at both ends.
    end_fade = max(0.0, duration - 0.12)
    parts.append("[ac]highpass=f=70,acompressor=threshold=-20dB:ratio=2.5:attack=10:release=120:makeup=1.5,"
                 f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:d=0.05,afade=t=out:st={end_fade:.3f}:d=0.12[aout]")
    return ";".join(parts)


def clip_args(plan: ClipPlan, tmap: TimelineMap, *, src_name: str, ass_name: str = "overlay.ass", crop_cx: float = 0.5,
              quality: str = "high", out_name: str = "clip.mp4") -> list[str]:
    """FFmpeg arguments for one clip, with @in/ @out/ @fonts/ placeholders. The app's sandbox and the
    browser build (ffmpeg.wasm) each map them to their own folders; the arguments are identical."""
    seek0 = max(0.0, plan.start - 1.0)
    graph = filtergraph(tmap.iv, seek0, plan, crop_center(crop_cx), ass_name)
    return ["-ss", f"{seek0:.3f}", "-to", f"{plan.end + 0.5:.3f}", "-i", f"@in/{src_name}",
            "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]", *encode_args(quality), "-y", f"@out/{out_name}"]


def encode_args(quality: str = "high") -> list[str]:
    return [*ENCODE[quality], *ENCODE_A, "-movflags", "+faststart", "-map_metadata", "-1"]


def crop_center(motion_cx: float) -> float:
    """Map the motion centroid (0..1 of frame width) to crop offset (0..1 of free width)."""
    # For a 16:9 frame and a 9:16 crop, the crop covers 0.316 of the width; centre it on the
    # speaker, clamped so the crop window stays inside the frame.
    frac = (9 / 16) / (16 / 9)
    left = min(max(motion_cx - frac / 2, 0.0), 1.0 - frac)
    return left / (1.0 - frac)


def render(runner: MediaRunner, work: Path, source: Path, plan: ClipPlan, tmap: TimelineMap,
           ass_text: str, crop_cx: float = 0.5, quality: str = "high") -> RenderResult:
    in_dir, out_dir, (src,) = job_dirs(work, [source])
    ass = in_dir / "overlay.ass"
    ass.write_text(ass_text, "utf-8")
    (in_dir / "cutlist.json").write_text(json.dumps([[i.a, i.b] for i in tmap.iv]))
    runner.run("ffmpeg", ["-protocol_whitelist", "file",
                          *clip_args(plan, tmap, src_name=src.name, ass_name=ass.name, crop_cx=crop_cx, quality=quality)],
               in_dir=in_dir, out_dir=out_dir, inputs=[src])  # the .ass file is our own text, not media
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
