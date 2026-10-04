#!/usr/bin/env python3
"""Make the Studio example source: "Resonance", a ~2-minute 1080p motion-graphics explainer, CC0.

Everything is made here, so the example has a clean rights record you can check:
* narration: Kokoro-82M text-to-speech (Apache-2.0 model weights, voice ``af_heart``), run
  locally through kokoro-onnx. The voice is synthetic, so the rights record says so and the
  Studio ticks YouTube's "altered or synthetic content" box for every clip;
* picture: animated diagrams drawn frame by frame with pycairo. No stock footage, music,
  photos, logos or real people;
* captions: exact sentence timings from how the audio was assembled; word timings spread over
  each sentence's voiced span in proportion to its phonemes.

Output (examples/studio/): source.mp4, captions.srt, transcript.json, rights.json, SHA256SUMS
Run:  pip install kokoro-onnx soundfile pycairo   &&   python scripts/make_example.py
The model files (~120 MB) are downloaded once to ~/.cache/fair-clip/kokoro and hash-checked.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # this checkout's fairclip, not some other installed copy
OUT = ROOT / "examples" / "studio"
FONTS = ROOT / "src" / "fairclip" / "data" / "fonts"
W, H, FPS, SR = 1920, 1080, 30, 24000
VOICE = "af_heart"
KOKORO = {  # https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
    "kokoro-v1.0.int8.onnx": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"

# --------------------------------------------------------------------------------------- script
# (shown text, spoken text or None, pause after in seconds). One list per scene.
SCRIPT: list[list[tuple[str, str | None, float]]] = [
    [  # 0 intro
        ("Here's something strange.", None, 0.30),
        ("A bridge can carry a line of heavy trucks without a wobble, and still be shaken by people simply walking across it.", None, 0.40),
        ("The reason is resonance, and once you see it, you'll notice it everywhere.", None, 0.90),
    ],
    [  # 1 swing
        ("Think about pushing a child on a swing.", None, 0.30),
        ("Push at random moments, and nothing much happens.", None, 0.40),
        ("But push once every swing, right at the top, and each small push adds to the last.", None, 0.35),
        ("The swing has its own rhythm, and matching that rhythm is the whole trick.", None, 0.30),
        ("That rhythm is called the natural frequency.", None, 0.90),
    ],
    [  # 2 everything has one
        ("Everything has one: a guitar string, a wine glass, a skyscraper, a bridge.", None, 0.35),
        ("A tall building might sway once every few seconds, and a footbridge might bounce about twice a second.", None, 0.35),
        ("When a force keeps arriving at that same rhythm, small pushes stack up into big motion.", None, 0.90),
    ],
    [  # 3 Millennium Bridge
        ("In June 2000, London opened the Millennium Bridge, and on opening day it began to sway sideways under the crowds.",
         "In June two thousand, London opened the Millennium Bridge, and on opening day it began to sway sideways under the crowds.", 0.35),
        ("When we walk, our bodies rock slightly from side to side, about once a second.", None, 0.35),
        ("As the deck started to move, people adjusted their steps to keep their balance, and that put them in step with the bridge.", None, 0.35),
        ("Thousands of small sideways pushes, all in time. The bridge closed after just two days.", None, 0.90),
    ],
    [  # 4 the fix
        ("The fix wasn't to make it stronger. Engineers added dampers, devices that soak up motion the way shock absorbers do in a car.", None, 0.35),
        ("First they measured the motion, then they fitted 37 fluid dampers, and finally they tested it with crowds of volunteers.",
         "First they measured the motion, then they fitted thirty-seven fluid dampers, and finally they tested it with crowds of volunteers.", 0.35),
        ("It reopened in 2002, and it has stayed steady ever since.", "It reopened in two thousand and two, and it has stayed steady ever since.", 0.90),
    ],
    [  # 5 tuned mass damper
        ("Tall buildings use a similar idea. Taipei 101 hangs a steel ball weighing about 660 tonnes near its top.",
         "Tall buildings use a similar idea. Taipei one oh one hangs a steel ball weighing about six hundred and sixty tonnes near its top.", 0.35),
        ("When the wind pushes the tower one way, the heavy ball lags behind and pulls it back.", None, 0.90),
    ],
    [  # 6 recap
        ("So, resonance in one line: small pushes, perfectly timed, can beat brute force.", None, 0.40),
        ("Engineers don't fight it with strength. They change the rhythm, or they soak it up.", None, 0.40),
    ],
]
TITLES = ["", "the swing", "everything has a rhythm", "London, 2000", "the fix", "the tower", "recap"]
LEAD_IN, TAIL = 0.7, 3.0  # seconds of picture before the first word / after the last


# ----------------------------------------------------------------------------------------- audio
def kokoro_files() -> tuple[Path, Path]:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fair-clip" / "kokoro"
    cache.mkdir(parents=True, exist_ok=True)
    for name, sha in KOKORO.items():
        p = cache / name
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            print(f"downloading {name} …", file=sys.stderr)
            tmp = p.with_suffix(".part")
            urllib.request.urlretrieve(KOKORO_URL + name, tmp)  # noqa: S310 - fixed https URL
            if hashlib.sha256(tmp.read_bytes()).hexdigest() != sha:
                tmp.unlink()
                sys.exit(f"{name}: checksum mismatch, refusing to use it")
            tmp.replace(p)
    return cache / "kokoro-v1.0.int8.onnx", cache / "voices-v1.0.bin"


def voiced_span(x: np.ndarray, thr: float = 0.012) -> tuple[int, int]:
    env = np.convolve(np.abs(x), np.ones(240) / 240, mode="same")
    idx = np.flatnonzero(env > thr)
    return (int(idx[0]), int(idx[-1]) + 1) if len(idx) else (0, len(x))


@dataclass
class Line:
    scene: int
    text: str
    start: float = 0.0
    end: float = 0.0
    words: list[tuple[str, float, float]] = field(default_factory=list)


def narrate() -> tuple[np.ndarray, list[Line]]:
    from kokoro_onnx import Kokoro

    model, voices = kokoro_files()
    k = Kokoro(str(model), str(voices))
    chunks, lines, t = [np.zeros(int(LEAD_IN * SR), np.float32)], [], LEAD_IN
    for si, scene in enumerate(SCRIPT):
        for text, spoken, pause in scene:
            audio, sr = k.create(spoken or text, voice=VOICE, speed=1.0, lang="en-us")
            assert sr == SR
            a, b = voiced_span(audio)
            audio = audio[max(0, a - 480): b + 960].astype(np.float32)  # keep 20 ms in, 40 ms out
            dur = len(audio) / SR
            ln = Line(si, text, round(t + 0.02, 3), round(t + dur - 0.04, 3))
            # word timings: share the voiced span by phoneme count (+ a beat for commas)
            shown, said = text.split(), (spoken or text).split()
            if len(said) != len(shown):
                said = shown
            weights = [max(1, len(k.tokenizer.phonemize(w.strip(",.:;?!"), "en-us"))) + (3 if w[-1] in ",:;" else 0) for w in said]
            span, cur = ln.end - ln.start, ln.start
            for w, wt in zip(shown, weights, strict=True):
                d = span * wt / sum(weights)
                ln.words.append((w, round(cur, 3), round(cur + d * (0.8 if w[-1] in ",:;" else 0.94), 3)))
                cur += d
            lines.append(ln)
            chunks += [audio, np.zeros(int(pause * SR), np.float32)]
            t += dur + pause
            print(f"  {ln.start:7.2f}  {text[:70]}", file=sys.stderr)
    chunks.append(np.zeros(int(TAIL * SR), np.float32))
    pcm = np.concatenate(chunks)
    pcm = pcm / max(1e-6, float(np.max(np.abs(pcm)))) * 0.89
    return pcm, lines


# ------------------------------------------------------------------------------------- drawing
def fontconfig() -> None:
    """Let cairo find the bundled Inter fonts without installing anything system-wide."""
    conf = Path(tempfile.mkdtemp()) / "fonts.conf"
    conf.write_text(f'<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir>{FONTS}</dir>'
                    '<include ignore_missing="yes">/etc/fonts/fonts.conf</include></fontconfig>')
    os.environ["FONTCONFIG_FILE"] = str(conf)


def hexc(h: str, a: float = 1.0) -> tuple[float, float, float, float]:
    h = h.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a


INK, MUTED, ACCENT, BLUE, CORAL, GOLD = "#F4F6FB", "#8A94A8", "#3DDC97", "#5B8CFF", "#FF6B6B", "#FFB547"


def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def ease(x: float) -> float:
    x = clamp(x)
    return x * x * (3 - 2 * x)


def appear(t: float, at: float, d: float = 0.5) -> float:
    return ease((t - at) / d)


class Painter:
    def __init__(self, ctx):
        self.c = ctx

    def text(self, s, x, y, size, colour=INK, alpha=1.0, face="Inter", bold=True, align="center", spacing=0.0):
        import cairo

        c = self.c
        c.select_font_face(face, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        c.set_font_size(size)
        width = sum(c.text_extents(ch).x_advance + spacing for ch in s) - spacing if spacing else c.text_extents(s).x_advance
        x0 = x - width / 2 if align == "center" else (x - width if align == "right" else x)
        c.set_source_rgba(*hexc(colour, alpha))
        if spacing:
            for ch in s:
                c.move_to(x0, y)
                c.show_text(ch)
                x0 += c.text_extents(ch).x_advance + spacing
        else:
            c.move_to(x0, y)
            c.show_text(s)
        return width

    def line(self, pts, colour, width, alpha=1.0, glow=True):
        c = self.c
        for w, a in ([(width * 3.2, 0.10), (width * 1.8, 0.18)] if glow else []) + [(width, 1.0)]:
            c.set_line_width(w)
            c.set_line_cap(1)
            c.set_line_join(1)
            c.move_to(*pts[0])
            for p in pts[1:]:
                c.line_to(*p)
            c.set_source_rgba(*hexc(colour, alpha * a))
            c.stroke()

    def rrect(self, x, y, w, h, r):
        c = self.c
        c.new_sub_path()
        c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        c.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        c.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        c.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        c.close_path()

    def fill(self, colour, alpha=1.0):
        self.c.set_source_rgba(*hexc(colour, alpha))
        self.c.fill()

    def circle(self, x, y, r, colour, alpha=1.0):
        self.c.arc(x, y, r, 0, 2 * math.pi)
        self.fill(colour, alpha)

    def arrow(self, x0, y0, x1, y1, colour, width=8, alpha=1.0, head=26):
        ang = math.atan2(y1 - y0, x1 - x0)
        self.line([(x0, y0), (x1 - head * 0.6 * math.cos(ang), y1 - head * 0.6 * math.sin(ang))], colour, width, alpha, glow=False)
        c = self.c
        c.move_to(x1, y1)
        c.line_to(x1 - head * math.cos(ang - 0.45), y1 - head * math.sin(ang - 0.45))
        c.line_to(x1 - head * math.cos(ang + 0.45), y1 - head * math.sin(ang + 0.45))
        c.close_path()
        self.fill(colour, alpha)


@dataclass
class Scene:
    index: int
    start: float
    end: float
    lines: list[Line]

    def mark(self, i: int) -> float:  # sentence i start, scene-local
        return self.lines[min(i, len(self.lines) - 1)].start - self.start

    def word(self, i: int, word: str) -> float:  # when `word` is said in sentence i, scene-local
        for w, a, _b in self.lines[i].words:
            if w.strip(",.:;?!").lower() == word.lower():
                return a - self.start
        return self.mark(i)


def wave(p: Painter, x0, x1, y, amp_fn, period, phase, colour, width=6, alpha=1.0, upto=1.0):
    pts = []
    n = 220
    for k in range(int(n * upto) + 1):
        u = k / n
        x = x0 + (x1 - x0) * u
        pts.append((x, y - amp_fn(u) * math.sin(2 * math.pi * (u * (x1 - x0) / period) - phase)))
    if len(pts) > 1:
        p.line(pts, colour, width, alpha)


def s_intro(p: Painter, t, sc: Scene):
    dur = sc.end - sc.start
    p.text("RESONANCE", W / 2, 470, 150, INK, appear(t, 0.15, 0.8), face="Inter Display", spacing=14)
    p.text("why small, well-timed pushes can beat brute force", W / 2, 560, 40, MUTED, appear(t, 0.9, 0.8), bold=False)
    grow = ease(t / dur)
    wave(p, 260, 1660, 780, lambda u: (6 + 90 * grow) * math.sin(math.pi * u) ** 1.5, 230, t * 5.5, ACCENT, 6, appear(t, 0.4, 0.8))


RANDOM_PUSHES = [0.4, 1.3, 1.7, 2.9, 3.4]


def s_swing(p: Painter, t, sc: Scene):
    c = p.c
    px, py, L, T = W / 2, 170, 560, 2.6
    # frame
    p.line([(px - 260, 900), (px - 30, py), (px + 30, py), (px + 260, 900)], MUTED, 10, 0.7, glow=False)
    p.line([(px - 320, py), (px + 320, py)], MUTED, 14, 0.8, glow=False)
    timed = sc.mark(2)
    grow = ease((t - timed) / max(1.0, sc.mark(4) - timed))
    amp = math.radians(5 + 33 * grow)
    th = amp * math.sin(2 * math.pi * t / T)
    sx, sy = px + L * math.sin(th), py + L * math.cos(th)
    # swept arc (how far it reaches)
    c.set_line_width(3)
    c.arc(px, py, L, math.pi / 2 - amp, math.pi / 2 + amp)
    c.set_source_rgba(*hexc(ACCENT, 0.25 * appear(t, timed, 1)))
    c.stroke()
    for dx in (-46, 46):
        p.line([(px + dx, py), (sx + dx * math.cos(th), sy - dx * math.sin(th))], INK, 4, 0.85, glow=False)
    c.save()
    c.translate(sx, sy)
    c.rotate(-th)
    p.rrect(-80, -10, 160, 22, 10)
    p.fill(GOLD)
    p.circle(0, -96, 36, INK, 0.95)
    p.rrect(-26, -62, 52, 54, 16)
    p.fill(INK, 0.95)
    c.restore()
    # pushes: random ones first, then one at the top of every swing
    phase = (t / T) % 1.0
    if t < timed:
        for r in RANDOM_PUSHES:
            rt = sc.mark(1) + r
            a = math.exp(-((t - rt) ** 2) / 0.02)
            if a > 0.02:
                p.arrow(sx - 210, sy - 40, sx - 90, sy - 40, CORAL, 9, a)
    else:
        a = math.exp(-((phase - 0.75) ** 2) / 0.004) * appear(t, timed, 0.4)
        if a > 0.02:
            p.arrow(sx + 220, sy - 40, sx + 100, sy - 40, ACCENT, 10, a)
            p.text("push", sx + 170, sy - 74, 34, ACCENT, a)
    nf = sc.mark(4)
    p.text("natural frequency", W / 2, 990, 52, ACCENT, appear(t, nf, 0.6), face="Inter Display")


def s_everything(p: Painter, t, sc: Scene):
    c = p.c
    labels = [("string", "hundreds / second"), ("glass", "hundreds / second"), ("tower", "1 sway / few seconds"), ("bridge", "~2 bounces / second")]
    focus = appear(t, sc.mark(2), 0.6)
    for i, (name, rate) in enumerate(labels):
        cx, cy = W / 2 + (i - 1.5) * 410, 500
        when = sc.word(0, name if name != "tower" else "skyscraper") - 0.15
        a = appear(t, when, 0.5)
        if a <= 0:
            continue
        dim = 1 - 0.65 * focus if i != 3 else 1.0
        p.rrect(cx - 175, cy - 230, 350, 420, 28)
        p.fill(INK, 0.045 * a)
        p.rrect(cx - 175, cy - 230, 350, 420, 28)
        c.set_line_width(4 if i == 3 else 2)
        c.set_source_rgba(*hexc(ACCENT if (i == 3 and focus > 0) else INK, (0.12 + 0.6 * focus * (i == 3)) * a))
        c.stroke()
        al = a * dim
        if i == 0:
            p.circle(cx - 125, cy - 40, 10, MUTED, al)
            p.circle(cx + 125, cy - 40, 10, MUTED, al)
            pts = [(cx - 125 + 250 * u / 40, cy - 40 + 22 * math.sin(math.pi * u / 40) * math.sin(2 * math.pi * 7 * t)) for u in range(41)]
            p.line(pts, GOLD, 4, al)
        elif i == 1:
            s = 1 + 0.035 * math.sin(2 * math.pi * 5 * t)
            c.save()
            c.translate(cx, cy - 60)
            c.scale(s, 1 / s)
            c.move_to(-70, -90)
            c.curve_to(-75, 10, -20, 40, 0, 40)
            c.curve_to(20, 40, 75, 10, 70, -90)
            c.set_line_width(6)
            c.set_source_rgba(*hexc(BLUE, al))
            c.stroke()
            c.restore()
            p.line([(cx, cy - 20), (cx, cy + 70)], BLUE, 6, al, glow=False)
            p.line([(cx - 50, cy + 72), (cx + 50, cy + 72)], BLUE, 6, al, glow=False)
        elif i == 2:
            sway = 16 * math.sin(2 * math.pi * t / 3.2)
            c.save()
            c.translate(cx, cy + 80)
            c.transform(__import__("cairo").Matrix(1, 0, -sway / 220, 1, 0, 0))
            p.rrect(-40, -230, 80, 230, 6)
            p.fill(MUTED, 0.9 * al)
            for r in range(9):
                for q in (-1, 1):
                    p.rrect(q * 18 - 7, -215 + r * 24, 14, 12, 2)
                    p.fill("#0E1424", al)
            c.restore()
        else:
            for x in (cx - 110, cx + 110):
                p.line([(x, cy - 110), (x, cy + 70)], INK, 7, al, glow=False)
            bounce = 16 * math.sin(2 * math.pi * 2 * t) * (0.4 + 0.6 * focus)
            pts = [(cx - 160 + 320 * u / 40, cy + 10 + bounce * math.sin(math.pi * u / 40)) for u in range(41)]
            p.line(pts, ACCENT, 8, al)
        p.text(name, cx, cy + 140, 42, INK, al)
        p.text(rate, cx, cy + 180, 26, MUTED, al * appear(t, sc.mark(1), 0.6), bold=False)
    p.text("same rhythm  →  small pushes stack up", W / 2, 960, 46, ACCENT, focus, face="Inter Display")


WALKERS = [(random.Random(i).uniform(0, 1), random.Random(i * 7 + 1).uniform(-1, 1), random.Random(i * 3 + 2).uniform(0, 2 * math.pi),
            random.Random(i * 5 + 3).choice([-1, 1])) for i in range(70)]


def deck_topview(p: Painter, t, x0, x1, yc, half, sway, sync, colour_mix):
    p.c.rectangle(0, yc - half - 170, W, 120)
    p.fill(BLUE, 0.10)
    p.c.rectangle(0, yc + half + 50, W, 120)
    p.fill(BLUE, 0.10)
    off = sway * math.sin(2 * math.pi * t / 1.0)
    p.rrect(x0, yc - half + off, x1 - x0, 2 * half, 18)
    p.fill("#26324D")
    p.rrect(x0, yc - half + off, x1 - x0, 2 * half, 18)
    p.c.set_line_width(3)
    p.c.set_source_rgba(*hexc(INK, 0.25))
    p.c.stroke()
    for u, lane, ph, d in WALKERS:
        x = x0 + 30 + ((u + d * t * 0.025) % 1.0) * (x1 - x0 - 60)
        phase = (1 - sync) * ph
        y = yc + off + lane * (half - 22) * 0.75 + 7 * math.sin(2 * math.pi * t / 1.0 + phase)
        p.circle(x, y, 9, ACCENT if colour_mix > 0.5 else INK, 0.9)
    return off


def s_millennium(p: Painter, t, sc: Scene):
    p.text("LONDON  ·  JUNE 2000", W / 2, 170, 40, ACCENT, appear(t, 0.1), spacing=6)
    s0, s1 = sc.mark(2), sc.mark(3)
    sync = ease((t - s0) / max(0.5, s1 - s0))
    sway = 3 + 24 * ease((t - s0 + 1.0) / max(0.5, s1 - s0 + 2.5))
    deck_topview(p, t, 220, 1700, 540, 90, sway, sync, sync)
    a = appear(t, s0, 0.6)
    p.arrow(1790, 540, 1790, 440, CORAL, 7, a, 22)
    p.arrow(1790, 540, 1790, 640, CORAL, 7, a, 22)
    p.text("sideways sway", 1790, 700, 28, CORAL, a)
    p.text("each walker rocks ~1× a second", W / 2, 840, 34, MUTED, appear(t, sc.mark(1), 0.6) * (1 - appear(t, s1, 0.4)), bold=False)
    p.text("in step with the bridge", W / 2, 840, 40, ACCENT, appear(t, sc.word(2, "step"), 0.5) * (1 - appear(t, s1, 0.4)))
    p.text("closed after 2 days", W / 2, 900, 76, CORAL, appear(t, sc.word(3, "closed"), 0.5), face="Inter Display")


def s_fix(p: Painter, t, sc: Scene):
    m1 = sc.mark(1)
    fitted = sc.word(1, "fitted")
    sway = 22 * (math.exp(-max(0.0, t - fitted) / 1.4))
    off = deck_topview(p, t, 340, 1580, 300, 70, sway, 0.0, 0)
    # damper between deck and bank
    a = appear(t, sc.word(0, "dampers") - 0.2, 0.5)
    yd = 300 + 70 + off
    p.rrect(W / 2 - 34, 450, 68, 90, 10)
    p.fill(GOLD, a)
    p.line([(W / 2, yd), (W / 2, 470)], INK, 10, a, glow=False)
    p.text("damper", W / 2 + 70, 510, 32, GOLD, a, align="left")
    steps = [("1", "measure the motion", sc.word(1, "First")), ("2", "fit 37 fluid dampers", fitted),
             ("3", "test with crowds", sc.word(1, "finally"))]
    for k, (n, s, at) in enumerate(steps):
        y = 690 + k * 100
        a = appear(t, at - 0.1, 0.4) if t >= m1 - 0.2 else 0
        p.circle(700, y - 16, 34, ACCENT, a)
        p.text(n, 700, y - 1, 40, "#0E1424", a)
        p.text(s, 760, y, 50, INK, a, align="left")
    p.text("reopened 2002", 1540, 1000, 40, ACCENT, appear(t, sc.mark(2), 0.5), align="right")


def s_tower(p: Painter, t, sc: Scene):
    c = p.c
    cx, base, h = 820, 1000, 780
    wind = appear(t, sc.mark(1), 0.4)
    damp = appear(t, sc.word(1, "pulls"), 1.5)
    ang = math.radians(1.6 * (0.35 + 0.65 * wind) * (1 - 0.6 * damp)) * math.sin(2 * math.pi * t / 4.0)
    c.save()
    c.translate(cx, base)
    c.rotate(ang)
    for k in range(8):  # stacked sections
        w0, y0 = 150 - k * 9, -k * 88
        c.move_to(-w0 / 2, y0)
        c.line_to(-w0 / 2 - 10, y0 - 88)
        c.line_to(w0 / 2 + 10, y0 - 88)
        c.line_to(w0 / 2, y0)
        c.close_path()
        p.fill("#2A3654")
        c.move_to(-w0 / 2, y0)
        c.line_to(-w0 / 2 - 10, y0 - 88)
        c.line_to(w0 / 2 + 10, y0 - 88)
        c.line_to(w0 / 2, y0)
        c.close_path()
        c.set_line_width(2)
        c.set_source_rgba(*hexc(INK, 0.25))
        c.stroke()
    p.line([(0, -704), (0, -h)], MUTED, 6, glow=False)
    # the ball hangs from near the top and lags behind the sway
    a = appear(t, sc.word(0, "steel") - 0.2, 0.5)
    hang = -640
    lag = -ang * 9
    bx, by = 120 * math.sin(lag), hang + 120 * math.cos(lag)
    p.line([(0, hang), (bx, by)], INK, 3, a, glow=False)
    p.circle(bx, by, 40, GOLD, a)
    c.restore()
    bxw = cx + math.cos(ang) * bx - math.sin(ang) * by
    byw = base + math.sin(ang) * bx + math.cos(ang) * by
    la = appear(t, sc.word(0, "660"), 0.5)
    p.line([(bxw + 50, byw), (1180, byw - 40)], GOLD, 3, la, glow=False)
    p.text("660 t", 1200, byw - 10, 110, GOLD, la, face="Inter Display", align="left")
    p.text("steel ball, hung near the top", 1205, byw + 40, 32, MUTED, la, bold=False, align="left")
    for k in range(3):
        y = 330 + k * 120
        x = 200 + ((t * 260 + k * 150) % 380)
        p.arrow(x, y, x + 140, y, BLUE, 6, wind * 0.8, 20)
    p.text("wind", 260, 290, 32, BLUE, wind, align="left")


def s_recap(p: Painter, t, sc: Scene):
    reveal = clamp(t / 3.5)
    p.text("random pushes", 560, 372, 40, MUTED, appear(t, 0.2), align="right")
    rnd = random.Random(5)
    jit = [rnd.uniform(0.3, 1.0) for _ in range(300)]
    wave(p, 620, 1640, 360, lambda u: 16 * jit[int(u * 299)], 120, t * 3, MUTED, 5, appear(t, 0.2), reveal)
    p.text("timed pushes", 560, 652, 40, ACCENT, appear(t, 0.6), align="right")
    wave(p, 620, 1640, 640, lambda u: 8 + 120 * u, 120, t * 3, ACCENT, 6, appear(t, 0.6), reveal)
    p.text("small pushes, perfectly timed", W / 2, 900, 64, INK, appear(t, sc.word(0, "small"), 0.5) * (1 - appear(t, sc.mark(1), 0.4)),
           face="Inter Display")
    p.text("change the rhythm, or soak it up", W / 2, 900, 64, ACCENT, appear(t, sc.word(1, "change"), 0.5) * (1 - appear(t, sc.end - sc.start - TAIL + 0.6, 0.4)),
           face="Inter Display")
    end = sc.end - sc.start - TAIL + 0.8
    a = appear(t, end, 0.6)
    if a > 0:
        p.c.rectangle(0, 0, W, H)
        p.fill("#0B1020", a * 0.92)
        p.text("RESONANCE", W / 2, 520, 110, INK, a, face="Inter Display", spacing=10)
        p.text("CC0 example video · made with fair-clip", W / 2, 600, 34, MUTED, a, bold=False)


SCENES = [s_intro, s_swing, s_everything, s_millennium, s_fix, s_tower, s_recap]


def background():
    import cairo

    bg = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    c = cairo.Context(bg)
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, *hexc("#121A33")[:3])
    g.add_color_stop_rgb(1, *hexc("#0A0F1C")[:3])
    c.set_source(g)
    c.paint()
    r = cairo.RadialGradient(W / 2, H * 0.45, 100, W / 2, H * 0.45, W * 0.7)
    r.add_color_stop_rgba(0, *hexc(BLUE, 0.10))
    r.add_color_stop_rgba(1, *hexc(BLUE, 0.0))
    c.set_source(r)
    c.paint()
    for x in range(48, W, 48):
        for y in range(48, H, 48):
            c.arc(x, y, 1.6, 0, 2 * math.pi)
            c.set_source_rgba(1, 1, 1, 0.05)
            c.fill()
    return bg


def render(lines: list[Line], total: float, wav: Path, out: Path) -> None:
    import cairo

    scenes = []
    for si in range(len(SCRIPT)):
        ls = [ln for ln in lines if ln.scene == si]
        start = 0.0 if si == 0 else ls[0].start - 0.45
        scenes.append(Scene(si, start, 0.0, ls))
    for i, sc in enumerate(scenes):
        sc.end = scenes[i + 1].start if i + 1 < len(scenes) else total
    bg = background()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surf)
    p = Painter(ctx)
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS),
                           "-i", "-", "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                           "-tune", "animation", "-pix_fmt", "yuv420p", "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                           "-movflags", "+faststart", "-shortest", str(out)], stdin=subprocess.PIPE)
    n = int(total * FPS)
    for f in range(n):
        t = f / FPS
        sc = next(s for s in scenes if s.start <= t < s.end or s is scenes[-1])
        lt = t - sc.start
        ctx.set_source_surface(bg)
        ctx.paint()
        fade = min(appear(lt, 0, 0.35), 1 - appear(lt, sc.end - sc.start - 0.3, 0.3)) if sc is not scenes[-1] else appear(lt, 0, 0.35)
        ctx.push_group()
        SCENES[sc.index](p, lt, sc)
        if TITLES[sc.index]:
            p.text(f"{sc.index:02d}  /  {TITLES[sc.index].upper()}", 80, 92, 24, MUTED, 0.9, align="left", spacing=3)
        p.text("fair-clip example · CC0", W - 80, H - 56, 22, MUTED, 0.6, bold=False, align="right")
        ctx.pop_group_to_source()
        ctx.paint_with_alpha(fade)
        surf.flush()
        ff.stdin.write(bytes(surf.get_data()))
        if f % (FPS * 10) == 0:
            print(f"  frame {f}/{n}", file=sys.stderr)
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed")


def to_srt(lines: list[Line]) -> str:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{ts(ln.start)} --> {ts(ln.end)}\n{ln.text}\n\n" for i, ln in enumerate(lines, 1))


def main() -> None:
    fontconfig()
    import soundfile as sf

    OUT.mkdir(parents=True, exist_ok=True)
    print("narrating …", file=sys.stderr)
    pcm, lines = narrate()
    total = len(pcm) / SR
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "narration.wav"
        sf.write(wav, pcm, SR, subtype="PCM_16")
        print(f"drawing {total:.1f} s at {FPS} fps …", file=sys.stderr)
        render(lines, total, wav, OUT / "source.mp4")
    transcript = {"language": "en", "engine": f"generator:kokoro-82m:{VOICE}+phoneme-weighted-word-timing",
                  "sentences": [{"start": ln.start, "end": ln.end, "text": ln.text,
                                 "words": [{"text": w, "start": a, "end": b} for w, a, b in ln.words]} for ln in lines]}
    from fairclip.models import Transcript

    Transcript.model_validate(transcript)
    (OUT / "transcript.json").write_text(json.dumps(transcript, indent=1) + "\n")
    (OUT / "captions.srt").write_text(to_srt(lines))
    sha = hashlib.sha256((OUT / "source.mp4").read_bytes()).hexdigest()
    rights = {"record_id": "example-resonance-cc0", "basis": "creative_commons", "cc_license": "CC0-1.0",
              "source_title": "Resonance (fair-clip example video)", "rights_holder": "fair-clip contributors",
              "attribution": "\"Resonance\" example video by fair-clip contributors",
              "evidence": "examples/studio/LICENSE-EXAMPLE.txt", "source_sha256": sha, "synthetic_or_altered": True,
              "notes": "AI text-to-speech narration (Kokoro-82M); flagged as altered/synthetic."}
    (OUT / "rights.json").write_text(json.dumps(rights, indent=2) + "\n")
    sums = "".join(f"{hashlib.sha256((OUT / n).read_bytes()).hexdigest()}  {n}\n" for n in ("source.mp4", "captions.srt", "transcript.json"))
    (OUT / "SHA256SUMS").write_text(sums)
    print(f"done: {OUT}  ({total:.1f} s)", file=sys.stderr)


if __name__ == "__main__":
    main()
