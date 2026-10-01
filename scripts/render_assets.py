#!/usr/bin/env python3
"""Regenerate every vector visual in assets/ from source (this script + assets/src/*).

    python scripts/render_assets.py           # write assets
    python scripts/render_assets.py --check   # CI: fail if any committed asset is stale

Vector assets are deterministic (fixed SVG hash salt, no timestamps), so --check compares bytes.
Raster assets (hero GIF, screenshots, frame composites) come from scripts/render_media.py,
which needs the demo output and a browser; --check only verifies they exist and are small.
"""

from __future__ import annotations

import json
import math
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clipwarden.gate.reasons import GROUP_ORDER, REASONS  # noqa: E402
from clipwarden.scoring import FEATURES, normalise, step_toward  # noqa: E402

ASSETS = ROOT / "assets"
SRC = ASSETS / "src"
PAL = json.loads((SRC / "palette.json").read_text())
FONT = "DejaVu Sans, Verdana, Segoe UI, sans-serif"
MONO = "DejaVu Sans Mono, Menlo, Consolas, monospace"
OUT: dict[str, str | bytes] = {}

STAGES = ["Rights intake", "Transcribe", "Highlights", "Edit", "Value-add", "Metadata", "Compliance gate",
          "Human approval", "Publish", "Analytics"]
STAGE_NOTE = ["no record = refuse", "word timestamps", "ranked by score S", "reframe · captions", "required, original",
              "grounded in transcript", "automated · blocking", "a person decides", "scheduled, private first", "feeds weights back"]


def svg_open(w: int, h: int, label: str, p: dict) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="{escape(label)}" font-family="{FONT}"><title>{escape(label)}</title>'
            f'<rect width="{w}" height="{h}" rx="16" fill="{p["bg"]}"/>')


def arrow_marker(mid: str, color: str) -> str:
    return (f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')


# ------------------------------------------------------------------------------------------ 1. pipeline
def pipeline_svg(mode: str) -> str:
    p = PAL[mode]
    W, H = 1200, 410
    bw, bh, gap = 190, 92, 36
    x0 = (W - (5 * bw + 4 * gap)) / 2
    rows_y = [74, 254]
    pos = []
    for i in range(10):
        r, c = divmod(i, 5)
        pos.append((x0 + c * (bw + gap), rows_y[r]))
    out = [svg_open(W, H, "Animated diagram of the ten pipeline stages with the compliance gate and human approval as hard stops", p)]
    out.append(f"<defs>{arrow_marker('a', p['muted'])}{arrow_marker('g', p['gate'])}</defs>")
    # connectors
    for i in range(9):
        (xa, ya), (xb, yb) = pos[i], pos[i + 1]
        if i == 4:  # row break: down from stage 5 then across to stage 6
            out.append(f'<path d="M{xa + bw / 2},{ya + bh} V{(ya + bh + yb) / 2} H{xb + bw / 2} V{yb - 4}" fill="none" stroke="{p["muted"]}" stroke-width="2" marker-end="url(#a)"/>')
        else:
            out.append(f'<line x1="{xa + bw}" y1="{ya + bh / 2}" x2="{xb - 4}" y2="{yb + bh / 2}" stroke="{p["muted"]}" stroke-width="2" marker-end="url(#a)"/>')
    # feedback loop 10 -> 3 (weights, human-approved)
    (xa, ya), (xb, yb) = pos[9], pos[2]
    lx = xa + bw + 26
    out.append(f'<path d="M{xa + bw},{ya + bh / 2} H{lx} V{yb - 34} H{xb + bw / 2} V{yb - 4}" fill="none" stroke="{p["flow"]}" stroke-width="2" stroke-dasharray="6 6" marker-end="url(#a)"/>')
    out.append(f'<text x="{(xb + bw / 2 + lx) / 2}" y="{yb - 42}" text-anchor="middle" font-size="13" fill="{p["flow"]}">feedback loop: analytics propose new score weights, a person approves them</text>')
    # boxes
    for i, ((x, y), name) in enumerate(zip(pos, STAGES, strict=True)):
        hard = i in (6, 7)
        stroke = p["gate"] if hard else p["line"]
        fill = p["gate_soft"] if hard else p["panel"]
        out.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="{3 if hard else 1.5}"/>')
        out.append(f'<rect class="glow s{i}" x="{x - 4}" y="{y - 4}" width="{bw + 8}" height="{bh + 8}" rx="15" fill="none" stroke="{p["highlight_fill"]}" stroke-width="4" opacity="0"/>')
        out.append(f'<text x="{x + 14}" y="{y + 26}" font-size="13" font-family="{MONO}" fill="{p["muted"]}">{i + 1:02d}</text>')
        if hard:
            out.append(f'<rect x="{x + bw - 30}" y="{y + 12}" width="16" height="16" rx="2" fill="{p["gate"]}"/>')
            out.append(f'<text x="{x + bw - 36}" y="{y + 25}" text-anchor="end" font-size="11" font-weight="700" fill="{p["gate"]}">HARD STOP</text>')
        out.append(f'<text x="{x + 14}" y="{y + 52}" font-size="17" font-weight="700" fill="{p["ink"]}">{escape(name)}</text>')
        out.append(f'<text x="{x + 14}" y="{y + 74}" font-size="13" fill="{p["muted"]}">{escape(STAGE_NOTE[i])}</text>')
    # the travelling clip token: keyframes computed from geometry so it stops on each stage
    pts = [(x + bw / 2, y - 14) for x, y in pos]
    path = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in pts)
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(9)]
    total = sum(seg)
    hold = [0.35] * 10
    hold[6], hold[7] = 1.1, 1.3
    move = 0.45
    T = sum(hold) + 9 * move + 0.8
    kf, glows, t, d = [], {i: [] for i in range(10)}, 0.0, 0.0
    for i in range(10):
        a, b = t / T * 100, (t + hold[i]) / T * 100
        kf += [f"{a:.2f}% {{ offset-distance: {d / total * 100:.3f}%; }}", f"{b:.2f}% {{ offset-distance: {d / total * 100:.3f}%; }}"]
        glows[i] = [a, b]
        t += hold[i]
        if i < 9:
            t += move
            d += seg[i]
    kf.append("100% { offset-distance: 100%; }")
    css = [f".tok {{ offset-path: path('{path}'); offset-rotate: 0deg; animation: travel {T:.2f}s ease-in-out infinite; }}",
           "@keyframes travel { " + " ".join(kf) + " }",
           f".tokfill {{ animation: tint {T:.2f}s step-end infinite; }}",
           f"@keyframes tint {{ 0% {{ fill: {p['flow']}; }} {glows[7][1]:.2f}% {{ fill: {p['pass']}; }} 98% {{ fill: {p['pass']}; }} }}"]
    for i, (a, b) in glows.items():
        css.append(f".s{i} {{ animation: g{i} {T:.2f}s linear infinite; }} @keyframes g{i} {{ 0%, {max(0, a - 0.5):.2f}% {{ opacity: 0; }} "
                   f"{a:.2f}%, {b:.2f}% {{ opacity: 1; }} {min(100, b + 1.5):.2f}%, 100% {{ opacity: 0; }} }}")
    css.append("@media (prefers-reduced-motion: reduce) { .tok, .tokfill, .glow { animation: none !important; } .tok { offset-distance: 66%; } }")
    out.insert(1, "<style>" + "\n".join(css) + "</style>")
    out.append(f'<g class="tok"><circle class="tokfill" r="11" fill="{p["flow"]}" stroke="{p["panel"]}" stroke-width="3"/>'
               f'<rect x="-5" y="-4" width="10" height="8" rx="1.5" fill="{p["panel"]}"/></g>')
    out.append(f'<text x="{W / 2}" y="{H - 14}" text-anchor="middle" font-size="13" fill="{p["muted"]}">The token is one clip. It cannot pass ■ stage 7 with any block, or ■ stage 8 without a person clicking Approve.</text>')
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------------------------------ 2. security boundary
def security_svg(mode: str) -> str:
    p = PAL[mode]
    W, H = 1200, 700
    o = [svg_open(W, H, "Security boundary diagram: what runs sandboxed, where tokens live, and what reaches Google", p)]
    o.append(f"<defs>{arrow_marker('m', p['ink'])}{arrow_marker('r', p['gate'])}{arrow_marker('t', p['google'])}</defs>")

    def box(x, y, w, h, title, lines, stroke=None, fill=None, tsize=16, icon=""):
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill or p["panel"]}" stroke="{stroke or p["line"]}" stroke-width="2"/>')
        o.append(f'<text x="{x + 14}" y="{y + 26}" font-size="{tsize}" font-weight="700" fill="{p["ink"]}">{icon}{escape(title)}</text>')
        for k, line in enumerate(lines):
            o.append(f'<text x="{x + 14}" y="{y + 49 + k * 19}" font-size="13" fill="{p["muted"]}">{escape(line)}</text>')

    def zone(x, y, w, h, title, sub, stroke, fill="none", dash=None):
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="2.5"'
                 + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
        o.append(f'<text x="{x + 18}" y="{y + 30}" font-size="18" font-weight="800" fill="{p["ink"]}">{escape(title)}</text>')
        o.append(f'<text x="{x + 18}" y="{y + 51}" font-size="12.5" fill="{p["muted"]}">{escape(sub)}</text>')

    zone(16, 30, 176, 640, "Untrusted", "treated as data, never", p["gate"], dash="8 6")
    o.append(f'<text x="34" y="98" font-size="12.5" fill="{p["muted"]}">as instructions</text>')
    zone(208, 30, 640, 640, "Your computer", "no inbound ports · dashboard on 127.0.0.1 only", p["ink"])
    zone(866, 30, 318, 640, "Google", "your own Cloud project per deployment", p["google"], fill=p["google_soft"])

    # column A: pipeline, sandbox
    box(230, 100, 290, 150, "Pipeline + gate", ["strict schemas: unknown = reject", "model output = untrusted data", "links must match the allowlist", "injection phrases flagged", "approval binds file + metadata hash"])
    o.append(f'<rect x="230" y="276" width="290" height="320" rx="14" fill="{p["sandbox_soft"]}" stroke="{p["sandbox"]}" stroke-width="2.5" stroke-dasharray="9 6"/>')
    o.append(f'<text x="246" y="304" font-size="16" font-weight="800" fill="{p["sandbox"]}">Media sandbox (Docker)</text>')
    for k, line in enumerate(["no network (--network none)", "read-only root, all caps dropped", "no-new-privileges, non-root user",
                              "memory / CPU / process limits", "one job folder, inputs read-only", "FFmpeg: file protocol only"]):
        o.append(f'<text x="246" y="{330 + k * 20}" font-size="13" fill="{p["ink"]}">{escape(line)}</text>')
    box(250, 472, 250, 50, "FFmpeg · FFprobe", [], fill=p["panel"], stroke=p["sandbox"], tsize=15)
    o.append(f'<text x="246" y="550" font-size="12.5" fill="{p["muted"]}">outputs are hashed (SHA-256) before</text>')
    o.append(f'<text x="246" y="568" font-size="12.5" fill="{p["muted"]}">anything else trusts them</text>')
    # column B: dashboard, vault, state, llm
    box(540, 100, 290, 130, "Dashboard (FastAPI)", ["Host-header allowlist (anti-rebinding)", "password (scrypt) + signed cookie", "CSRF token on every POST", "strict CSP, no inline script"])
    box(540, 246, 290, 112, "Token vault", ["OS keychain, or AES-GCM file", "key = scrypt(passphrase)", "never logged · revocable"], stroke=p["highlight"], icon="🔒 ")
    box(540, 374, 290, 130, "State + audit log", ["SQLite, content-addressed media", "append-only, hash-chained log", "UPDATE/DELETE refused by triggers", "dry-run by default, kill switch"])
    box(540, 520, 290, 76, "Optional local LLM (127.0.0.1)", ["proposes text only; cannot act"], tsize=14)
    o.append(f'<text x="228" y="640" font-size="13" fill="{p["muted"]}">Approve/reject happens here, by a person. Nothing public without it.</text>')

    # untrusted inputs -> targets (horizontal lanes, no crossings)
    for label, sub, y, x_to in (("Transcript text", "what was said", 156, 230), ("Comments", "(future milestone)", 232, 230),
                                ("Source video file", "magic bytes checked first", 497, 250)):
        o.append(f'<text x="34" y="{y - 6}" font-size="14" font-weight="700" fill="{p["gate"]}">{escape(label)}</text>')
        o.append(f'<text x="34" y="{y + 30}" font-size="12" fill="{p["muted"]}">{escape(sub)}</text>')
        o.append(f'<path d="M34,{y + 8} H{x_to - 4}" stroke="{p["gate"]}" stroke-width="2.5" stroke-dasharray="7 5" fill="none" marker-end="url(#r)"/>')

    # Google side
    box(886, 100, 280, 130, "OAuth 2.0 sign-in", ["installed-app flow, loopback", "PKCE (S256) + state check", "one scope: youtube.upload"], fill=p["panel"], stroke=p["google"])
    box(886, 300, 280, 150, "YouTube Data API v3", ["videos.insert, only after approval", "privacy + publishAt in the", "same call: no broad 'manage", "account' scope ever needed"], fill=p["panel"], stroke=p["google"])
    box(886, 486, 280, 110, "YouTube Analytics", ["read-only scope, opt-in", "(milestone 2)"], fill=p["panel"], stroke=p["google"])
    # flows
    o.append(f'<path d="M830,160 H882" stroke="{p["google"]}" stroke-width="2.5" fill="none" marker-end="url(#t)"/>')
    o.append(f'<path d="M830,300 C858,300 858,215 882,215" stroke="{p["google"]}" stroke-width="2" stroke-dasharray="3 4" fill="none" marker-end="url(#t)"/>')
    o.append(f'<path d="M830,430 C858,430 858,375 882,375" stroke="{p["ink"]}" stroke-width="3.5" fill="none" marker-end="url(#m)"/>')
    o.append(f'<path d="M886,541 C850,541 858,470 834,470" stroke="{p["google"]}" stroke-width="2" fill="none" marker-end="url(#t)"/>')
    o.append(f'<text x="886" y="268" font-size="12.5" fill="{p["google"]}">⇠ refresh token goes only to Google’s</text>')
    o.append(f'<text x="886" y="285" font-size="12.5" fill="{p["google"]}">   token endpoint, over HTTPS</text>')
    o.append(f'<text x="886" y="470" font-size="12.5" font-weight="700" fill="{p["ink"]}">⇠ the approved clip + its exact metadata</text>')
    o.append(f'<text x="886" y="620" font-size="12.5" fill="{p["muted"]}">⇢ retention numbers come back read-only</text>')
    # legend
    o.append(f'<line x1="34" y1="610" x2="80" y2="610" stroke="{p["gate"]}" stroke-width="2.5" stroke-dasharray="7 5"/><text x="88" y="614" font-size="12" fill="{p["muted"]}">untrusted</text>')
    o.append(f'<line x1="34" y1="634" x2="80" y2="634" stroke="{p["sandbox"]}" stroke-width="2.5" stroke-dasharray="9 6"/><text x="88" y="638" font-size="12" fill="{p["muted"]}">isolation</text>')
    o.append(f'<line x1="34" y1="658" x2="80" y2="658" stroke="{p["google"]}" stroke-width="2.5"/><text x="88" y="662" font-size="12" fill="{p["muted"]}">to/from Google</text>')
    o.append("</svg>")
    return "".join(o)


# ------------------------------------------------------------------------------------------ 3. gate flowchart
def gate_svg(mode: str) -> str:
    p = PAL[mode]
    groups = [(g, [(c, v) for c, v in REASONS.items() if v[0] == g]) for g in GROUP_ORDER]
    row_h = [max(56, 20 + 19 * len(items)) for _, items in groups]
    W = 1200
    H = 150 + sum(row_h) + 26 * len(groups) + 120
    o = [svg_open(W, H, "Compliance gate flowchart showing every block and warning reason", p)]
    o.append(f"<defs>{arrow_marker('d', p['pass'])}{arrow_marker('x', p['gate'])}</defs>")
    o.append(f'<text x="40" y="50" font-size="22" font-weight="800" fill="{p["ink"]}">Compliance gate: every check runs, every reason is reported</text>')
    o.append(f'<text x="40" y="76" font-size="14" fill="{p["muted"]}">✕ = blocks the clip (it can never reach approval) · ⚠ = warning the reviewer must acknowledge · generated from gate/reasons.py</text>')
    o.append(f'<rect x="40" y="96" width="300" height="40" rx="20" fill="{p["flow_soft"]}" stroke="{p["flow"]}" stroke-width="2"/>')
    o.append(f'<text x="190" y="122" text-anchor="middle" font-size="15" font-weight="700" fill="{p["ink"]}">Rendered clip + metadata</text>')
    y = 162
    for (g, items), h in zip(groups, row_h, strict=True):
        o.append(f'<line x1="190" y1="{y - 26}" x2="190" y2="{y - 4}" stroke="{p["pass"]}" stroke-width="2.5" marker-end="url(#d)"/>')
        o.append(f'<rect x="40" y="{y}" width="300" height="{h}" rx="10" fill="{p["panel"]}" stroke="{p["line"]}" stroke-width="1.5"/>')
        o.append(f'<text x="58" y="{y + h / 2 + 6}" font-size="16" font-weight="700" fill="{p["ink"]}">{escape(g)}</text>')
        nb = sum(1 for _, v in items if v[1] == "block")
        o.append(f'<text x="322" y="{y + h / 2 + 5}" text-anchor="end" font-size="12" fill="{p["muted"]}">{nb} block · {len(items) - nb} warn</text>')
        o.append(f'<line x1="340" y1="{y + h / 2}" x2="392" y2="{y + h / 2}" stroke="{p["gate"]}" stroke-width="2" marker-end="url(#x)"/>')
        for k, (code, v) in enumerate(items):
            ty = y + 24 + k * 19 + (h - 20 - 19 * len(items)) / 2
            sym, col = ("✕", p["gate"]) if v[1] == "block" else ("⚠", p["highlight"])
            o.append(f'<text x="404" y="{ty}" font-size="13" font-weight="700" fill="{col}">{sym}</text>')
            o.append(f'<text x="424" y="{ty}" font-size="13" font-family="{MONO}" fill="{p["ink"]}">{escape(code)}</text>')
            o.append(f'<text x="800" y="{ty}" font-size="13" fill="{p["muted"]}">{escape(v[2])}</text>')
        y += h + 26
    o.append(f'<line x1="190" y1="{y - 26}" x2="190" y2="{y - 4}" stroke="{p["pass"]}" stroke-width="2.5" marker-end="url(#d)"/>')
    o.append(f'<rect x="40" y="{y}" width="300" height="48" rx="10" fill="{p["gate_soft"]}" stroke="{p["gate"]}" stroke-width="3"/>')
    o.append(f'<rect x="58" y="{y + 16}" width="16" height="16" rx="2" fill="{p["gate"]}"/>')
    o.append(f'<text x="82" y="{y + 30}" font-size="14.5" font-weight="800" fill="{p["ink"]}">Human approval (hard stop)</text>')
    o.append(f'<text x="360" y="{y + 30}" font-size="14" fill="{p["muted"]}">zero blocks AND warnings acknowledged AND a person clicks Approve</text>')
    o.append("</svg>")
    return "".join(o)


def gate_mermaid() -> str:
    lines = ["flowchart TD", '  IN(["Rendered clip + metadata"]) --> G0']
    for i, g in enumerate(GROUP_ORDER):
        items = [(c, v) for c, v in REASONS.items() if v[0] == g]
        blocks = [c for c, v in items if v[1] == "block"]
        warns = [c for c, v in items if v[1] == "warn"]
        lines.append(f'  G{i}{{"{g}"}}')
        nxt = f"G{i + 1}" if i + 1 < len(GROUP_ORDER) else "HA"
        lines.append(f'  G{i} -->|pass| {nxt}')
        if blocks:
            lines.append(f'  G{i} -->|block| B{i}["✕#160;' + "<br/>✕#160;".join(blocks) + '"]')  # #160; = no-break space
        if warns:
            lines.append(f'  G{i} -.->|warn| W{i}["⚠#160;' + "<br/>⚠#160;".join(warns) + '"]')
    lines.append('  HA[["■ Human approval"]] -->|approve| UP(["Upload: private or scheduled"])')
    lines.append("  classDef block fill:#FDE2E2,stroke:#C81E1E,color:#0B1020")
    lines.append("  classDef warn fill:#FFF4D6,stroke:#9A5B00,color:#0B1020")
    lines.append("  classDef stop fill:#FDE2E2,stroke:#C81E1E,stroke-width:3px,color:#0B1020")
    bl = ",".join(f"B{i}" for i, g in enumerate(GROUP_ORDER) if any(v[0] == g and v[1] == "block" for v in REASONS.values()))
    wl = ",".join(f"W{i}" for i, g in enumerate(GROUP_ORDER) if any(v[0] == g and v[1] == "warn" for v in REASONS.values()))
    lines += [f"  class {bl} block", f"  class {wl} warn", "  class HA stop"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------ 4. equation charts
def mpl(mode: str):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    p = PAL[mode]
    plt.rcParams.update({
        "svg.hashsalt": "clipwarden", "svg.fonttype": "path", "font.family": "DejaVu Sans", "font.size": 11,
        "figure.facecolor": p["bg"], "axes.facecolor": p["bg"], "axes.edgecolor": p["line"], "axes.labelcolor": p["ink"],
        "xtick.color": p["muted"], "ytick.color": p["muted"], "text.color": p["ink"], "axes.grid": True, "grid.color": p["grid"],
        "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    })
    return plt, p


def save(fig, name: str) -> None:
    import io

    buf = io.StringIO()
    fig.savefig(buf, format="svg", metadata={"Date": None, "Creator": None}, bbox_inches="tight", pad_inches=0.25)
    import matplotlib.pyplot as plt

    plt.close(fig)
    OUT[name] = buf.getvalue()


def eq1_charts(mode: str) -> None:
    plt, p = mpl(mode)
    data = json.loads((SRC / "eq1-candidates.json").read_text())
    w = normalise(data["weights"])
    cands = data["candidates"]
    labels = [f"#{c['rank']}  {c['start']:.0f}–{c['end']:.0f} s" for c in cands]
    sign = {"energy": 1, "relevance": 1, "rate": 1, "scene": 1, "silence": -1}
    names = {"energy": "w_E·z(E) energy", "relevance": "w_R·R relevance", "rate": "w_r·z(r) speech rate", "scene": "w_D·D scene cuts", "silence": "−w_Q·Q silence"}
    colors = {"energy": p["highlight_fill"], "relevance": p["flow"], "rate": p["google"], "scene": p["sandbox"], "silence": p["muted"]}
    fig, ax = plt.subplots(figsize=(9.5, 3.9))
    for i, c in enumerate(cands):
        pos, neg = 0.0, 0.0
        for k in FEATURES:
            v = sign[k] * w[k] * c["z"][k]
            left = pos if v >= 0 else neg + v
            ax.barh(i, abs(v) if v >= 0 else -v, left=left if v >= 0 else neg + v, color=colors[k], height=0.62,
                    label=names[k] if i == 0 else None, edgecolor=p["bg"], linewidth=1)
            if v >= 0:
                pos += v
            else:
                neg += v
        ax.plot([c["score"]], [i], marker="D", color=p["ink"], markersize=7, zorder=5)
        ax.text(max(pos, c["score"]) + 0.03, i, f"S = {c['score']:+.2f}", va="center", fontsize=11, color=p["ink"], fontweight="bold")
    ax.axvline(0, color=p["ink"], linewidth=1)
    ax.set_yticks(range(len(cands)), labels)
    ax.invert_yaxis()
    ax.set_xlabel("contribution to highlight score S (dimensionless)")
    ax.set_xlim(min(-0.6, ax.get_xlim()[0]), ax.get_xlim()[1] + 0.35)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.2), fontsize=10)
    ax.set_title("Why each candidate ranked where it did (◆ = total S)", loc="left", fontsize=13, fontweight="bold")
    save(fig, f"eq1-contributions-{mode}.svg")

    # sensitivity: how the ranking changes as the energy weight moves (others rescaled)
    fig, ax = plt.subplots(figsize=(9.5, 3.6))
    grid = [0.05 + i * 0.025 for i in range(23)]
    ranks = {c["candidate_id"]: [] for c in cands}
    knob = "energy"
    for wr in grid:
        ww = dict(w)
        rest = 1 - wr
        other = sum(v for k, v in w.items() if k != knob)
        for k in ww:
            ww[k] = wr if k == knob else w[k] * rest / other
        scores = {c["candidate_id"]: sum(sign[k] * ww[k] * c["z"][k] for k in FEATURES) for c in cands}
        order = sorted(scores, key=scores.get, reverse=True)
        for cid in ranks:
            ranks[cid].append(order.index(cid) + 1)
    palette = [p["flow"], p["highlight_fill"], p["google"], p["sandbox"], p["muted"]]
    for (_cid, r), col, c in zip(ranks.items(), palette, cands, strict=False):
        ax.plot(grid, r, color=col, linewidth=2.6, marker="o", markersize=3.5)
        ax.text(grid[-1] + 0.01, r[-1], f"{c['start']:.0f}–{c['end']:.0f} s", va="center", fontsize=10, color=col)
    ax.axvline(w[knob], color=p["ink"], linestyle="--", linewidth=1)
    ax.text(w[knob] + 0.005, 0.7, f"current w_E = {w[knob]:.2f}", fontsize=10, color=p["ink"])
    ax.set_ylim(len(cands) + 0.5, 0.5)
    ax.set_yticks(range(1, len(cands) + 1), [f"rank {i}" for i in range(1, len(cands) + 1)])
    ax.set_xlabel("energy weight w_E (other weights rescaled to keep Σw = 1)")
    ax.set_title("Turn one knob: move w_E and the order of #1–#3 changes", loc="left", fontsize=13, fontweight="bold")
    save(fig, f"eq1-sensitivity-{mode}.svg")


def eq2_funnel(mode: str) -> None:
    plt, p = mpl(mode)
    fig, ax = plt.subplots(figsize=(9.5, 3.0))
    steps = [("Impressions", 12000, "shown in a feed"), ("Clicks", 420, "CTR = 420 / 12 000 = 3.5 %"),
             ("Affiliate clicks", 300, "clicked a link in the description"), ("Conversions", 12, "CR = 12 / 300 = 4 %")]
    cols = [p["flow"], p["highlight_fill"], p["google"], p["pass"]]
    for i, (name, n, note) in enumerate(steps):
        width = 0.08 + 0.92 * math.log10(n) / math.log10(12000)
        ax.barh(i, width, height=0.66, color=cols[i])
        ax.text(-0.02, i, f"{name}  {n:,}", ha="right", va="center", fontsize=12, fontweight="bold", color=p["ink"])
        ax.text(width + 0.02, i, note, va="center", fontsize=11, color=p["ink"])
    ax.set_xlim(-0.5, 1.75)
    ax.set_ylim(3.6, -0.6)
    ax.axis("off")
    ax.set_title(r"Clip funnel (bar length on a log scale) · EPC = \$18.60 / 300 clicks = \$0.062", loc="left", fontsize=13, fontweight="bold")
    save(fig, f"eq2-funnel-{mode}.svg")


def eq3_retention(mode: str) -> None:
    plt, p = mpl(mode)
    import numpy as np

    tau = np.linspace(0, 1, 81)
    r = 1.0 - 0.16 * (1 - np.exp(-tau * 18)) - 0.22 * tau - 0.10 / (1 + np.exp(-(tau - 0.55) * 40)) + 0.07 * np.exp(-((tau - 0.3) ** 2) / 0.002)
    area = float(np.trapezoid(r, tau))
    fig, ax = plt.subplots(figsize=(9.5, 3.8))
    ax.fill_between(tau, 0, r, color=p["flow"], alpha=0.18)
    ax.plot(tau, r, color=p["flow"], linewidth=3)
    i = int(np.argmin(np.abs(tau - 0.56)))
    ax.annotate("sharp drop mid-clip:\nlook at the edit here", (tau[i], r[i]), (0.62, 0.25), color=p["ink"], fontsize=10.5,
                arrowprops={"arrowstyle": "->", "color": p["gate"], "linewidth": 1.8})
    ax.annotate("rewatch bump", (0.3, r[24]), (0.33, 1.05), color=p["ink"], fontsize=10.5, arrowprops={"arrowstyle": "->", "color": p["highlight"], "linewidth": 1.8})
    ax.annotate("hook swipe-away", (0.06, r[5]), (0.08, 0.55), color=p["ink"], fontsize=10.5, arrowprops={"arrowstyle": "->", "color": p["muted"], "linewidth": 1.5})
    ax.text(0.12, 0.18, f"shaded area ≈ average % viewed (APV) ≈ {area:.0%}", fontsize=12, fontweight="bold", color=p["flow"])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.15)
    ax.set_xlabel("τ = position in clip (share of duration)")
    ax.set_ylabel("r(τ) = share of viewers still watching")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_title("Audience retention r(τ) — illustrative curve", loc="left", fontsize=13, fontweight="bold")
    save(fig, f"eq3-retention-{mode}.svg")


def eq4_bounded(mode: str) -> None:
    plt, p = mpl(mode)
    cur = {"energy": 0.25, "relevance": 0.35, "rate": 0.15, "scene": 0.10, "silence": 0.15}
    target = {"energy": 0.45, "relevance": 0.25, "rate": 0.10, "scene": 0.10, "silence": 0.10}
    new = step_toward(cur, target)
    fig, ax = plt.subplots(figsize=(9.5, 3.6))
    xs = range(len(FEATURES))
    ax.bar([x - 0.27 for x in xs], [cur[k] for k in FEATURES], 0.26, color=p["muted"], label="current")
    ax.bar(list(xs), [new[k] for k in FEATURES], 0.26, color=p["flow"], label="proposed (after cap)")
    ax.bar([x + 0.27 for x in xs], [target[k] for k in FEATURES], 0.26, color=p["highlight_fill"], alpha=0.55, label="what the data wanted", hatch="//", edgecolor=p["highlight"])
    for x, k in zip(xs, FEATURES, strict=True):
        ax.errorbar(x, cur[k], yerr=0.05, fmt="none", ecolor=p["gate"], capsize=6, linewidth=1.5)
    ax.axhline(0.60, color=p["gate"], linestyle=":", linewidth=1.5)
    ax.text(4.45, 0.612, "W_MAX = 0.60", ha="right", fontsize=10, color=p["gate"])
    ax.set_xticks(list(xs), ["energy w_E", "relevance w_R", "rate w_r", "scene w_D", "silence w_Q"])
    ax.set_ylabel("weight")
    ax.set_ylim(0, 0.65)
    ax.legend(loc="upper center", ncol=3, fontsize=10, bbox_to_anchor=(0.5, -0.12))
    ax.set_title("One approved update: no weight moves more than ±0.05 (red whiskers)", loc="left", fontsize=13, fontweight="bold")
    save(fig, f"eq4-bounded-{mode}.svg")


def eq5_coverage(mode: str) -> None:
    plt, p = mpl(mode)
    fig, ax = plt.subplots(figsize=(9.5, 1.9))
    D = 23.8
    ax.barh(0, D, color=p["grid"], height=0.5)
    for (a, b), lab in (((2.2, 10.0), "context card"), ((12.0, 21.0), "my take")):
        ax.barh(0, b - a, left=a, color=p["flow"], height=0.5)
        ax.text((a + b) / 2, 0, lab, ha="center", va="center", color=p["on_accent"], fontsize=11, fontweight="bold")
    ax.barh(0, 2.0, left=0, color=p["highlight_fill"], height=0.5)
    ax.text(1.0, 0, "hook", ha="center", va="center", fontsize=10, color="#0B1020")
    ax.grid(False)
    ax.set_xlim(0, D)
    ax.set_yticks([])
    ax.set_xlabel("clip time (s)")
    ax.set_title("Value-add coverage = (7.8 s + 9.0 s) / 23.8 s = 71 %  (minimum 25 %)", loc="left", fontsize=13, fontweight="bold")
    save(fig, f"eq5-coverage-{mode}.svg")


# ------------------------------------------------------------------------------------------ main
def build() -> None:
    for mode in ("light", "dark"):
        OUT[f"pipeline-{mode}.svg"] = pipeline_svg(mode)
        OUT[f"security-{mode}.svg"] = security_svg(mode)
        OUT[f"gate-flow-{mode}.svg"] = gate_svg(mode)
        eq1_charts(mode)
        eq2_funnel(mode)
        eq3_retention(mode)
        eq4_bounded(mode)
        eq5_coverage(mode)
    OUT["src/gate-flow.mmd"] = gate_mermaid()


def inject_readme(check: bool) -> bool:
    readme = ROOT / "README.md"
    if not readme.exists():
        return True
    text = readme.read_text()
    start, end = "<!-- gate-mermaid:start -->", "<!-- gate-mermaid:end -->"
    if start not in text:
        return True
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    new = f"{head}{start}\n```mermaid\n{OUT['src/gate-flow.mmd']}```\n{end}{tail}"
    if check:
        return new == text
    readme.write_text(new)
    return True


RASTER = {"hero.gif": 5_000_000, "before-after.png": 2_000_000, "disclosures.png": 2_000_000,
          "dashboard-review-light.png": 2_000_000, "dashboard-review-dark.png": 2_000_000,
          "dashboard-pipeline-light.png": 2_000_000, "dashboard-analytics-light.png": 2_000_000, "dashboard-audit-light.png": 2_000_000}


def main() -> int:
    check = "--check" in sys.argv
    build()
    stale = []
    for name, content in OUT.items():
        path = ASSETS / name
        data = content.encode() if isinstance(content, str) else content
        if check:
            if not path.exists() or path.read_bytes() != data:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if name.endswith(".svg") and len(data) > 500_000:
            stale.append(f"{name} is {len(data)} bytes (> 500 KB limit)")
    if not inject_readme(check):
        stale.append("README.md gate flowchart block")
    for name, limit in RASTER.items():
        path = ASSETS / name
        if check and (not path.exists() or path.stat().st_size > limit):
            stale.append(f"{name} missing or over {limit // 1_000_000} MB")
    if stale:
        print("stale or invalid assets (run: python scripts/render_assets.py):\n  " + "\n  ".join(stale))
        return 1
    print(f"{'checked' if check else 'wrote'} {len(OUT)} vector assets" + (" + README flowchart" if not check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
