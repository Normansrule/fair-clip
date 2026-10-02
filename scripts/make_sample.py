#!/usr/bin/env python3
"""Generate the demo source video: an original ~2-minute explainer, released as CC0.

Everything is made here from scratch, so the demo has a clean rights record:
* narration: espeak-ng text-to-speech (a synthetic voice, disclosed by default);
* picture: slides and a geometric "speaker" drawn with Pillow (no real person depicted);
* transcript: exact sentence timings from how we assembled the audio; word timings spread
  across each sentence's voiced span in proportion to word length.

Output (examples/demo/): source.mp4, transcript.json, rights.json, SHA256SUMS
Run: python scripts/make_sample.py
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "demo"
FONT = ROOT / "src" / "fairclip" / "data" / "fonts" / "DejaVuSans.ttf"
FONT_B = ROOT / "src" / "fairclip" / "data" / "fonts" / "DejaVuSans-Bold.ttf"
W, H, FPS, SR = 1280, 720, 25, 22050

INK, PAPER, BLUE, AMBER, SLATE, GREEN = "#0B1020", "#F7F7F2", "#2563EB", "#FFB000", "#64748B", "#16A34A"

# (slide index, text shown in transcript, text spoken, amplitude, speed wpm, pause after s)
SCRIPT = [
    (0, "Here's a question that sounds simple: how does a suspension bridge hold up a fully loaded truck?", None, 190, 175, 0.35),
    (0, "The deck looks like it's floating, but every kilogram of that truck is going somewhere.", None, 150, 165, 0.30),
    (0, "Let's follow the weight, one piece at a time.", None, 140, 150, 1.20),
    (1, "First, the truck pushes down on the deck.", None, 150, 160, 0.30),
    (1, "The deck hangs from vertical ropes called suspenders.", None, 140, 155, 0.30),
    (1, "The suspenders pull down on the two main cables.", None, 140, 155, 0.30),
    (1, "And those main cables are in pure tension, like a rope in a game of tug of war.", None, 170, 170, 0.30),
    (1, "Steel is incredibly good at tension, which is exactly why engineers chose it.", None, 150, 160, 1.00),
    (2, "Now the main cables drape over the towers.", None, 140, 150, 0.30),
    (2, "Here is the surprising part: the towers are not holding the bridge sideways at all.", None, 200, 185, 0.30),
    (2, "The cables pull down on the top of each tower, so the tower is squeezed, in compression.", None, 160, 165, 0.30),
    (2, "Concrete and steel columns love compression, so tall towers can be surprisingly slender.", None, 150, 160, 1.40),
    (3, "But the cables still have to end somewhere.", None, 130, 145, 0.30),
    (3, "At each end they are tied into huge blocks called anchorages, often set deep into rock.", None, 140, 155, 0.30),
    (3, "The anchorage holds the cable back, the way your feet hold you back in tug of war.", None, 150, 160, 0.30),
    (3, "So the weight of the truck travels from deck, to suspenders, to cables, to towers, and finally into the ground.", None, 160, 170, 0.80),
    (4, "So what actually threatens a bridge like this? Usually it is not weight. It is wind.", None, 200, 180, 0.30),
    (4, "In 1940, the Tacoma Narrows Bridge twisted itself apart in a steady wind, only months after it opened.", "In nineteen forty, the Tacoma Narrows Bridge twisted itself apart in a steady wind, only months after it opened.", 180, 175, 0.30),
    (4, "Its deck was thin and flexible, so the wind made it flutter, and each twist made the next one bigger.", None, 170, 175, 0.30),
    (4, "Modern decks are deeper and stiffer, and engineers test scale models in wind tunnels before anything is built.", None, 150, 160, 1.00),
    (5, "Let's recap.", None, 150, 140, 0.40),
    (5, "Cables pull, towers push, anchorages hold, and the ground takes it all.", None, 170, 165, 0.40),
    (5, "And the thing to respect most isn't the heaviest truck. It's the wind.", None, 190, 170, 0.40),
    (5, "Next time you drive across one, you'll know exactly where your weight is going.", None, 150, 160, 0.80),
]
SLIDES = [
    ("Where does the weight go?", "A suspension bridge, piece by piece", None),
    ("1 · Cables in tension", "deck → suspenders → main cables", "cables"),
    ("2 · Towers in compression", "the cable presses DOWN on each tower", "towers"),
    ("3 · Anchorages hold the ends", "the cable is tied back into rock", "anchors"),
    ("4 · The real enemy: wind", "Tacoma Narrows, 1940: flutter", "wind"),
    ("Recap", "cables pull · towers push · anchorages hold", "all"),
]


def tts(text: str, amp: int, wpm: int) -> np.ndarray:
    r = subprocess.run(["espeak-ng", "-v", "en-us", "-a", str(amp), "-s", str(wpm), "-p", "45", "--stdout", text],
                       capture_output=True, check=True)
    with wave.open(io.BytesIO(r.stdout)) as w:
        assert w.getframerate() == SR, w.getframerate()
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)


def voiced_span(x: np.ndarray) -> tuple[int, int]:
    win = int(SR * 0.01)
    env = np.array([np.abs(x[i : i + win]).mean() for i in range(0, len(x) - win, win)])
    thr = max(env.max() * 0.04, 50)
    idx = np.where(env > thr)[0]
    if idx.size == 0:
        return 0, len(x)
    return int(idx[0] * win), int(min(len(x), (idx[-1] + 1) * win))


def build_audio():
    chunks, sentences, t = [], [], 0.6
    chunks.append(np.zeros(int(SR * 0.6), np.float32))
    for slide, text, spoken, amp, wpm, pause in SCRIPT:
        x = tts(spoken or text, amp, wpm)
        a, b = voiced_span(x)
        start, end = t + a / SR, t + b / SR
        words = text.split()
        weights = np.array([len(w.strip(".,:?!'")) + 1 for w in words], float)
        edges = start + np.concatenate([[0], np.cumsum(weights)]) / weights.sum() * (end - start)
        sentences.append({"start": round(start, 3), "end": round(end, 3), "text": text,
                          "words": [{"text": w, "start": round(edges[i], 3), "end": round(edges[i + 1] - 0.02, 3)} for i, w in enumerate(words)],
                          "_slide": slide})
        chunks.append(x)
        chunks.append(np.zeros(int(SR * pause), np.float32))
        t += len(x) / SR + pause
    audio = np.concatenate(chunks + [np.zeros(int(SR * 1.0), np.float32)])
    return audio, sentences


def bridge(d: ImageDraw.ImageDraw, x0: int, y0: int, w: int, focus: str | None, t: float) -> None:
    deck_y = y0 + 190
    tl, tr = x0 + int(w * 0.25), x0 + int(w * 0.75)
    top = y0 + 20
    hl = lambda k: AMBER if focus in (k, "all") else SLATE  # noqa: E731
    # ground + anchorages
    d.rectangle([x0 - 20, deck_y + 70, x0 + w + 20, deck_y + 90], fill="#CBD5E1")
    for ax in (x0, x0 + w - 60):
        d.rectangle([ax, deck_y + 20, ax + 60, deck_y + 80], fill=hl("anchors"))
    # towers
    for tx in (tl, tr):
        d.rectangle([tx - 9, top, tx + 9, deck_y + 70], fill=hl("towers"))
    # main cable (parabola between towers, straight backstays)
    sway = math.sin(t * 5) * 10 if focus == "wind" else 0
    pts = [(x0 + 30, deck_y + 30), (tl, top)]
    for i in range(1, 40):
        u = i / 40
        x = tl + (tr - tl) * u
        pts.append((x, top + 150 * (1 - (2 * u - 1) ** 2)))
    pts += [(tr, top), (x0 + w - 30, deck_y + 30)]
    d.line(pts, fill=hl("cables"), width=6, joint="curve")
    # suspenders + deck
    for i in range(1, 12):
        x = tl + (tr - tl) * i / 12
        u = i / 12
        yc = top + 150 * (1 - (2 * u - 1) ** 2)
        d.line([(x, yc), (x, deck_y + sway * math.sin(math.pi * u))], fill=hl("cables"), width=2)
    deck = [(x0 - 10, deck_y), (x0 + w + 10, deck_y)]
    if focus == "wind":
        deck = [(x0 - 10 + i * (w + 20) / 30, deck_y + sway * math.sin(math.pi * i / 30)) for i in range(31)]
    d.line(deck, fill=INK, width=10)
    # the truck
    d.rectangle([x0 + w // 2 - 40, deck_y - 38, x0 + w // 2 + 40, deck_y - 6], fill=BLUE)
    if focus == "towers":
        for tx in (tl, tr):
            d.polygon([(tx - 16, top - 44), (tx + 16, top - 44), (tx, top - 12)], fill=AMBER)
    if focus == "wind":
        for k in range(4):
            yy = y0 + 70 + k * 40
            xx = x0 + ((t * 180 + k * 90) % (w + 80)) - 40
            d.line([(xx, yy), (xx + 60, yy)], fill=BLUE, width=4)


def main() -> int:
    if not shutil.which("espeak-ng") or not shutil.which("ffmpeg"):
        print("needs espeak-ng and ffmpeg", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    audio, sentences = build_audio()
    peak = np.abs(audio).max() or 1.0
    pcm = (audio / peak * 0.8 * 32767).astype("<i2")
    wav = OUT / "_narration.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    dur = len(pcm) / SR
    nframes = int(dur * FPS)
    env = np.abs(pcm.astype(np.float32)) / 32767
    # slide timeline: a slide starts with its first sentence
    slide_start = {}
    for s in sentences:
        slide_start.setdefault(s["_slide"], s["start"] - 0.4)
    f_title = ImageFont.truetype(str(FONT_B), 46)
    f_sub = ImageFont.truetype(str(FONT), 28)
    f_small = ImageFont.truetype(str(FONT), 20)
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset", "medium",
                           "-crf", "30", "-tune", "animation", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                           "-shortest", "-movflags", "+faststart", "-map_metadata", "-1", str(OUT / "source.mp4")],
                          stdin=subprocess.PIPE)
    assert ff.stdin
    for i in range(nframes):
        t = i / FPS
        k = max([s for s, st in slide_start.items() if st <= t] or [0])
        title, sub, focus = SLIDES[k]
        img = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 8], fill=BLUE)
        d.text((60, 50), title, font=f_title, fill=INK)
        d.text((60, 115), sub, font=f_sub, fill=SLATE)
        bridge(d, 60, 250, 760, focus, t)
        # the "speaker": a geometric avatar on the right whose mouth follows the narration level
        a = int(t * SR)
        lvl = float(env[a : a + SR // FPS].mean()) if a < len(env) else 0.0
        cx, cy = 1060, 390
        bob = int(4 * math.sin(t * 2.1))
        d.ellipse([cx - 110, cy - 110 + bob, cx + 110, cy + 110 + bob], fill=BLUE)
        d.ellipse([cx - 45, cy - 40 + bob, cx - 20, cy - 15 + bob], fill=PAPER)
        d.ellipse([cx + 20, cy - 40 + bob, cx + 45, cy - 15 + bob], fill=PAPER)
        mouth = 6 + int(min(1.0, lvl * 9) * 46)
        d.rounded_rectangle([cx - 42, cy + 30 + bob, cx + 42, cy + 30 + mouth + bob], radius=10, fill=INK)
        d.text((cx - 95, cy + 150), "synthetic narrator", font=f_small, fill=SLATE)
        d.text((60, H - 40), "Sample talk · CC0 · made by fair-clip contributors for the demo", font=f_small, fill=SLATE)
        ff.stdin.write(img.tobytes())
    ff.stdin.close()
    ff.wait()
    wav.unlink()
    for s in sentences:
        s.pop("_slide")
    (OUT / "transcript.json").write_text(json.dumps({"language": "en", "engine": "generator:espeak-ng+proportional-word-timing",
                                                     "sentences": sentences}, indent=1))
    sha = hashlib.sha256((OUT / "source.mp4").read_bytes()).hexdigest()
    rights = {
        "record_id": "demo-bridge-talk-cc0",
        "basis": "creative_commons",
        "cc_license": "CC0-1.0",
        "source_title": "Where does the weight go? (sample talk)",
        "rights_holder": "fair-clip contributors",
        "attribution": "Sample talk by fair-clip contributors",
        "evidence": "examples/demo/LICENSE-SAMPLE.txt",
        "source_sha256": sha,
        "synthetic_or_altered": True,
        "notes": "Synthetic text-to-speech narration; flagged as altered/synthetic by default (when unsure, disclose).",
    }
    (OUT / "rights.json").write_text(json.dumps(rights, indent=2) + "\n")
    (OUT / "SHA256SUMS").write_text(f"{sha}  source.mp4\n")
    print(f"wrote {OUT/'source.mp4'} ({dur:.1f}s, {(OUT/'source.mp4').stat().st_size/1e6:.1f} MB) sha256={sha[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
