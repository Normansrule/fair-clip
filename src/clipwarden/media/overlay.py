"""Everything drawn on top of the clip, written as one Advanced SubStation Alpha (ASS) file.

ASS lets FFmpeg's libass renderer draw animated captions, cards and disclosures in a single
pass. All text that came from a person or a transcript is escaped first, so it can't smuggle in
ASS override tags (for example ``{\\alpha&HFF&}`` would make a disclosure invisible).
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from ..models import ClipPlan, TemplateKind, ValueAddKind, Word
from . import templates as tpl
from .timeline import TimelineMap


# Palette from docs/STYLE.md, converted to ASS &HAABBGGRR (alpha, blue, green, red).
def _ass(hex_rgb: str, alpha: int = 0) -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


INK = "#0B1020"
PAPER = "#FFFFFF"
ACCENT = "#FFB000"  # caption highlight (amber)
CONTEXT = "#2563EB"  # value-add cards (blue)
DISCLOSE = "#FDE047"  # disclosures (yellow, never reused for anything else)
GATE = "#DC2626"


def escape(text: str) -> str:
    """Neutralise ASS syntax: braces start override blocks; backslash starts \\N, \\h, \\n."""
    return (text.replace("\\", "∖").replace("{", "｛").replace("}", "｝")
            .replace("\r", " ").replace("\n", " "))


def ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


@dataclass
class Layout:
    w: int
    h: int
    caption_y: int
    card_y: int
    hook_y: int
    font_caption: int
    font_card: int
    disclose_y: int  # one fixed "disclosure band" so viewers learn where to look


VERTICAL = Layout(1080, 1920, 1500, 330, 180, 76, 46, 1300)
HORIZONTAL = Layout(1920, 1080, 960, 120, 80, 64, 40, 820)


def header(L: Layout) -> str:
    f = "DejaVu Sans"
    styles = [
        # Name, Font, Size, Primary, Secondary, Outline, Back, Bold, Italic, Underline, Strike,
        # ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, ML, MR, MV, Encoding
        f"Style: Caption,{f},{L.font_caption},{_ass(PAPER)},{_ass(ACCENT)},{_ass(INK)},{_ass(INK, 0x60)},1,0,0,0,100,100,0,0,1,6,2,8,60,60,0,1",
        f"Style: Hook,{f},{L.font_caption - 10},{_ass(INK)},{_ass(INK)},{_ass(PAPER)},{_ass(PAPER, 0x10)},1,0,0,0,100,100,0,0,3,18,0,8,70,70,0,1",
        f"Style: Card,{f},{L.font_card},{_ass(PAPER)},{_ass(PAPER)},{_ass(CONTEXT)},{_ass(CONTEXT, 0x10)},0,0,0,0,100,100,0,0,3,22,0,8,90,90,0,1",
        f"Style: CardLabel,{f},{int(L.font_card * 0.62)},{_ass(CONTEXT)},{_ass(CONTEXT)},{_ass(PAPER)},{_ass(PAPER)},1,0,0,0,100,100,2,0,3,10,0,8,90,90,0,1",
        f"Style: Disclose,{f},{L.font_card},{_ass(INK)},{_ass(INK)},{_ass(DISCLOSE)},{_ass(DISCLOSE)},1,0,0,0,100,100,0,0,3,20,0,8,80,80,0,1",
        f"Style: Lower,{f},{int(L.font_card * 0.8)},{_ass(PAPER)},{_ass(PAPER)},{_ass(INK)},{_ass(INK, 0x30)},1,0,0,0,100,100,0,0,3,14,0,1,50,50,0,1",
        f"Style: Fine,{f},{int(L.font_card * 0.6)},{_ass(PAPER)},{_ass(PAPER)},{_ass(INK)},{_ass(INK, 0x40)},0,0,0,0,100,100,0,0,3,8,0,2,40,40,0,1",
        tpl.TEMPLATE_STYLE,
    ]
    return (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {L.w}\nPlayResY: {L.h}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n" + "\n".join(styles) +
        "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def ev(layer: int, a: float, b: float, style: str, text: str, pos: tuple[int, int] | None = None, fade: bool = True) -> str:
    tags = ""
    if pos:
        tags += f"\\pos({pos[0]},{pos[1]})"
    if fade:
        tags += "\\fad(180,180)"
    return f"Dialogue: {layer},{ts(a)},{ts(b)},{style},,0,0,0,,{{{tags}}}{text}"


def wrap(text: str, width: int) -> str:
    return "\\N".join(textwrap.wrap(escape(text), width=width)) or escape(text)


def caption_events(words: list[Word], tmap: TimelineMap, L: Layout, group: int = 3) -> list[str]:
    """Word-by-word captions: groups of `group` words, the spoken word highlighted in amber."""
    mapped = []
    for w in words:
        a, b = tmap.to_clip(w.start), tmap.to_clip(w.end)
        if a is None or b is None:
            continue
        mapped.append((escape(w.text), a, max(b, a + 0.08)))
    out = []
    x, y = L.w // 2, L.caption_y
    for g in range(0, len(mapped), group):
        chunk = mapped[g : g + group]
        for i, (_t, a, b) in enumerate(chunk):
            nxt = chunk[i + 1][1] if i + 1 < len(chunk) else b
            parts = []
            for j, (tt, _, _) in enumerate(chunk):
                if j == i:
                    parts.append(f"{{\\c{_ass(ACCENT)}\\fscx108\\fscy108}}{tt}{{\\r}}")
                else:
                    parts.append(tt)
            out.append(ev(1, a, max(nxt, b), "Caption", " ".join(parts), pos=(x, y), fade=False))
    return out


LABELS = {
    ValueAddKind.COMMENTARY: "MY TAKE",
    ValueAddKind.CONTEXT_CARD: "CONTEXT",
    ValueAddKind.GRAPHIC: "EXPLAINER",
    ValueAddKind.ANALYSIS: "ANALYSIS",
}


def paid_disclosure_text(plan: ClipPlan) -> str:
    m = plan.markets
    if m and m.paid_by:
        return f"Paid promotion: {m.paid_by} paid {m.payment_amount or 'me'} for this video."
    return f"Paid promotion: sponsored by {plan.sponsor}." if plan.sponsor else "Paid promotion: this video is sponsored."


def build_ass(plan: ClipPlan, words: list[Word], tmap: TimelineMap, *, hook_text: str,
              channel: str, attribution: str) -> str:
    L = VERTICAL if plan.aspect == "9:16" else HORIZONTAL
    wrapw = 30 if plan.aspect == "9:16" else 60
    D = tmap.duration
    lines = [header(L)]
    if plan.hook_seconds > 0 and hook_text:
        lines.append(ev(3, 0, plan.hook_seconds, "Hook", wrap(hook_text, 24 if plan.aspect == "9:16" else 44), pos=(L.w // 2, L.hook_y)))
    for va in plan.value_add:
        a, b = min(va.start, D), min(va.end, D)
        if b <= a:
            continue
        if va.template is not TemplateKind.CARD:
            lines.extend(tpl.template_events(va, plan.aspect, D, LABELS[va.kind]))
            continue
        lines.append(ev(2, a, b, "CardLabel", LABELS[va.kind], pos=(L.w // 2, L.card_y)))
        lines.append(ev(2, a, b, "Card", wrap(va.text, wrapw), pos=(L.w // 2, L.card_y + int(L.font_card * 1.55))))
    if plan.lower_third or channel:
        lines.append(ev(2, min(plan.hook_seconds, D), min(plan.hook_seconds + 4.0, D), "Lower",
                        escape(plan.lower_third or channel), pos=(50, L.h - 240 if plan.aspect == "9:16" else L.h - 60)))
    if attribution:
        lines.append(ev(0, 0, D, "Fine", escape(attribution), pos=(L.w // 2, L.h - 40), fade=False))
    if plan.affiliate:
        af = plan.affiliate
        if af.start_card:
            lines.append(ev(4, af.start_card[0], af.start_card[1], "Disclose", wrap(af.disclosure_text, wrapw), pos=(L.w // 2, L.disclose_y)))
        for a, b in af.recommendation_cards:
            lines.append(ev(4, a, b, "Disclose", wrap(af.disclosure_text, wrapw), pos=(L.w // 2, L.disclose_y)))
    if plan.paid_disclosure_card:
        a, b = plan.paid_disclosure_card
        lines.append(ev(4, a, min(b, D), "Disclose", wrap(paid_disclosure_text(plan), wrapw), pos=(L.w // 2, L.disclose_y)))
    if plan.markets:
        m = plan.markets
        if m.disclaimer_on_screen:
            lines.append(ev(4, 0, D, "Fine", escape("Educational only. Not investment advice."), pos=(L.w // 2, L.h - 90), fade=False))
        if m.data_points:
            src = "; ".join(f"{d.label}: {d.value} ({d.source_name}, {d.retrieved_at:%Y-%m-%d %H:%M} UTC)" for d in m.data_points[:4])
            lines.append(ev(2, max(0.0, D - 5.0), D, "Card", wrap("Sources: " + src, wrapw), pos=(L.w // 2, L.card_y)))
    lines.extend(caption_events(words, tmap, L))
    return "\n".join(lines) + "\n"
