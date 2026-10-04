"""Clip look presets (milestone 4): how captions, the hook, the channel pill and the progress bar
are drawn. Everything is ASS (Advanced SubStation Alpha) text rendered by libass inside FFmpeg, so
the same file renders identically in the app's sandbox and in the browser build (ffmpeg.wasm).

Presets
-------
* ``bold``    (default) Inter Display Black, upper case, 2-word groups; the spoken word pops in
              your accent colour. The common "Shorts" look, kept clear of YouTube's own buttons.
* ``clean``   the original fair-clip look: DejaVu Sans, 3-word groups, amber highlight.
* ``minimal`` Inter on a soft dark box, 4-word groups, no pop: for calm or technical channels.

All owner and transcript text is escaped (overlay.escape) before it reaches ASS.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

DISCLOSE_RGB = (0xFD, 0xE0, 0x47)  # reserved for disclosures; accents must stay clearly different
DEFAULT_ACCENT = "#3DDC97"


def ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS stores blue-green-red, alpha 00 = opaque)."""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def accent_problem(hex_rgb: str) -> str | None:
    """Why an accent colour can't be used (None = fine)."""
    if len(hex_rgb) != 7 or hex_rgb[0] != "#" or any(c not in "0123456789abcdefABCDEF" for c in hex_rgb[1:]):
        return "accent must look like #RRGGBB"
    rgb = tuple(int(hex_rgb[i : i + 2], 16) for i in (1, 3, 5))
    dist = sum((a - b) ** 2 for a, b in zip(rgb, DISCLOSE_RGB, strict=True)) ** 0.5
    if dist < 90:
        return "accent is too close to the yellow reserved for disclosures; pick another colour"
    if sum(rgb) < 120:
        return "accent is too dark to read on video"
    return None


@dataclass(frozen=True)
class Preset:
    name: str
    font: str  # caption font family (must exist in data/fonts)
    size_v: int  # caption size on 1080x1920
    size_h: int  # caption size on 1920x1080
    group: int  # words per caption group
    upper: bool
    outline: int
    shadow: int
    box: bool  # BorderStyle 3 (opaque box) instead of an outline
    pop: bool  # spoken word scales up briefly
    caption_y_v: int  # caption centre line on 9:16
    caption_y_h: int
    hook_font: str
    body_font: str


PRESETS: dict[str, Preset] = {
    "bold": Preset("bold", "Inter Display Black", 92, 74, 2, True, 8, 4, False, True, 1330, 920, "Inter Display ExtraBold", "Inter"),
    "clean": Preset("clean", "DejaVu Sans", 76, 64, 3, False, 6, 2, False, False, 1500, 960, "DejaVu Sans", "DejaVu Sans"),
    "minimal": Preset("minimal", "Inter", 60, 50, 4, False, 0, 0, True, False, 1380, 940, "Inter", "Inter"),
}

INK = "#0B1020"
PAPER = "#FFFFFF"


def esc(text: str) -> str:
    return (text.replace("\\", "∖").replace("{", "｛").replace("}", "｝")
            .replace("\r", " ").replace("\n", " "))


def ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def rounded_rect(w: int, h: int, r: int) -> str:
    """ASS drawing of a w x h rectangle with corner radius r (Bezier corners), origin top-left."""
    r = max(0, min(r, w // 2, h // 2))
    k = round(r * 0.45)  # control-point inset that approximates a quarter circle
    return (f"m {r} 0 l {w - r} 0 b {w - k} 0 {w} {k} {w} {r} l {w} {h - r} b {w} {h - k} {w - k} {h} {w - r} {h} "
            f"l {r} {h} b {k} {h} 0 {h - k} 0 {h - r} l 0 {r} b 0 {k} {k} 0 {r} 0")


def styles(p: Preset, vertical: bool, accent: str) -> list[str]:
    size = p.size_v if vertical else p.size_h
    back = ass_colour(INK, 0x40) if p.box else ass_colour(INK, 0x60)
    border = 3 if p.box else 1
    return [
        # Name, Font, Size, Primary, Secondary, Outline, Back, Bold, Italic, Underline, Strike, ScaleX, ScaleY,
        # Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
        f"Style: PCaption,{p.font},{size},{ass_colour(PAPER)},{ass_colour(accent)},{ass_colour(INK)},{back},0,0,0,0,100,100,"
        f"{1 if p.upper else 0},0,{border},{p.outline if not p.box else 14},{p.shadow},5,90,90,0,1",
        f"Style: PHook,{p.hook_font},{int(size * 0.66)},{ass_colour(INK)},{ass_colour(INK)},{ass_colour(PAPER)},{ass_colour(PAPER, 0xFF)},"
        f"0,0,0,0,100,100,0,0,1,0,0,5,60,60,0,1",
        f"Style: PPill,{p.body_font},{int(size * 0.42)},{ass_colour(PAPER)},{ass_colour(PAPER)},{ass_colour(INK)},{ass_colour(INK, 0xFF)},"
        f"0,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1",
        f"Style: PDraw,{p.body_font},20,{ass_colour(PAPER)},{ass_colour(PAPER)},{ass_colour(INK)},{ass_colour(INK, 0xFF)},"
        f"0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
    ]


def _dlg(layer: int, a: float, b: float, style: str, text: str, tags: str = "") -> str:
    return f"Dialogue: {layer},{ts(a)},{ts(b)},{style},,0,0,0,,{{{tags}}}{text}"


def caption_events(p: Preset, mapped: list[tuple[str, float, float]], vertical: bool, accent: str, width: int) -> list[str]:
    """`mapped` = [(escaped word, clip start, clip end)]. One event per spoken word: the whole group
    is visible, the current word is highlighted (and pops, in presets that pop)."""
    out = []
    y = p.caption_y_v if vertical else p.caption_y_h
    x = width // 2
    hi = ass_colour(accent)
    for g in range(0, len(mapped), p.group):
        chunk = mapped[g : g + p.group]
        for i, (_w, a, b) in enumerate(chunk):
            nxt = chunk[i + 1][1] if i + 1 < len(chunk) else b
            parts = []
            for j, (tt, _, _) in enumerate(chunk):
                tt = tt.upper() if p.upper else tt
                if j == i and p.pop:
                    parts.append(f"{{\\c{hi}\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}}{tt}{{\\r}}")
                elif j == i and not p.box:
                    parts.append(f"{{\\c{hi}}}{tt}{{\\r}}")
                else:
                    parts.append(tt)
            # a group fades in on its first word only, so words don't flicker
            fade = "\\fad(60,0)" if i == 0 else ""
            out.append(_dlg(1, a, max(nxt, b), "PCaption", " ".join(parts), f"\\an5\\pos({x},{y}){fade}\\q2"))
    return out


def _fit_lines(text: str, size: int, max_lines: int, widths: tuple[int, ...]) -> tuple[list[str], int]:
    """Wrap `text` into at most `max_lines`, trying wider lines then smaller type, never cutting words."""
    for scale in (1.0, 0.88, 0.78, 0.7):
        for w in widths:
            ww = int(w / scale)
            lines = textwrap.wrap(text, ww)
            if len(lines) <= max_lines:
                return lines, int(size * scale)
    return textwrap.wrap(text, int(widths[-1] / 0.7)), int(size * 0.7)


def hook_events(p: Preset, text: str, seconds: float, vertical: bool, width: int) -> list[str]:
    """A white rounded card with the verbatim hook quote, popping in (scale 85 -> 100 %)."""
    if not text or seconds <= 0:
        return []
    lines, size = _fit_lines(text, int((p.size_v if vertical else p.size_h) * 0.66), 3, (22, 26) if vertical else (40, 46))
    longest = max((len(s) for s in lines), default=1)
    box_w = min(width - 120, int(longest * size * 0.58) + 80)
    box_h = int(len(lines) * size * 1.22) + 56
    cx, cy = width // 2, (300 if vertical else 170) + box_h // 2
    pop = "\\fscx85\\fscy85\\t(0,180,\\fscx100\\fscy100)\\fad(120,200)"
    return [
        _dlg(6, 0, seconds, "PDraw", f"{{\\p1}}{rounded_rect(box_w, box_h, 28)}{{\\p0}}",
             f"\\an5\\pos({cx},{cy})\\1c{ass_colour(PAPER)}\\1a&H08&\\bord0\\shad0{pop}"),
        _dlg(7, 0, seconds, "PHook", "\\N".join(esc(s) for s in lines), f"\\an5\\pos({cx},{cy})\\fs{size}{pop}\\q2"),
    ]


def card_events(p: Preset, label: str, text: str, a: float, b: float, vertical: bool, accent: str, width: int) -> list[str]:
    """The plain value-add card in the preset look: a rounded dark panel in the top band with an
    accent label, the owner's sentence in white. (9:16: above the source frame, never over it.)"""
    if b <= a:
        return []
    size = int((p.size_v if vertical else p.size_h) * 0.5)
    lines, size = _fit_lines(text, size, 4 if vertical else 3, (30, 34) if vertical else (54, 60))
    longest = max((len(s) for s in lines), default=1)
    box_w = min(width - 120, max(int(longest * size * 0.56) + 96, 420))
    label_h = int(size * 1.2)
    box_h = int(len(lines) * size * 1.28) + label_h + 72
    x0 = (width - box_w) // 2
    y0 = 150 if vertical else 60
    fade = "\\fad(180,180)"
    lab_size = int(size * 0.62)
    return [
        _dlg(5, a, b, "PDraw", f"{{\\p1}}{rounded_rect(box_w, box_h, 26)}{{\\p0}}",
             f"\\an7\\pos({x0},{y0})\\1c{ass_colour(INK)}\\1a&H1C&\\bord0\\shad0{fade}"),
        _dlg(6, a, b, "PDraw", f"{{\\p1}}{rounded_rect(int(len(label) * lab_size * 0.72) + 36, int(lab_size * 1.7), 10)}{{\\p0}}",
             f"\\an7\\pos({x0 + 36},{y0 + 30})\\1c{ass_colour(accent)}\\bord0\\shad0{fade}"),
        _dlg(7, a, b, "PPill", esc(label), f"\\an7\\pos({x0 + 54},{y0 + 30 + int(lab_size * 0.28)})\\fs{lab_size}\\b1\\1c{ass_colour(INK)}{fade}\\q2"),
        _dlg(7, a, b, "PPill", "\\N".join(esc(s) for s in lines),
             f"\\an7\\pos({x0 + 36},{y0 + 30 + label_h + 24})\\fs{size}\\1c{ass_colour(PAPER)}{fade}\\q2"),
    ]


def pill_events(p: Preset, text: str, a: float, b: float, vertical: bool, accent: str) -> list[str]:
    """Channel name in a dark pill with an accent dot, sliding in from the left."""
    if not text or b <= a:
        return []
    size = int((p.size_v if vertical else p.size_h) * 0.42)
    w = int(len(text) * size * 0.56) + size + 64
    h = int(size * 1.9)
    y = (1520 if vertical else 980)
    x0, x1 = -w, 48
    move = f"\\move({x0},{y},{x1},{y},0,280)"
    return [
        _dlg(5, a, b, "PDraw", f"{{\\p1}}{rounded_rect(w, h, h // 2)}{{\\p0}}",
             f"\\an4{move}\\1c{ass_colour(INK)}\\1a&H30&\\bord0\\shad0\\fad(0,220)"),
        _dlg(6, a, b, "PDraw", f"{{\\p1}}{rounded_rect(int(size * 0.55), int(size * 0.55), size)}{{\\p0}}",
             f"\\an4\\move({x0 + 26},{y},{x1 + 26},{y},0,280)\\1c{ass_colour(accent)}\\bord0\\shad0\\fad(0,220)"),
        _dlg(7, a, b, "PPill", esc(text), f"\\an4\\move({x0 + 26 + int(size * 0.9)},{y},{x1 + 26 + int(size * 0.9)},{y},0,280)\\fad(0,220)\\q2"),
    ]


def progress_events(duration: float, width: int, accent: str) -> list[str]:
    """A thin accent bar along the top that fills over the clip (viewers see how long is left)."""
    if duration <= 0:
        return []
    ms = int(duration * 1000)
    return [_dlg(8, 0, duration, "PDraw", f"{{\\p1}}m 0 0 l {width} 0 l {width} 8 l 0 8{{\\p0}}",
                 f"\\an7\\pos(0,0)\\1c{ass_colour(accent)}\\bord0\\shad0\\fscx0\\t(0,{ms},\\fscx100)")]
