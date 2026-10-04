"""Value-add templates (milestone 2): structured, animated explainers drawn in the ASS overlay.

Each template is ASS *drawing* commands (``{\\p1}m x y l ...``) for panels and bars plus escaped
text events. Animation uses only standard libass features: staggered event start times (steps,
count-up), ``\\fad`` fades, and ``\\t(..)`` transforms (bars growing). Nothing here touches the
source video; on 9:16 every template sits in the top band above the frame, so the original is
never covered (docs/TEMPLATES.md has a still of each one).

All strings come from the owner (validated in models.TemplateData) and are escaped with
overlay.escape before they reach the ASS file, so they can't inject override tags.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from ..models import TemplateKind, ValueAddElement, fmt_value

# Palette (docs/STYLE.md): context blue for value-add, ink/paper for text. Disclosure yellow is
# reserved for disclosures and never used here.
BLUE = "&H00EB6325&"  # #2563EB as &HAABBGGRR
BLUE_SOFT = "&H00FEC193&"  # #93C5FD
INK = "&H0020100B&"  # #0B1020
PAPER = "&H00FFFFFF&"
PANEL_ALPHA = "&H18&"  # nearly opaque


def esc(text: str) -> str:
    return (text.replace("\\", "∖").replace("{", "｛").replace("}", "｝")
            .replace("\r", " ").replace("\n", " "))


def ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


@dataclass(frozen=True)
class Box:
    """The area a template may draw in, in ASS pixels (PlayRes)."""

    x: int
    y: int
    w: int
    h: int
    font: int  # body font size
    wrap: int  # characters per line for the owner sentence


# 9:16: the blurred top band above the source frame (frame starts at y≈656 of 1920).
VERTICAL_BOX = Box(60, 96, 960, 548, 48, 34)
# 16:9: a panel on the right third; the source fills the frame, so keep it compact.
HORIZONTAL_BOX = Box(1180, 60, 680, 500, 34, 34)


def box_for(aspect: str) -> Box:
    return VERTICAL_BOX if aspect == "9:16" else HORIZONTAL_BOX


def _dlg(layer: int, a: float, b: float, text: str, tags: str = "") -> str:
    return f"Dialogue: {layer},{ts(a)},{ts(b)},Tpl,,0,0,0,,{{{tags}}}{text}"


def _rect(x: int, y: int, w: int, h: int) -> str:
    return f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"


def _shape(layer: int, a: float, b: float, x: int, y: int, w: int, h: int, colour: str, alpha: str = "&H00&",
           extra: str = "", fade: bool = True) -> str:
    f = "\\fad(200,200)" if fade else ""
    return _dlg(layer, a, b, f"{{\\p1}}{_rect(x, y, w, h)}{{\\p0}}",
                f"\\an7\\pos({x},{y})\\bord0\\shad0\\1c{colour}\\1a{alpha}{f}{extra}")


def _text(layer: int, a: float, b: float, x: int, y: int, text: str, size: int, colour: str = PAPER,
          bold: bool = False, an: int = 7, fade: str = "\\fad(200,200)", extra: str = "") -> str:
    # \q2: no automatic wrapping; lines break only where we put \N, so layout stays predictable.
    return _dlg(layer, a, b, text, f"\\an{an}\\pos({x},{y})\\q2\\fs{max(12, size)}\\b{int(bold)}\\bord0\\shad0\\1c{colour}{fade}{extra}")


def fit(size: int, width_px: int, text: str, em: float = 0.58) -> int:
    """Largest font size <= `size` at which `text` fits `width_px` on one line (DejaVu Sans averages
    ~0.55 em per character; 0.58 leaves a margin for wide letters and bold)."""
    return max(14, min(size, int(width_px / (em * max(1, len(text))))))


def _wrapped(text: str, width: int) -> str:
    return "\\N".join(textwrap.wrap(esc(text), width=width)) or esc(text)


LABELS = {
    TemplateKind.KEY_NUMBER: "BY THE NUMBERS",
    TemplateKind.DEFINITION: "DEFINITION",
    TemplateKind.STEPS: "STEP BY STEP",
    TemplateKind.COMPARE: "SIDE BY SIDE",
    TemplateKind.BAR: "COMPARED",
}

# Count up only plain figures: optional currency/sign prefix, ONE number, a suffix with no digits
# ("$1.9B", "853 m", "37%"). "COVID-19 cases", "10/7" or "2 of 3" just appear, unanimated.
NUM = re.compile(r"^([$€£+~≈]?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\D*)$")
COUNT_UP_SECONDS = 0.5  # the exact figure is on screen from here to the element's end
MIN_FINAL_SECONDS = 0.8


def count_up_frames(headline: str, steps: int = 8) -> list[str]:
    """'$1,250.5B' -> ['$0B', '$156.3B', ..., '$1,250.5B']: intermediate values for a count-up.

    The last frame is always the exact owner text (that's what the gate checked). Headlines
    without a parseable number just show as-is.
    """
    m = NUM.match(headline)
    if not m:
        return [headline]
    pre, num, post = m.groups()
    try:
        target = float(num.replace(",", ""))
    except ValueError:
        return [headline]
    if target < 10:  # counting 0 -> 3 is noise, not emphasis
        return [headline]
    decimals = len(num.split(".")[1]) if "." in num else 0
    commas = "," in num
    out = []
    for i in range(steps):
        v = target * (i / steps) ** 0.6  # ease-out: fast start, settles on the value
        s = f"{v:,.{decimals}f}" if commas else f"{v:.{decimals}f}"
        out.append(f"{pre}{s}{post}")
    out.append(headline)
    return out


def template_events(va: ValueAddElement, aspect: str, duration: float, label: str, *, count_up: bool = True) -> list[str]:
    """ASS events for one value-add element drawn with a template (anything but CARD).

    `count_up=False` (markets clips) shows the exact figure from the first frame: intermediate
    numbers would be figures on screen that no data source backs."""
    B = box_for(aspect)
    a, b = min(va.start, duration), min(va.end, duration)
    if b <= a or va.template is TemplateKind.CARD or va.data is None:
        return []
    d = va.data
    pad = 28
    x0, y0, inner = B.x + pad, B.y + pad, B.w - 2 * pad
    ev: list[str] = [
        _shape(5, a, b, B.x, B.y, B.w, B.h, INK, PANEL_ALPHA),
        _shape(6, a, b, B.x, B.y, 10, B.h, BLUE),  # accent bar on the left edge
        _text(7, a, b, x0 + 6, y0, esc(LABELS.get(va.template, label)), int(B.font * 0.7), BLUE_SOFT, bold=True),
    ]
    top = y0 + int(B.font * 1.3)
    # The owner's sentence always closes the panel: it is the value-add the gate measures.
    sentence_lines = max(1, len(textwrap.wrap(va.text, B.wrap)))
    sent_h = int(sentence_lines * B.font * 0.82 * 1.25)
    sent_y = B.y + B.h - pad - sent_h
    ev.append(_text(7, a, b, x0 + 6, sent_y, _wrapped(va.text, B.wrap), int(B.font * 0.82), PAPER))
    area_h = sent_y - top - 16  # space for the graphic itself
    t = va.template

    if t is TemplateKind.KEY_NUMBER:
        # Fit the figure on one line: ~0.62 em per character in DejaVu Sans Bold.
        big = max(24, min(int(B.font * 3.2), int(area_h * 0.7), int(inner / (0.62 * max(1, len(d.headline))))))
        frames = count_up_frames(d.headline) if count_up else [d.headline]
        # The exact (gated) text must hold the screen for the end of the element, however short.
        if len(frames) > 1 and (b - a) >= COUNT_UP_SECONDS + MIN_FINAL_SECONDS:
            step = COUNT_UP_SECONDS / (len(frames) - 1)
        else:
            frames, step = [d.headline], 0.0
        for i, f in enumerate(frames):
            last = i == len(frames) - 1
            fa = a + i * step
            fb = b if last else a + (i + 1) * step
            ev.append(_text(8, fa, fb, x0 + 6, top, esc(f), big, PAPER, bold=True,
                            fade="\\fad(0,200)" if last else ""))
        if d.unit:
            ev.append(_text(8, a, b, x0 + 10, top + int(big * 1.08), esc(d.unit), int(B.font * 0.9), BLUE_SOFT, bold=True))

    elif t is TemplateKind.DEFINITION:
        ev.append(_text(8, a, b, x0 + 6, top, esc(d.headline), fit(int(B.font * 1.6), inner - 12, d.headline, 0.64), PAPER, bold=True))
        ev.append(_shape(8, a, b, x0 + 6, top + int(B.font * 2.0), min(inner - 12, 18 * len(d.headline) + 60), 6, BLUE,
                         extra="\\fscx0\\t(0,450,\\fscx100)"))

    elif t is TemplateKind.STEPS:
        n = len(d.items)
        row = min(int(area_h / n), int(B.font * 1.6))
        gap = (b - a) * 0.6 / n  # all steps visible by 60% of the element's time
        for i, item in enumerate(d.items):
            ya = top + i * row
            sa = a + i * gap
            ev.append(_shape(8, sa, b, x0 + 6, ya + 2, int(row * 0.78), int(row * 0.78), BLUE))
            ev.append(_text(9, sa, b, x0 + 6 + int(row * 0.39), ya + 2 + int(row * 0.39), str(i + 1), int(row * 0.5), PAPER,
                            bold=True, an=5))
            ev.append(_text(9, sa, b, x0 + 6 + row, ya + int(row * 0.1), esc(item),
                            fit(int(min(B.font * 0.95, row * 0.62)), inner - row - 12, item), PAPER))

    elif t is TemplateKind.COMPARE:
        col = (inner - 24) // 2
        size = int(B.font * 0.85)
        rows = max(len(d.left), len(d.right))
        row = min(int((area_h - size * 1.6) / max(rows, 1)), int(size * 1.5))
        for side, (title, items, x) in enumerate(((d.left_title, d.left, x0 + 6), (d.right_title, d.right, x0 + 30 + col))):
            sa = a + side * min(0.8, (b - a) * 0.15)
            ev.append(_shape(8, sa, b, x, top, col, int(size * 1.4), BLUE if side else "&H00B8A394&"))  # slate vs blue
            ev.append(_text(9, sa, b, x + 14, top + int(size * 0.2), esc(title), fit(size, col - 28, title, 0.64), PAPER, bold=True))
            for i, item in enumerate(items):
                ev.append(_text(9, sa + 0.15 * (i + 1), b, x + 14, top + int(size * 1.7) + i * row,
                                "• " + esc(item), fit(min(int(size * 0.9), int(row * 0.8)), col - 28, "• " + item), PAPER))

    elif t is TemplateKind.BAR:
        n = len(d.bars)
        vmax = max(bar.value for bar in d.bars)
        row = min(int(area_h / n), int(B.font * 2.2))
        label_size = int(min(B.font * 0.8, row * 0.38))
        bar_h = int(row * 0.38)
        longest = max(len(fmt_value(bar.value) + (f" {d.unit}" if d.unit else "")) for bar in d.bars)
        maxw = max(80, inner - 30 - int(longest * bar_h * 0.75))  # leave room for the value label (bold digits ~0.7 em)
        for i, bar in enumerate(d.bars):
            ya = top + i * row
            w = max(6, int(maxw * bar.value / vmax))
            sa = a + 0.12 * i
            ev.append(_text(8, sa, b, x0 + 6, ya, esc(bar.label), fit(label_size, inner - 12, bar.label), PAPER))
            ev.append(_shape(8, sa, b, x0 + 6, ya + label_size + 6, w, bar_h, BLUE if i == n - 1 else "&H00F6A060&",
                             extra="\\fscx0\\t(0,600,\\fscx100)"))
            val = fmt_value(bar.value) + (f" {d.unit}" if d.unit else "")
            ev.append(_text(9, sa + 0.6, b, x0 + 18 + w, ya + label_size + 2, esc(val),
                            fit(int(bar_h * 1.05), B.x + B.w - (x0 + 18 + w) - 12, val, 0.72), PAPER, bold=True))
    return ev


TEMPLATE_FONT = "DejaVu Sans"
TEMPLATE_STYLE = (f"Style: Tpl,{TEMPLATE_FONT},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,"
                  "100,100,0,0,1,0,0,7,0,0,0,1")

EXAMPLES: dict[str, dict] = {
    "key_number": {"kind": "context_card", "template": "key_number", "start": 2.0, "end": 9.0,
                   "text": "The 1940 Tacoma Narrows main span: about nine American football fields between the towers.",
                   "data": {"headline": "853 m", "unit": "main span"}},
    "definition": {"kind": "context_card", "template": "definition", "start": 2.0, "end": 9.0,
                   "text": "A self-feeding twist: each swing changes how the wind pushes, which makes the next swing bigger.",
                   "data": {"headline": "Flutter"}},
    "steps": {"kind": "analysis", "template": "steps", "start": 2.0, "end": 12.0,
              "text": "Try it at home: the flatter the string, the harder you pull. Real main cables keep a deliberate sag.",
              "data": {"items": ["Hang a paper deck from string", "Pull the string ends apart", "Watch the sag shrink as you pull"]}},
    "compare": {"kind": "analysis", "template": "compare", "start": 2.0, "end": 11.0,
                "text": "Same wind, different deck: depth and stiffness changed after 1940, not the strength of the steel.",
                "data": {"left_title": "1940 deck", "left": ["thin", "flexible", "fluttered"],
                         "right_title": "Modern deck", "right": ["deep", "stiff", "wind-tunnel tested"]}},
    "bar": {"kind": "graphic", "template": "bar", "start": 2.0, "end": 10.0,
            "text": "For a solid rectangular section, doubling the depth makes it about eight times stiffer in bending.",
            "data": {"bars": [{"label": "Shallow deck", "value": 1}, {"label": "Twice as deep", "value": 8}],
                     "unit": "× stiffness"}},
}
