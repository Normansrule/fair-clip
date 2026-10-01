"""clipwarden command line. Run `clipwarden --help`."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import redact
from .config import load_settings
from .pipeline import Pipeline, PipelineError
from .rights import RightsError


def _pipeline(args: argparse.Namespace) -> Pipeline:
    return Pipeline(load_settings(args.config))


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
            print("set publish.mode = \"live\" in clipwarden.toml first", file=sys.stderr)
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


def cmd_analytics(a: argparse.Namespace) -> int:
    """Import retention/metrics JSON: {clip_id: {"tau": [...], "ratio": [...], "stats": {...}}}."""
    p = _pipeline(a)
    raw = json.loads(Path(a.file).read_text())
    if not isinstance(raw, dict) or len(raw) > 1000:
        raise ValueError("analytics file must be an object of at most 1000 clips")
    clean = {}
    for cid, v in raw.items():
        tau, ratio = [float(x) for x in v["tau"]], [float(x) for x in v["ratio"]]
        if len(tau) != len(ratio) or not 2 <= len(tau) <= 1000 or any(not 0 <= t <= 1 for t in tau):
            raise ValueError(f"{cid}: tau/ratio malformed")
        clean[str(cid)[:64]] = {"tau": tau, "ratio": ratio, "stats": {str(k)[:40]: v2 for k, v2 in (v.get("stats") or {}).items()},
                                "notes": [[float(n[0]), str(n[1])[:60]] for n in v.get("notes", [])][:5],
                                "illustrative": bool(v.get("illustrative", False))}
    p.store.set("analytics", clean)
    p.store.audit.append(getpass.getuser(), "analytics_import", "analytics", {"clips": len(clean)})
    print(f"imported analytics for {len(clean)} clip(s)")
    return 0


def cmd_weights(a: argparse.Namespace) -> int:
    """Propose new highlight weights from {"rows": [{"z": {...5 features...}, "outcome": APV}, ...]}."""
    from .scoring import propose_weights

    p = _pipeline(a)
    data = json.loads(Path(a.file).read_text())
    X, y = [r["z"] for r in data["rows"]], [float(r["outcome"]) for r in data["rows"]]
    proposed, raw = propose_weights(p.weights(), X, y)
    with p.store.conn:
        p.store.conn.execute(
            "INSERT INTO weight_proposals (created, current, proposed, evidence, status) VALUES (datetime('now'), ?, ?, ?, 'pending')",
            (json.dumps(p.weights()), json.dumps(proposed), json.dumps({"n": len(y), "raw_fit": raw})))
    p.store.audit.append(getpass.getuser(), "weights_proposed", "scoring", {"proposed": proposed, "n": len(y)})
    print(f"proposal stored (pending human approval on the Settings page): {proposed}")
    return 0


def cmd_dashboard(a: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    s = load_settings(a.config)
    app = create_app(s)
    print(f"dashboard on http://{s.dashboard.host}:{s.dashboard.port} (localhost only)")
    uvicorn.run(app, host=s.dashboard.host, port=s.dashboard.port, log_level="warning", server_header=False)
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
    ap = argparse.ArgumentParser(prog="clipwarden", description="Security-first clipping pipeline. Automate the labor, never the judgment.")
    ap.add_argument("--config", default=None, help="path to clipwarden.toml")
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
    s = sub.add_parser("analytics", help="stage 10: import retention data")
    s.add_argument("action", choices=["import"]), s.add_argument("file")
    s.set_defaults(fn=cmd_analytics)
    s = sub.add_parser("weights", help="propose bounded highlight-weight update (needs approval)")
    s.add_argument("action", choices=["propose"]), s.add_argument("file")
    s.set_defaults(fn=cmd_weights)
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
