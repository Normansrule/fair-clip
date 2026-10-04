"""fair-clip command line. Run `fair-clip --help`."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import redact
from .config import load_settings
from .pipeline import Pipeline, PipelineError
from .rights import RightsError

APP_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fair-clip"


def config_path(arg: str | None) -> Path | None:
    """One rule for every command, so `fair-clip kill on` stops the same app you launched:
    --config  >  $FAIRCLIP_CONFIG  >  ./fairclip.toml  >  the app's ~/.local/share/fair-clip/fairclip.toml."""
    if arg:
        return Path(arg)
    if os.environ.get("FAIRCLIP_CONFIG"):
        return Path(os.environ["FAIRCLIP_CONFIG"])
    if Path("fairclip.toml").exists():
        return Path("fairclip.toml")
    app = APP_HOME / "fairclip.toml"
    return app if app.exists() else None


def _pipeline(args: argparse.Namespace) -> Pipeline:
    return Pipeline(load_settings(config_path(args.config)))


def cmd_ingest(a: argparse.Namespace) -> int:
    p = _pipeline(a)
    sid = p.ingest(Path(a.video), Path(a.rights), Path(a.transcript) if a.transcript else None)
    print(f"source {sid}: ranked candidates")
    for r in p.store.rows("candidates", "source_id=?", (sid,), order="rank"):
        c = r["data"]
        print(f"  #{c['rank']:<2} {c['candidate_id']}  {c['start']:6.1f}-{c['end']:6.1f}s  S={c['score']:+.3f}  {c['text'][:70]}…")
    return 0


def cmd_plan(a: argparse.Namespace) -> int:
    p = _pipeline(a)
    overrides = json.loads(Path(a.plan_file).read_text()) if a.plan_file else {}
    overrides.pop("source_id", None), overrides.pop("start", None), overrides.pop("end", None)
    plan = p.plan_from_candidate(a.candidate, **overrides)
    print(f"planned {plan.clip_id} ({plan.end - plan.start:.1f}s, {len(plan.value_add)} value-add element(s))")
    return 0


def cmd_render(a: argparse.Namespace) -> int:
    p = _pipeline(a)
    rep = p.render_and_gate(a.clip)
    print(f"{a.clip}: gate {'PASSED' if rep.passed else 'BLOCKED'}")
    for r in rep.results:
        mark = {"pass": "  ok ", "warn": " WARN", "block": "BLOCK"}[r.severity.value]
        print(f"  [{mark}] {r.check:<16} {r.code:<34} {r.message[:110]}")
    return 0 if rep.passed else 2


def cmd_publish(a: argparse.Namespace) -> int:
    from .publish.publisher import publish_approved

    p = _pipeline(a)
    if a.action == "enable-live":
        if p.s.publish.mode != "live":
            print("set publish.mode = \"live\" in fairclip.toml first", file=sys.stderr)
            return 1
        if input("Type ENABLE LIVE to allow real uploads: ").strip() != "ENABLE LIVE":
            return 1
        p.store.set("live_enabled", True)
        p.store.audit.append(getpass.getuser(), "enable_live", "global", {})
        print("live publishing enabled")
        return 0
    uploader = None
    if p.s.publish.mode == "live" and p.store.get("live_enabled"):
        from googleapiclient.discovery import build

        from .publish.youtube import credentials, upload
        from .vault import open_vault

        creds = credentials(Path(p.s.publish.oauth_client_file), open_vault(p.store.root))
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        uploader = lambda body, path: upload(yt, body, path)  # noqa: E731
    for r in publish_approved(p.store, p.s, uploader=uploader, actor=getpass.getuser()):
        print(r)
    return 0


def cmd_auth(a: argparse.Namespace) -> int:
    from .publish.youtube import authorize, revoke
    from .vault import open_vault

    p = _pipeline(a)
    vault = open_vault(p.store.root)
    if a.action == "login":
        info = authorize(Path(p.s.publish.oauth_client_file), p.s.publish.scopes, vault)
        p.store.audit.append(getpass.getuser(), "oauth_login", "youtube", info)
        print(f"authorized with scopes {info['scopes']}")
    elif a.action == "revoke":
        ok = revoke(vault)
        p.store.audit.append(getpass.getuser(), "oauth_revoke", "youtube", {"google_ok": ok})
        print("token revoked" if ok else "local token deleted (Google did not confirm revocation)")
    return 0


def cmd_kill(a: argparse.Namespace) -> int:
    p = _pipeline(a)
    p.store.set_kill_switch(a.state == "on", getpass.getuser())
    print(f"kill switch {a.state}")
    return 0


def cmd_audit(a: argparse.Namespace) -> int:
    p = _pipeline(a)
    if a.action == "verify":
        ok, bad = p.store.audit.verify()
        print("audit chain OK" if ok else f"audit chain BROKEN at entry {bad}")
        return 0 if ok else 3
    if a.action == "head":
        print(p.store.audit.head())
        return 0
    for e in reversed(p.store.audit.entries(limit=a.limit)):
        print(f"{e.seq:>5} {e.ts} {e.actor:<18} {e.action:<16} {e.subject:<24} {json.dumps(e.details)[:90]}")
    return 0


def _import_analytics(p: Pipeline, file: str) -> int:
    """Import retention/metrics JSON: {clip_id: {"tau": [...], "ratio": [...], "stats": {...}}}."""
    raw = json.loads(Path(file).read_text())
    if not isinstance(raw, dict) or len(raw) > 1000:
        raise ValueError("analytics file must be an object of at most 1000 clips")
    clean: dict[str, Any] = {}
    for cid, v in raw.items():
        tau, ratio = [float(x) for x in v["tau"]], [float(x) for x in v["ratio"]]
        if len(tau) != len(ratio) or not 2 <= len(tau) <= 1000 or any(not 0 <= t <= 1 for t in tau):
            raise ValueError(f"{cid}: tau/ratio malformed")
        clean[str(cid)[:64]] = {"tau": tau, "ratio": ratio, "stats": {str(k)[:40]: v2 for k, v2 in (v.get("stats") or {}).items()},
                                "notes": [[float(n[0]), str(n[1])[:60]] for n in v.get("notes", [])][:5],
                                "illustrative": bool(v.get("illustrative", False))}
    p.store.update("analytics", lambda cur: dict(cur or {}) | clean)  # atomic, even against a running dashboard
    p.store.audit.append(getpass.getuser(), "analytics_import", "analytics", {"clips": len(raw)})
    print(f"imported analytics for {len(raw)} clip(s)")
    return 0


def cmd_analytics(a: argparse.Namespace) -> int:
    """import FILE | connect | pull | disconnect. connect/pull use the read-only analytics scope."""
    from .analytics import ANALYTICS_SCOPE
    from .analytics_api import ANALYTICS_TOKEN

    p = _pipeline(a)
    if a.action == "import":
        if not a.file:
            raise ValueError("usage: fair-clip analytics import FILE.json")
        return _import_analytics(p, a.file)
    from .publish.youtube import authorize, credentials, revoke
    from .vault import open_vault

    vault = open_vault(p.store.root)
    client = Path(p.s.publish.oauth_client_file)
    if a.action == "connect":
        info = authorize(client, [ANALYTICS_SCOPE], vault, token_name=ANALYTICS_TOKEN)
        p.store.audit.append(getpass.getuser(), "oauth_login", "youtube-analytics", info)
        print(f"analytics connected (read-only) with scopes {info['scopes']}")
    elif a.action == "disconnect":
        ok = revoke(vault, token_name=ANALYTICS_TOKEN)
        p.store.audit.append(getpass.getuser(), "oauth_revoke", "youtube-analytics", {"google_ok": ok})
        print("analytics token revoked" if ok else "local analytics token deleted (Google did not confirm revocation)")
    else:  # pull
        import httpx
        from google.auth.transport.requests import Request

        from .analytics_api import pull

        creds = credentials(client, vault, token_name=ANALYTICS_TOKEN)
        creds.refresh(Request())
        with httpx.Client(timeout=30) as http:
            res = pull(p.store, http, creds.token, actor=getpass.getuser())
        print(f"pulled {res['clips']} clip(s); {res['no_retention_yet']} without a retention curve yet")
        for e in res["errors"]:
            print("  " + e, file=sys.stderr)
    return 0


def cmd_affiliate(a: argparse.Namespace) -> int:
    """show | import FILE | review-terms PROGRAM_ID | heard CLIP_ID (milestone 3, docs/AFFILIATE.md)."""
    from datetime import UTC, datetime

    from .affiliate import TERMS_STALE_DAYS, import_registry, load_registry, update_registry

    p = _pipeline(a)
    who = getpass.getuser()
    if a.action == "import":
        if not a.arg:
            raise ValueError("usage: fair-clip affiliate import registry.json")
        r = import_registry(p.store, json.loads(Path(a.arg).read_text()), who)
        print(f"registry saved: {len(r.programs)} program(s), {len(r.products)} product(s), {len(r.links)} link(s)")
    elif a.action == "review-terms":
        today = datetime.now(UTC).date()

        def mark(reg):  # type: ignore[no-untyped-def]
            prog = reg.program(a.arg or "")
            if prog is None:
                raise ValueError(f"no program '{a.arg}'")
            prog.terms_reviewed_on = today
            return reg
        update_registry(p.store, mark, who, "terms_reviewed", {"program": a.arg, "on": today.isoformat()})
        print(f"{a.arg}: terms marked as re-read on {today}")
    elif a.action == "heard":
        if not a.arg:
            raise ValueError("usage: fair-clip affiliate heard CLIP_ID")
        heard = p.heard_disclosures(a.arg)
        for t, phrase in heard:
            print(f"  {t:6.1f} s  '{phrase}'")
        print(f"{len(heard)} spoken disclosure(s) in {a.arg}" if heard else f"no spoken disclosure in {a.arg}")
    else:  # show
        r = load_registry(p.store)
        today = datetime.now(UTC).date()
        for prog in r.programs:
            age = prog.terms_age_days(today)
            flag = "  ⚠ re-read terms" if age > TERMS_STALE_DAYS else ""
            print(f"program {prog.id:<16} {'active' if prog.active else 'PAUSED':<7} terms read {age:>3} d ago{flag}  {prog.terms_url}")
        for prod in r.products:
            print(f"product {prod.name:<30} evaluated {prod.evaluated_on}{'  (retired)' if prod.retired else ''}")
        for lk in r.links:
            print(f"link    {lk.url}  [{lk.program_id} / {lk.product}]")
        if not (r.programs or r.products or r.links):
            print("empty registry: add programs on the dashboard's Affiliate page, or `fair-clip affiliate import FILE`")
    return 0


def cmd_weights(a: argparse.Namespace) -> int:
    """Propose bounded weights from live analytics (--from-analytics) or a rows file:
    {"rows": [{"z": {...5 features...}, "outcome": APV}, ...]}. A person applies it in Settings."""
    from .analytics_api import propose_from_analytics, save_proposal
    from .scoring import propose_weights

    p = _pipeline(a)
    if a.from_analytics:
        pid, proposed = propose_from_analytics(p.store, p.weights(), getpass.getuser())
    else:
        if not a.file:
            raise ValueError("give a rows FILE or --from-analytics")
        data = json.loads(Path(a.file).read_text())
        X, y = [r["z"] for r in data["rows"]], [float(r["outcome"]) for r in data["rows"]]
        proposed, raw = propose_weights(p.weights(), X, y)
        pid = save_proposal(p.store, p.weights(), proposed, {"n": len(y), "raw_fit": raw, "from": "file"}, getpass.getuser())
    print(f"proposal #{pid} stored (pending human approval on the Settings page): {proposed}")
    return 0


def cmd_studio(a: argparse.Namespace) -> int:
    """Studio from the command line, in two runs:
    1. without takes: finds the best moments and writes them into the project file with a
       suggestion for each, for YOU to fill in (the take is the value-add; fair-clip never writes it);
    2. with takes: renders every clip, gates it, and writes the videos + copy-paste texts + a zip."""
    import tempfile

    from . import studio
    from .media.sandbox import MediaToolError, SandboxUnavailable
    from .studio_run import (
        StudioError,
        analyze_source,
        gate_inputs,
        make_clip,
        owner_settings,
        write_package,
    )

    proj_path = Path(a.project)
    try:
        proj = json.loads(proj_path.read_text()) if proj_path.exists() else {}
        brand = studio.Brand.model_validate(proj.get("brand") or {"channel": "My Channel"})
        choice = studio.RightsChoice.model_validate(proj.get("rights") or {})
    except ValueError as exc:
        print(f"refused: {proj_path}: {redact.redact(str(exc))[:600]}", file=sys.stderr)
        return 1
    p = _pipeline(a)
    settings = owner_settings(p.s, brand)
    captions = Path(a.captions).read_text("utf-8", "replace") if a.captions else None
    work = Path(tempfile.mkdtemp(prefix="studio-", dir=p.work))
    try:
        sess = analyze_source(Path(a.video), captions, brand, choice, runner=p.runner, work=work, settings=settings,
                              progress=lambda m: print(f"  … {m}", file=sys.stderr))
    except (StudioError, ValueError, SandboxUnavailable, MediaToolError) as exc:
        print(f"refused: {redact.redact(str(exc))[:800]}", file=sys.stderr)
        return 1
    clips = proj.get("clips") or []
    moments = [{"start": round(c.start, 2), "end": round(c.end, 2)} for c in sess.candidates]
    same = len(clips) == len(moments) and all(c.get("moment") == m for c, m in zip(clips, moments, strict=True))
    if not same or any(c.get("include", True) and not c.get("take", "").strip() for c in clips):
        old = {json.dumps(c.get("moment")): c for c in clips}
        proj["clips"] = []
        for c, m, sug in zip(sess.candidates, moments, sess.suggestions, strict=True):
            prev = old.get(json.dumps(m), {})
            proj["clips"].append({
                "moment": m, "says": c.text[:400], "question": sug["prompt"],
                "template": prev.get("template", sug["template"]), "data": prev.get("data", sug["data"]),
                "take": prev.get("take", ""), "title": prev.get("title", ""), "include": prev.get("include", True)})
        proj.setdefault("brand", brand.model_dump()), proj.setdefault("rights", choice.model_dump(mode="json"))
        proj_path.write_text(json.dumps(proj, indent=2, ensure_ascii=False) + "\n")
        print(f"found {len(moments)} moments. Write your take for each in {proj_path} ('take', and optionally 'title'), "
              "or set \"include\": false to skip one. Then run the same command again.")
        return 3
    made = []
    for n, (c, cand) in enumerate(zip(clips, sess.candidates, strict=True), 1):
        if not c.get("include", True):
            continue
        take = studio.Take(text=c["take"], template=c.get("template") or "card", data=c.get("data"))
        print(f"  … rendering clip {n} of {len(clips)}", file=sys.stderr)
        try:
            made.append(make_clip(sess, cand, take, c.get("title", ""), n, runner=p.runner, work=work, quality=a.quality,
                                  gate=gate_inputs(p)))
        except (ValueError, SandboxUnavailable, MediaToolError) as exc:
            print(f"refused: clip {n}: {redact.redact(str(exc))[:800]}", file=sys.stderr)
            return 1
    out = Path(a.out)
    zpath = write_package(made, brand, out)
    for m in made:
        f = m.finished
        state = "ready  " if f.passed else "BLOCKED"
        print(f"  [{state}] {f.filename}  {f.duration:5.1f}s  {f.metadata.title}")
        for r in f.report.blocks:
            print(f"            {r.code}: {r.message}")
    print(f"wrote {out}/ (videos, ALL-TITLES-AND-DESCRIPTIONS.txt, upload-sheet.csv) and {zpath.name}")
    return 0 if all(m.finished.passed for m in made) else 2


def cmd_dashboard(a: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    s = load_settings(config_path(a.config))
    app = create_app(s)
    print(f"dashboard on http://{s.dashboard.host}:{s.dashboard.port} (localhost only)")
    uvicorn.run(app, host=s.dashboard.host, port=s.dashboard.port, log_level="warning", server_header=False)
    return 0


APP_CONFIG = """# fair-clip app config (created on first run). Edit, then restart `fair-clip app`.
data_dir = "."
link_allowlist = []

[channel]
name = "My Channel"
topics = []
daily_cap = 3

[publish]
mode = "dry-run"          # nothing leaves your machine until you run `fair-clip publish enable-live`

[dashboard]
host = "127.0.0.1"
port = 8765
"""


def _open_browser(url: str) -> None:
    """Open the dashboard. Under WSL, open it in the Windows browser."""
    import shutil
    import subprocess
    import webbrowser

    if "microsoft" in os.uname().release.lower():
        for cmd in (["wslview", url], ["explorer.exe", url]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fixed argv, no shell
                return
    webbrowser.open(url)


def cmd_app(a: argparse.Namespace) -> int:
    """The desktop way in: create a config on first run, start the dashboard, open it in a browser."""
    import secrets
    import threading
    import time

    import uvicorn

    from .store import Store
    from .web.app import create_app

    cfg = config_path(a.config) or APP_HOME / "fairclip.toml"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.write_text(APP_CONFIG)
        print(f"first run: wrote {cfg} (dry-run mode)")
    s = load_settings(cfg)
    first = not Store(s.data_path).get("dashboard_pw")
    token = secrets.token_urlsafe(24) if first else None
    app = create_app(s, setup_token=token)
    url = f"http://{s.dashboard.host}:{s.dashboard.port}/"
    server = uvicorn.Server(uvicorn.Config(app, host=s.dashboard.host, port=s.dashboard.port, log_level="warning", server_header=False))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Trust only OUR server: wait until it has bound the port, and give up if its thread dies (port
    # taken). A health probe alone could be answered by whatever else owns the port.
    deadline = time.monotonic() + 15
    while not server.started and t.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError(f"could not start on {url}: is fair-clip (or something else) already running there? "
                           "Close it, or set [dashboard] port in " + str(cfg))
    print(f"fair-clip is running at {url}  (localhost only; press Ctrl+C to stop)")
    print("tip: in Chrome or Edge choose 'Install fair-clip' (or 'Install page as app') to give it its own window")
    target = url + f"setup?t={token}" if token else url
    if token:
        print(f"first run: choose your password at {target}")
        print("  (closed that tab? stop fair-clip and start it again for a fresh link)")
    if not a.no_browser:
        _open_browser(target)
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        t.join(5)
    return 0


def cmd_set_password(a: argparse.Namespace) -> int:
    from .web.security import set_password

    p = _pipeline(a)
    pw = getpass.getpass("New dashboard password (12+ chars): ")
    if pw != getpass.getpass("Repeat: "):
        print("passwords differ", file=sys.stderr)
        return 1
    set_password(p.store, pw)
    p.store.audit.append(getpass.getuser(), "set_password", "dashboard", {})
    print("password set")
    return 0


def main(argv: list[str] | None = None) -> int:
    redact.install()
    ap = argparse.ArgumentParser(prog="fair-clip", description="Security-first clipping pipeline. Automate the labor, never the judgment.")
    ap.add_argument("--config", default=None, help="path to fairclip.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="stages 1-3: rights check, transcribe, rank highlights")
    s.add_argument("video"), s.add_argument("--rights", required=True), s.add_argument("--transcript")
    s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("plan", help="create a clip plan from a candidate")
    s.add_argument("candidate"), s.add_argument("--plan-file", help="JSON with value_add, tags, template, ...")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("render", help="stages 4-7: edit, value-add, metadata, compliance gate")
    s.add_argument("clip")
    s.set_defaults(fn=cmd_render)
    s = sub.add_parser("publish", help="stage 9: upload approved clips (dry-run unless live is enabled)")
    s.add_argument("action", nargs="?", default="run", choices=["run", "enable-live"])
    s.set_defaults(fn=cmd_publish)
    s = sub.add_parser("auth", help="YouTube OAuth: login or revoke")
    s.add_argument("action", choices=["login", "revoke"])
    s.set_defaults(fn=cmd_auth)
    s = sub.add_parser("kill", help="global kill switch")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_kill)
    s = sub.add_parser("audit", help="show or verify the audit log")
    s.add_argument("action", nargs="?", default="show", choices=["show", "verify", "head"])
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("analytics", help="stage 10: connect (read-only), pull, import FILE, disconnect")
    s.add_argument("action", choices=["import", "connect", "pull", "disconnect"]), s.add_argument("file", nargs="?")
    s.set_defaults(fn=cmd_analytics)
    s = sub.add_parser("affiliate", help="affiliate registry: show, import FILE, review-terms ID, heard CLIP_ID")
    s.add_argument("action", choices=["show", "import", "review-terms", "heard"]), s.add_argument("arg", nargs="?")
    s.set_defaults(fn=cmd_affiliate)
    s = sub.add_parser("weights", help="propose bounded highlight-weight update (needs approval)")
    s.add_argument("action", choices=["propose"]), s.add_argument("file", nargs="?")
    s.add_argument("--from-analytics", action="store_true", help="use pulled YouTube Analytics (>= 10 clips, >= 50 views each)")
    s.set_defaults(fn=cmd_weights)
    s = sub.add_parser("studio", help="video + captions + your takes -> finished clips and copy-paste texts")
    s.add_argument("video"), s.add_argument("--captions", help=".srt/.vtt (else speech-to-text, if installed)")
    s.add_argument("--project", required=True, help="studio project JSON (brand, rights, your takes); created if missing")
    s.add_argument("--out", default="fair-clip-out"), s.add_argument("--quality", choices=["high", "fast"], default="high")
    s.set_defaults(fn=cmd_studio)
    s = sub.add_parser("app", help="start the dashboard and open it in your browser (first run creates a config)")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_app)
    s = sub.add_parser("dashboard", help="stage 8 UI on localhost")
    s.set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("set-password", help="set the dashboard password")
    s.set_defaults(fn=cmd_set_password)
    args = ap.parse_args(argv)
    try:
        return int(args.fn(args))
    except (RightsError, PipelineError, ValueError, RuntimeError) as exc:
        print(f"refused: {redact.redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
