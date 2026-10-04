"""The Studio on the app side: real FFmpeg in the media sandbox, the same `studio` core as the
browser build. Used by the dashboard's /studio page and by `fairclip studio` on the command line.

    session = analyze(video, captions, brand, rights_choice, runner=..., work=...)
    # the owner writes a take (and optionally a title) for each suggested moment
    made = [make_clip(session, cand, take, title, n, runner=..., work=...) for ...]
    write_package(made, session.brand, out_dir)  # mp4s + copy-paste texts + one zip

Nothing here publishes anything. The download is the hand-off: the owner uploads to YouTube
themselves (or approves it in the dashboard's publish flow).
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import studio
from .captions import CaptionsError, parse_captions
from .config import Settings
from .media import analyze, probe
from .media.sandbox import MediaRunner
from .models import Candidate, RightsRecord, Transcript
from .util import sha256_file

ZIP_NAME = "fair-clip-clips.zip"


class StudioError(ValueError):
    """Something the owner can fix (shown to them as is)."""


@dataclass
class Session:
    media: Path
    sha: str
    rights: RightsRecord
    tr: Transcript
    rms: np.ndarray
    cuts: list[float]
    brand: studio.Brand
    candidates: list[Candidate]
    settings: Settings
    suggestions: list[dict] = field(default_factory=list)


@dataclass
class GateInputs:
    """The owner's own gate configuration (the app's settings, screen terms, kill switch, history
    and affiliate registry), so a Studio clip is judged exactly like a pipeline clip."""

    screen_terms: dict[str, list[str]] | None = None
    kill_switch: bool = False
    registry: Any = None
    history: Callable[[Any], Any] | None = None  # plan -> History


def gate_inputs(pipeline: Any) -> GateInputs:
    """The owner's gate configuration from the app's Pipeline (store, settings, data folder)."""
    from .affiliate import load_registry
    from .gate import load_screen_terms

    def history(plan: Any) -> Any:
        h = pipeline.history(plan, "")
        h.today_count = h.today_same_source = 0  # a download isn't a publish: the daily cap applies when it's posted
        return h

    return GateInputs(screen_terms=load_screen_terms(pipeline.screen_terms_path()), kill_switch=pipeline.store.kill_switch,
                      registry=load_registry(pipeline.store), history=history)


def owner_settings(settings: Settings, brand: studio.Brand) -> Settings:
    """The owner's own settings (gate thresholds, monetization, media limits); only the brand differs."""
    st = settings.model_copy(deep=True)
    st.channel.name, st.channel.topics = brand.channel, list(brand.topics)
    return st


@dataclass
class Made:
    finished: studio.Finished
    clip: Path | None  # None when the gate blocked it (nothing to download)
    poster: Path | None = None


def analyze_source(media: Path, captions: str | None, brand: studio.Brand, choice: studio.RightsChoice, *,
                   runner: MediaRunner, work: Path, settings: Settings | None = None,
                   progress: Callable[[str], None] = lambda _m: None) -> Session:
    """Step 1: check rights, probe the file, read what was said, find the best moments."""
    settings = settings or studio.studio_settings(brand)
    probe.sniff_container(media)  # magic bytes on the host, before any decoder sees the file
    sha = sha256_file(media)
    rec = studio.rights_record(choice, sha)
    if problems := studio.rights_check(rec, settings.channel.monetized):
        raise StudioError("rights: " + "; ".join(problems))
    progress("checking the video")
    m = settings.media
    probe.probe(runner, media, work=work, max_bytes=m.max_input_bytes, max_duration=m.max_duration_s,
                max_w=m.max_width, max_h=m.max_height)
    progress("measuring the audio")
    audio = analyze.extract_audio(runner, media, work)
    if captions and captions.strip():
        try:
            tr = parse_captions(captions)
        except CaptionsError as exc:
            raise StudioError(f"captions: {exc}") from exc
    else:
        progress("transcribing (no captions file given)")
        tr = _transcribe(runner, media, work)
    progress("looking for the best moments")
    cuts = analyze.scene_cuts(runner, media, work)
    cands = studio.find_clips(f"src-{sha[:12]}", tr, audio.rms_db, cuts, brand)
    if not cands:
        raise StudioError("no clip-sized moments found: the video may be too short, or the captions too sparse")
    return Session(media, sha, rec, tr, audio.rms_db, cuts, brand, cands, settings, [studio.suggest_take(c) for c in cands])


def moment_posters(s: Session, *, runner: MediaRunner, work: Path) -> list[Path | None]:
    """A still from the middle of each moment (step 2 shows them; any browser can display a JPEG)."""
    out: list[Path | None] = []
    for c in s.candidates:
        in_dir, out_dir, (src,) = analyze.job_dirs(work, [s.media])
        try:
            runner.run("ffmpeg", ["-protocol_whitelist", "file", "-ss", f"{(c.start + c.end) / 2:.2f}", "-i", f"@in/{src.name}",
                                  "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", "-y", "@out/still.jpg"],
                       in_dir=in_dir, out_dir=out_dir, inputs=[src])
            out.append(out_dir / "still.jpg")
        except Exception:  # noqa: BLE001 - a preview is optional
            out.append(None)
    return out


def _transcribe(runner: MediaRunner, media: Path, work: Path) -> Transcript:
    from .transcribe import whisper_transcribe

    in_dir, out_dir, (src,) = analyze.job_dirs(work, [media])
    runner.run("ffmpeg", ["-protocol_whitelist", "file", "-i", f"@in/{src.name}", "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "@out/a.raw"], in_dir=in_dir, out_dir=out_dir, inputs=[src])
    try:
        return whisper_transcribe(np.fromfile(out_dir / "a.raw", dtype="<i2"))
    except ImportError as exc:
        raise StudioError("no captions file, and speech-to-text isn't installed: add an .srt/.vtt "
                          "(YouTube Studio → Subtitles → Download) or install speech-to-text: `pip install '.[asr]'` in a source checkout") from exc


def make_clip(s: Session, cand: Candidate, take: studio.Take, title: str, n: int, *, runner: MediaRunner, work: Path,
              quality: str = "high", gate: GateInputs | None = None) -> Made:
    """Step 3, one clip: plan → render (sandboxed FFmpeg) → full compliance gate."""
    plan = studio.build_plan(cand, take, s.brand, n)
    words, tmap, sents = studio.clip_window(plan, s.tr, s.rms)
    ass = studio.compose_overlay(plan, words, tmap, sents, s.rights, s.brand.channel, s.brand.topics)
    cx = analyze.motion_centroid_x(runner, s.media, work, plan.start, plan.end) if plan.reframe.value == "crop" else 0.5
    in_dir, out_dir, (src,) = analyze.job_dirs(work, [s.media])
    (in_dir / "overlay.ass").write_text(ass, "utf-8")
    runner.run("ffmpeg", ["-protocol_whitelist", "file", *studio.render_args(plan, tmap, src_name=src.name, crop_cx=cx, quality=quality)],
               in_dir=in_dir, out_dir=out_dir, inputs=[src])
    clip = out_dir / "clip.mp4"
    fp = {"dhash": analyze.frame_dhashes(runner, clip, work), "shingles": studio.text_fingerprint(words)}
    g = gate or GateInputs()
    done = studio.finish(plan=plan, n=n, tr=s.tr, rms=s.rms, rights=s.rights, brand=s.brand, settings=s.settings,
                         screen_terms=g.screen_terms or studio.default_screen_terms(), clip_sha256=sha256_file(clip),
                         fingerprint=fp, title=title, kill_switch=g.kill_switch, registry=g.registry,
                         history=g.history(plan) if g.history else None)
    if not done.passed:
        return Made(done, None)
    pin, pout, (c,) = analyze.job_dirs(work, [clip])
    runner.run("ffmpeg", ["-protocol_whitelist", "file", "-ss", f"{min(4.0, done.duration / 3):.2f}", "-i", f"@in/{c.name}",
                          "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", "-y", "@out/poster.jpg"],
               in_dir=pin, out_dir=pout, inputs=[c])
    return Made(done, clip, pout / "poster.jpg")


def package_files(made: list[Made], brand: studio.Brand) -> dict[str, str]:
    return studio.upload_package([m.finished for m in made], brand)


def write_package(made: list[Made], brand: studio.Brand, out: Path) -> Path:
    """Write every passed clip + the copy-paste texts into `out`, and the same set as one zip."""
    out.mkdir(parents=True, exist_ok=True)
    texts = package_files(made, brand)
    zpath = out / ZIP_NAME
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # mp4 doesn't compress; texts are tiny
        for m in made:
            if m.clip is not None:
                data = m.clip.read_bytes()
                (out / m.finished.filename).write_bytes(data)
                z.writestr(m.finished.filename, data)
        for name, text in texts.items():
            (out / name).write_text(text, "utf-8")
            z.writestr(name, text.encode())
    return zpath


def write_zip(made: list[Made], brand: studio.Brand, dest: Path) -> Path:
    """The download as one file on disk (clips are streamed in, never held in memory)."""
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for m in made:
            if m.clip is not None:
                z.write(m.clip, m.finished.filename)
        for name, text in package_files(made, brand).items():
            z.writestr(name, text.encode())
    tmp.replace(dest)
    return dest


__all__ = ["GateInputs", "Made", "Session", "gate_inputs", "moment_posters", "owner_settings", "StudioError", "analyze_source", "make_clip", "package_files", "write_package", "write_zip"]
