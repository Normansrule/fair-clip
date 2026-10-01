#!/usr/bin/env python3
"""Raster visuals from a real demo run: hero GIF, before/after, disclosures, dashboard screenshots.

Needs: `make demo` output in ./demo-data, ffmpeg, and Playwright's Chromium.
    python scripts/render_media.py
Works on a copy of demo-data, so your own demo state is left as it was.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clipwarden.config import load_settings  # noqa: E402
from clipwarden.store import Store  # noqa: E402

ASSETS = ROOT / "assets"
FONTS = ROOT / "src" / "clipwarden" / "data" / "fonts"
PAL = json.loads((ASSETS / "src" / "palette.json").read_text())
D = PAL["dark"]
L = PAL["light"]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def frame(media: Path, t: float, out: Path, width: int | None = None) -> Image.Image:
    vf = ["-vf", f"scale={width}:-2"] if width else []
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(media), "-frames:v", "1", *vf, str(out)], check=True)
    return Image.open(out).convert("RGB")


def callout(d: ImageDraw.ImageDraw, xy: tuple[int, int], text_xy: tuple[int, int], text: str, color: str, f, ink: str = "#0B1020") -> None:
    """Label box with a leader line from its right edge to the feature it explains."""
    pad = 8
    bbox = d.multiline_textbbox(text_xy, text, font=f)
    start = (bbox[2] + pad, (bbox[1] + bbox[3]) // 2)
    d.line([start, xy], fill="#0B1020", width=4)
    d.line([start, xy], fill=color, width=2)
    d.ellipse([xy[0] - 8, xy[1] - 8, xy[0] + 8, xy[1] + 8], outline="#0B1020", width=4)
    d.ellipse([xy[0] - 7, xy[1] - 7, xy[0] + 7, xy[1] + 7], outline=color, width=2)
    d.rounded_rectangle([bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad], radius=8, fill=color)
    d.multiline_text(text_xy, text, font=f, fill=ink)


class Demo:
    def __init__(self, data: Path) -> None:
        self.s = load_settings(data / "clipwarden.toml")
        self.store = Store(self.s.data_path)
        self.clips = {r["id"]: r for r in self.store.rows("clips", order="created")}
        ids = list(self.clips)
        self.a, self.b, self.c = ids[0], ids[1], ids[2]
        src = self.store.rows("sources")[0]
        self.source = self.store.find_media(src["media_sha"])
        self.src_row = src

    def media(self, cid: str, key: str = "clip_sha") -> Path:
        return self.store.find_media(self.clips[cid][key])


# ---------------------------------------------------------------------------------------- before/after
def before_after(dm: Demo, tmp: Path) -> None:
    plan = dm.clips[dm.a]["plan"]
    t_src = plan["start"] + 6.0
    raw = frame(dm.source, t_src, tmp / "raw.png", 560)
    fin = frame(dm.media(dm.a), 5.0, tmp / "fin.png").resize((540, 960))
    W, H = 1600, 1160
    img = Image.new("RGB", (W, H), L["bg"])
    d = ImageDraw.Draw(img)
    d.text((40, 30), "Before → after: the same moment, raw vs. finished clip", font=font(30, True), fill=L["ink"])
    d.text((40, 76), "Left: the file the rights holder supplied. Right: what a viewer sees.", font=font(18), fill=L["muted"])
    d.text((40, 300), "BEFORE · raw segment, 16:9, no edits", font=font(20, True), fill=L["muted"])
    img.paste(raw, (40, 340))
    d.rectangle([40, 340, 40 + raw.width, 340 + raw.height], outline=L["line"], width=2)
    d.text((40, 340 + raw.height + 16), f"source time ≈ {t_src:.0f} s", font=font(17), fill=L["muted"])
    x0, y0 = 1020, 170
    d.text((x0, 130), "AFTER · 9:16 clip, 1080 × 1920", font=font(20, True), fill=L["ink"])
    img.paste(fin, (x0, y0))
    d.rectangle([x0, y0, x0 + 540, y0 + 960], outline=L["ink"], width=3)
    f = font(17, True)
    callout(d, (x0 + 160, y0 + 230), (650, 200), "Value-add card, written by the owner\n(gate: on screen ≥ 25 % of the clip)", "#9DBBFF", f)
    callout(d, (x0 + 60, y0 + 470), (650, 470), "Whole 16:9 frame over a blurred fill,\nso nothing on the slide is cropped off", "#E3E5EA", f)
    callout(d, (x0 + 140, y0 + 770), (650, 700), "Word-by-word burned-in captions\n(spoken word highlighted)", L["highlight_fill"], f)
    callout(d, (x0 + 20, y0 + 830), (650, 830), "Lower third: channel name (first seconds)", "#C9CED8", f)
    callout(d, (x0 + 100, y0 + 945), (650, 960), "Attribution line from the rights record", "#C9CED8", f)
    img.save(ASSETS / "before-after.png", optimize=True)


# ---------------------------------------------------------------------------------------- disclosures
def disclosures(dm: Demo, tmp: Path) -> None:
    c = dm.clips[dm.c]
    af = c["plan"]["affiliate"]
    t1 = (af["start_card"][0] + af["start_card"][1]) / 2
    t2 = (af["recommendation_cards"][0][0] + af["recommendation_cards"][0][1]) / 2
    f1 = frame(dm.media(dm.c), t1, tmp / "d1.png").resize((405, 720))
    f2 = frame(dm.media(dm.c), t2, tmp / "d2.png").resize((405, 720))
    W, H = 1600, 1000
    img = Image.new("RGB", (W, H), L["bg"])
    d = ImageDraw.Draw(img)
    d.text((40, 28), "Affiliate disclosures: three places, all checked by the gate before upload", font=font(30, True), fill=L["ink"])
    d.text((40, 74), "Rendered from the demo's affiliate example (fictional kit, example.com link). FTC: put it with the claim, in audio AND video, not only behind 'more'.",
           font=font(17), fill=L["muted"])
    for k, (im, title, sub) in enumerate(((f1, "① On screen near the start", f"{af['start_card'][0]:.1f}–{af['start_card'][1]:.1f} s · gate: begins by 10 s"),
                                         (f2, "② Card at the recommendation", f"{af['recommendation_cards'][0][0]:.1f}–{af['recommendation_cards'][0][1]:.1f} s · gate: within ±3 s"))):
        x = 40 + k * 450
        d.text((x, 130), title, font=font(21, True), fill=L["ink"])
        d.text((x, 160), sub, font=font(16), fill=L["muted"])
        img.paste(im, (x, 200))
        d.rectangle([x, 200, x + 405, 920], outline=L["ink"], width=2)
        d.rounded_rectangle([x + 20, 200 + 470, x + 385, 200 + 545], radius=6, outline=L["gate"], width=4)
    x = 960
    d.text((x, 130), "③ Description: disclosure ABOVE the first link", font=font(21, True), fill=L["ink"])
    d.text((x, 160), "the frame is assembled by code, so a model can't reorder it", font=font(16), fill=L["muted"])
    import textwrap

    box = [x, 200, W - 40, 650]
    d.rounded_rectangle(box, radius=12, fill="#FFFFFF", outline=L["line"], width=2)
    desc = c["metadata"]["description"].split("\n")
    y = 218
    mono = font(15)
    for line in desc:
        if not line.strip():
            y += 10
            continue
        for chunk in textwrap.wrap(line, 64):
            col = L["ink"]
            if "commission" in chunk:
                d.rectangle([x + 14, y - 2, x + 22 + d.textlength(chunk, font=mono), y + 20], fill=PAL["light"]["disclose"])
            d.text((x + 18, y), chunk, font=mono, fill=L["flow"] if "https://" in chunk else col)
            y += 22
            if "https://" in chunk:
                d.text((x + 18, y + 4), "↑ first link: the disclosure sits above it", font=font(14, True), fill=L["gate"])
                y += 26
    d.text((x, 674), "Spoken (add it in the first 10 s of your recording):", font=font(19, True), fill=L["ink"])
    d.rounded_rectangle([x, 705, W - 40, 790], radius=12, fill=L["flow_soft"])
    d.text((x + 20, 722), "“Quick note: I earn a commission if you buy\n the kit through my link.”", font=font(20), fill=L["ink"])
    d.text((x, 815), "The demo clip has no spoken line, so its gate report shows the", font=font(16), fill=L["muted"])
    d.text((x, 840), "AFFILIATE_SINGLE_CHANNEL warning the reviewer must acknowledge.", font=font(16), fill=L["muted"])
    d.text((x, 880), "Plain words only: 'affiliate link' alone is blocked (AFFILIATE_WEAK_WORDING).", font=font(16), fill=L["muted"])
    img.save(ASSETS / "disclosures.png", optimize=True)


# ---------------------------------------------------------------------------------------- dashboard screenshots
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def screenshots(data: Path) -> dict:
    import uvicorn
    from playwright.sync_api import sync_playwright

    from clipwarden.publish.publisher import publish_approved
    from clipwarden.web import security as sec
    from clipwarden.web.app import create_app

    s = load_settings(data / "clipwarden.toml")
    s.dashboard.port = free_port()
    store = Store(s.data_path)
    sec.set_password(store, "demo-password-123")
    app = create_app(s, store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=s.dashboard.port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{s.dashboard.port}"
    clips = [r["id"] for r in store.rows("clips", order="created")]
    out = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for scheme in ("light", "dark"):
            ctx = br.new_context(viewport={"width": 1360, "height": 900}, color_scheme=scheme, device_scale_factor=1)
            pg = ctx.new_page()
            pg.goto(base + "/login")
            pg.fill("#pw", "demo-password-123")
            pg.click("button.primary")
            pg.wait_for_url(base + "/")
            if scheme == "light":
                pg.screenshot(path=str(ASSETS / "dashboard-pipeline-light.png"), full_page=True)
            pg.goto(f"{base}/clips/{clips[0]}")
            # Playwright's open-source Chromium can't decode H.264, so show each video's poster frame
            # (exactly what a reviewer sees before pressing play in a normal browser).
            pg.evaluate("""document.querySelectorAll('video').forEach(v => { const i = document.createElement('img');
                i.src = v.poster; i.style.width = '100%'; i.style.borderRadius = '8px'; i.style.display = 'block'; v.replaceWith(i); })""")
            pg.wait_for_timeout(400)
            pg.screenshot(path=str(ASSETS / f"dashboard-review-{scheme}.png"), full_page=False)
            if scheme == "light":
                # A person approves clip A, scheduled for tomorrow (the hero GIF uses this moment too)
                pg.check("input[value=scheduled_public]")
                when = (datetime.now(UTC) + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
                pg.fill("#publish_at", when.strftime("%Y-%m-%dT%H:%M"))
                pg.fill("#reason", "Clear, accurate, commentary adds context. Ship it.")
                pg.click("button[value=approve]")
                pg.wait_for_timeout(300)
                publish_approved(store, s)
                pg.goto(base + "/audit")
                pg.screenshot(path=str(ASSETS / "dashboard-audit-light.png"), full_page=False)
                pg.goto(base + "/analytics")
                pg.screenshot(path=str(ASSETS / "dashboard-analytics-light.png"), full_page=False)
                pg.goto(f"{base}/clips/{clips[1]}")
                pg.screenshot(path=str(tmp_global / "blocked.png"), full_page=False)
                out["when"] = when
            ctx.close()
        br.close()
    server.should_exit = True
    th.join(timeout=5)
    return out


# ---------------------------------------------------------------------------------------- hero GIF
def hero(dm: Demo, tmp: Path, info: dict) -> None:
    W, H = 960, 540
    scenes: list[Image.Image] = []
    ink, muted, panel, bg = D["ink"], D["muted"], D["panel"], D["bg"]
    ftitle, fbody, fsmall = font(30, True), font(19), font(15)

    def base(step: int, title: str, sub: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        im = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(im)
        labels = ["video in", "highlights", "clip", "gate", "approved", "scheduled"]
        for i, lab in enumerate(labels):
            x = 40 + i * 148
            on = i <= step
            col = D["flow"] if on and i < 3 else (D["pass"] if on else D["line"])
            if i == 3 and on:
                col = D["gate"] if step == 3 else D["pass"]
            d.rounded_rectangle([x, 22, x + 136, 52], radius=15, fill=col if i == step else panel, outline=col, width=2)
            d.text((x + 68, 37), f"{i + 1} {lab}", font=fsmall, fill=bg if i == step else (ink if on else muted), anchor="mm")
        d.text((40, 78), title, font=ftitle, fill=ink)
        d.text((40, 118), sub, font=fbody, fill=muted)
        return im, d

    # 1. long video in
    thumbs = [frame(dm.source, t, tmp / f"t{i}.png", 140) for i, t in enumerate((5, 28, 52, 66, 90, 112))]
    for k in range(10):
        im, d = base(0, "A long video, with a rights record", "2-minute CC0 sample talk · rights checked before any processing")
        for i, th in enumerate(thumbs[: min(6, k + 1)]):
            im.paste(th, (40 + i * 148, 180))
        d.rounded_rectangle([40, 300, 920, 320], radius=10, fill=panel)
        d.rounded_rectangle([40, 300, 40 + int(880 * min(1, (k + 1) / 8)), 320], radius=10, fill=D["flow"])
        d.text((40, 345), "✓ rights record demo-bridge-talk-cc0 (CC0-1.0) · file hash matches", font=fbody, fill=D["pass"])
        d.text((40, 380), "✓ magic bytes + ffprobe inside the network-less sandbox", font=fbody, fill=D["pass"])
        scenes.append(im)
    # 2. ranked highlights
    cands = sorted([r["data"] for r in dm.store.rows("candidates", order="rank")], key=lambda c: c["rank"])
    dur = 125.8
    for k in range(14):
        im, d = base(1, "Ranked highlight candidates", "S = w·z(energy) + w·relevance + w·z(speech rate) + w·scenes − w·silence")
        d.rounded_rectangle([40, 190, 920, 214], radius=6, fill=panel)
        for c in cands:
            x0, x1 = 40 + 880 * c["start"] / dur, 40 + 880 * c["end"] / dur
            col = D["highlight_fill"] if c["rank"] == 1 else D["line"]
            d.rounded_rectangle([x0, 190, x1, 214], radius=6, fill=col)
        for i, c in enumerate(cands[: min(5, k // 2 + 1)]):
            y = 250 + i * 50
            wbar = int(max(0.0, c["score"] + 0.8) / 2.0 * 520)
            d.text((40, y + 6), f"#{c['rank']}  {c['start']:.0f}–{c['end']:.0f} s", font=fbody, fill=ink)
            d.rounded_rectangle([240, y, 240 + max(8, wbar), y + 34], radius=6, fill=D["highlight_fill"] if i == 0 else D["flow"])
            d.text((250 + max(8, wbar), y + 6), f"S = {c['score']:+.2f}", font=fbody, fill=ink)
        scenes.append(im)
    # 3. captioned clip (real frames)
    clip = dm.media(dm.a)
    vf = [frame(clip, t, tmp / f"c{t}.png").resize((216, 384)) for t in (0.6, 1.4, 3.0, 4.5, 6.0, 8.0, 10.0, 12.5, 15.0, 17.5, 20.0, 22.0)]
    for k, v in enumerate(vf * 2):
        im, d = base(2, "Edited clip with original value-add", "9:16 reframe · cut pauses · hook · word captions · owner's context card")
        im.paste(v, (700, 140))
        d.rectangle([700, 140, 916, 524], outline=ink, width=2)
        for i, line in enumerate(["• hook = verbatim quote (never invented)", "• 1.2 s of dead air removed", "• burned-in, word-by-word captions",
                                  "• value-add on screen 71 % of the clip", "• synthetic voice → disclosure flag set", "• attribution from the rights record"]):
            if i <= k // 3:
                d.text((40, 190 + i * 44), line, font=fbody, fill=ink)
        scenes.append(im)
    # 4. compliance report
    gate = dm.clips[dm.a]["gate"]["results"]
    for k in range(len(gate) + 6):
        im, d = base(3, "Compliance gate: automated and blocking", "every check runs; one ✕ and the clip can never reach a human")
        for i, r in enumerate(gate[: k + 1]):
            y = 170 + i * 36
            d.text((40, y), "✓", font=font(22, True), fill=D["pass"])
            d.text((72, y + 2), f"{r['check']:<16}", font=font(18, True), fill=ink)
            msg = r["message"] if len(r["message"]) <= 58 else r["message"][:57].rsplit(" ", 1)[0] + "…"
            d.text((260, y + 2), msg, font=fsmall, fill=muted)
        if k >= len(gate):
            d.rounded_rectangle([700, 440, 920, 500], radius=12, fill=D["pass"])
            d.text((810, 470), "PASSED", font=font(26, True), fill=bg, anchor="mm")
        scenes.append(im)
    # 5. approved (real screenshot)
    shot = Image.open(ASSETS / "dashboard-review-light.png").convert("RGB")
    shot = shot.crop((0, 0, 1360, 820)).resize((880, 530))
    for k in range(14):
        im, d = base(4, "A person approves (or rejects, with a reason)", "the decision binds the exact file and metadata hashes")
        im.paste(shot.crop((0, 0, 880, 380)), (40, 150))
        if k > 4:
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            od.rounded_rectangle([560, 300, 900, 400], radius=14, outline=D["pass"], width=7, fill=(11, 16, 32, 225))
            od.text((730, 350), "✓ APPROVED", font=font(36, True), fill=D["pass"], anchor="mm")
            im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
        scenes.append(im)
    # 6. scheduled
    when = info.get("when")
    for _k in range(18):
        im, d = base(5, "Scheduled: uploads private, YouTube flips it public", "dry-run by default · one OAuth scope · kill switch · audit log")
        d.rounded_rectangle([40, 170, 920, 470], radius=16, fill=panel, outline=D["line"], width=2)
        rows = [("videos.insert", "part=snippet,status  (scope: youtube.upload only)"),
                ("privacyStatus", "private"),
                ("publishAt", when.strftime("%Y-%m-%dT%H:%M:%S.000Z") if when else "tomorrow 16:00 UTC"),
                ("containsSyntheticMedia", "true   (synthetic narrator → disclosed)"),
                ("audit log", "ingest → plan → gate → approve → publish  ✓ intact")]
        for i, (k_, v) in enumerate(rows):
            d.text((70, 200 + i * 52), k_, font=font(19, True), fill=D["flow"])
            d.text((340, 200 + i * 52), v, font=fbody, fill=ink)
        scenes.append(im)
    # hold frames longer on scene ends: duplicate last frame of each scene
    frames: list[Image.Image] = []
    durations: list[int] = []
    bounds = [10, 24, 48, 48 + len(gate) + 6, 48 + len(gate) + 20, len(scenes)]
    for i, im in enumerate(scenes):
        frames.append(im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
        in_clip_scene = 24 <= i < 48
        durations.append(900 if (i + 1) in bounds else (170 if in_clip_scene else 110))
    frames[0].save(ASSETS / "hero.gif", save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True, disposal=1)


tmp_global = Path(tempfile.mkdtemp(prefix="cw-media-"))


def main() -> int:
    src = ROOT / "demo-data"
    if not (src / "clipwarden.sqlite3").exists():
        print("run `make demo` first", file=sys.stderr)
        return 1
    work = tmp_global / "demo-data"
    shutil.copytree(src, work)
    cfg = (work / "clipwarden.toml").read_text()
    (work / "clipwarden.toml").write_text(cfg)
    dm = Demo(work)
    before_after(dm, tmp_global)
    disclosures(dm, tmp_global)
    info = screenshots(work)
    dm = Demo(work)
    hero(dm, tmp_global, info)
    for n in ("hero.gif", "before-after.png", "disclosures.png"):
        print(f"{n}: {(ASSETS / n).stat().st_size / 1e6:.2f} MB")
    shutil.rmtree(tmp_global, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
