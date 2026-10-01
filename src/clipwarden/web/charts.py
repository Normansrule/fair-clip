"""Server-rendered SVG charts (no JavaScript needed, so the strict CSP stays strict)."""

from __future__ import annotations

from html import escape


def retention_svg(tau: list[float], ratio: list[float], notes: list[tuple[float, str]] | None = None,
                  w: int = 720, h: int = 300, colors: dict[str, str] | None = None) -> str:
    """Audience-retention curve r(τ) with the area under it shaded (≈ average % viewed)."""
    c = {"line": "var(--accent)", "fill": "var(--accent-soft)", "grid": "var(--grid)", "ink": "var(--ink)",
         "muted": "var(--muted)", "note": "var(--gate)"} | (colors or {})
    L, R, T, B = 78, 20, 30, 44
    pw, ph = w - L - R, h - T - B
    ymax = max(1.2, max(ratio) * 1.05 if ratio else 1.2)
    X = lambda t: L + t * pw  # noqa: E731
    Y = lambda r: T + ph - (r / ymax) * ph  # noqa: E731
    pts = " ".join(f"{X(t):.1f},{Y(r):.1f}" for t, r in zip(tau, ratio, strict=True))
    area = f"{X(tau[0]):.1f},{Y(0):.1f} {pts} {X(tau[-1]):.1f},{Y(0):.1f}" if tau else ""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="Audience retention curve" class="chart">']
    for yv in (0, 0.25, 0.5, 0.75, 1.0):
        out.append(f'<line x1="{L}" x2="{w - R}" y1="{Y(yv):.1f}" y2="{Y(yv):.1f}" stroke="{c["grid"]}" stroke-width="1"/>')
        out.append(f'<text x="{L - 8}" y="{Y(yv) + 4:.1f}" text-anchor="end" font-size="12" fill="{c["muted"]}">{int(yv * 100)}%</text>')
    for xv in (0, 0.25, 0.5, 0.75, 1.0):
        out.append(f'<text x="{X(xv):.1f}" y="{h - B + 18}" text-anchor="middle" font-size="12" fill="{c["muted"]}">{int(xv * 100)}%</text>')
    out.append(f'<text x="{L + pw / 2:.1f}" y="{h - 6}" text-anchor="middle" font-size="12" fill="{c["muted"]}">position in clip (τ, share of duration)</text>')
    out.append(f'<text transform="translate(14 {T + ph / 2:.1f}) rotate(-90)" text-anchor="middle" font-size="12" fill="{c["muted"]}">viewers still watching r(τ)</text>')
    if area:
        out.append(f'<polygon points="{area}" fill="{c["fill"]}"/>')
        out.append(f'<polyline points="{pts}" fill="none" stroke="{c["line"]}" stroke-width="3" stroke-linejoin="round"/>')
    for n, (t, label) in enumerate(notes or []):
        # numbered marker on the curve + a legend line under the chart: never overlaps anything
        i = min(range(len(tau)), key=lambda k: abs(tau[k] - t))
        x, y = X(tau[i]), Y(ratio[i])
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="{c["note"]}"/>')
        out.append(f'<text x="{x:.1f}" y="{y + 4.5:.1f}" text-anchor="middle" font-size="13" font-weight="700" fill="#fff">{n + 1}</text>')
        out.append(f'<circle cx="{L + 10}" cy="{h + 14 + 24 * n}" r="9" fill="{c["note"]}"/>'
                   f'<text x="{L + 10}" y="{h + 18 + 24 * n}" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">{n + 1}</text>'
                   f'<text x="{L + 26}" y="{h + 19 + 24 * n}" font-size="13" fill="{c["ink"]}">{escape(label)}</text>')
    if notes:
        out[0] = out[0].replace(f'viewBox="0 0 {w} {h}"', f'viewBox="0 0 {w} {h + 10 + 24 * len(notes)}"')
    out.append("</svg>")
    return "".join(out)


def score_bars_svg(rows: list[tuple[str, float]], w: int = 520, bar_h: int = 22) -> str:
    """Horizontal bars for highlight scores (negative scores extend left of the zero line)."""
    if not rows:
        return ""
    lo, hi = min(0.0, *(v for _, v in rows)), max(0.0, *(v for _, v in rows))
    span = (hi - lo) or 1.0
    L, R = 150, 60
    pw = w - L - R
    zero = L + (0 - lo) / span * pw
    h = len(rows) * (bar_h + 8) + 8
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="Highlight scores" class="chart">']
    for i, (label, v) in enumerate(rows):
        y = 8 + i * (bar_h + 8)
        x0, x1 = sorted((zero, L + (v - lo) / span * pw))
        out.append(f'<text x="{L - 8}" y="{y + bar_h * 0.7:.1f}" text-anchor="end" font-size="12" fill="var(--ink)">{escape(label)}</text>')
        out.append(f'<rect x="{x0:.1f}" y="{y}" width="{max(1.0, x1 - x0):.1f}" height="{bar_h}" rx="4" fill="{"var(--highlight)" if v >= 0 else "var(--muted)"}"/>')
        out.append(f'<text x="{x1 + 6:.1f}" y="{y + bar_h * 0.7:.1f}" font-size="12" fill="var(--muted)">{v:+.2f}</text>')
    out.append(f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="0" y2="{h}" stroke="var(--grid)"/>')
    out.append("</svg>")
    return "".join(out)
