"""Stage 8 dashboard: pipeline status, side-by-side review, compliance report, approve/reject.

Runs on 127.0.0.1 only. Server-rendered HTML (Jinja2 autoescaping on) with no inline script.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..analytics import area_under_retention, biggest_drop
from ..approval import ApprovalError, decide, human_required
from ..config import Settings
from ..gate.reasons import GROUP_ORDER, REASONS
from ..models import ClipPlan, Decision, GateReport, Visibility
from ..pipeline import STAGES, Pipeline, PipelineError
from ..publish.body import paid_promotion_required
from ..publish.publisher import live_ready
from ..publish.youtube import TOKEN_NAME
from ..scoring import normalise
from ..store import Store
from ..vault import VaultError, open_vault
from . import security as sec
from .charts import retention_svg, score_bars_svg

HERE = Path(__file__).resolve().parent


def create_app(settings: Settings, store: Store | None = None, extra_hosts: tuple[str, ...] = ()) -> FastAPI:
    store = store or Store(settings.data_path)
    sessions = sec.Sessions(sec.session_key(store.root), store)
    pipeline = Pipeline(settings, store)
    throttle = sec.Throttle()
    hosts = sec.allowed_hosts(settings.dashboard.port) - {"testserver"} | set(extra_hosts)
    first_pw = sec.ensure_password(store)
    if first_pw:
        print("\n  First run: dashboard password (shown once, change it with `clipwarden set-password`):\n"
              f"    {first_pw}\n")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(STAGES=STAGES, REASONS=REASONS)
    tpl.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "")
        if host not in hosts:
            return Response("bad host", status_code=400)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in hosts:
                return Response("cross-origin POST refused", status_code=403)
        resp = await call_next(request)
        for k, v in sec.SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    def session(request: Request) -> dict | None:
        return sessions.read(request.cookies.get(sec.SESSION_COOKIE))

    def need(request: Request) -> dict:
        s = session(request)
        if not s:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return s

    async def need_csrf(request: Request, s: dict) -> dict[str, Any]:
        form = dict(await request.form())
        if not sec.csrf_ok(s, str(form.get("csrf", ""))):
            raise HTTPException(status_code=403, detail="CSRF token missing or wrong")
        return form

    def page(request: Request, name: str, s: dict | None, **ctx: Any) -> HTMLResponse:
        base = {"user": s.get("u") if s else None, "csrf": s.get("csrf") if s else "", "kill": store.kill_switch,
                "mode": settings.publish.mode, "live": live_ready(store, settings)[0], "channel": settings.channel.name}
        return tpl.TemplateResponse(request, name, base | ctx)

    # ------------------------------------------------------------------ auth
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return page(request, "login.html", None, error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form(...)) -> Response:
        who = request.client.host if request.client else "?"
        if throttle.blocked(who):
            return page(request, "login.html", None, error="Too many attempts. Wait 15 minutes.")
        if not sec.check_password(store, password):
            throttle.fail(who)
            store.audit.append(who, "login_failed", "dashboard", {})
            return page(request, "login.html", None, error="Wrong password.")
        throttle.reset(who)
        store.audit.append("owner", "login", "dashboard", {"client": who})
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(sec.SESSION_COOKIE, sessions.issue("owner"), httponly=True, samesite="strict",
                     max_age=sec.SESSION_MAX_AGE, secure=False, path="/")
        return r

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        s = need(request)
        await need_csrf(request, s)
        sec.bump_epoch(store)  # server-side logout: the old cookie stops working everywhere
        store.audit.append(s["u"], "logout", "dashboard", {})
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(sec.SESSION_COOKIE, path="/")
        return r

    # ------------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        s = need(request)
        sources = store.rows("sources", order="created DESC")
        clips = store.rows("clips", order="created DESC")
        ok, bad = store.audit.verify()
        return page(request, "overview.html", s, sources=sources, clips=clips, audit_ok=ok, audit_bad=bad)

    @app.get("/sources/{sid}", response_class=HTMLResponse)
    def source(request: Request, sid: str) -> HTMLResponse:
        s = need(request)
        src = store.row("sources", sid)
        if not src:
            raise HTTPException(404)
        cands = [r["data"] for r in store.rows("candidates", "source_id=?", (sid,), order="rank")]
        bars = score_bars_svg([(f"#{c['rank']} {c['start']:.0f}-{c['end']:.0f}s", c["score"]) for c in cands])
        return page(request, "source.html", s, src=src, cands=cands, bars=bars, weights=normalise(store.get("weights") or settings.scoring.weights))

    # ------------------------------------------------------------------ review + decision
    @app.get("/clips/{cid}", response_class=HTMLResponse)
    def clip(request: Request, cid: str) -> HTMLResponse:
        s = need(request)
        row = store.row("clips", cid)
        if not row:
            raise HTTPException(404)
        gate = GateReport.model_validate(row["gate"]) if row.get("gate") else None
        plan = ClipPlan.model_validate(row["plan"])
        grouped: dict[str, list] = {g: [] for g in GROUP_ORDER}
        for r in gate.results if gate else []:
            grouped.setdefault(r.check, []).append(r)
        return page(request, "clip.html", s, row=row, plan=plan, gate=gate, grouped=grouped,
                    paid=paid_promotion_required(plan), human_only=human_required(plan, settings),
                    visibilities=[v.value for v in Visibility], error=request.query_params.get("error"))

    @app.post("/clips/{cid}/decide")
    async def clip_decide(request: Request, cid: str) -> Response:
        s = need(request)
        form = await need_csrf(request, s)
        try:
            when = form.get("publish_at") or None
            d = Decision(clip_id=cid, approved=form.get("decision") == "approve", reviewer=s["u"],
                         reason=str(form.get("reason", ""))[:1000], visibility=Visibility(str(form.get("visibility", "private"))),
                         publish_at=datetime.fromisoformat(str(when)).replace(tzinfo=UTC) if when else None,
                         warnings_acknowledged=form.get("ack") == "on")
            decide(store, settings, d)
        except (ApprovalError, ValidationError, ValueError) as exc:
            msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            return RedirectResponse(f"/clips/{cid}?error={msg[:200]}", status_code=303)
        return RedirectResponse(f"/clips/{cid}", status_code=303)

    @app.post("/clips/{cid}/metadata")
    async def clip_metadata(request: Request, cid: str) -> Response:
        """YouTube requires that users can set title and description; any edit is gated again."""
        s = need(request)
        form = await need_csrf(request, s)
        try:
            pipeline.regate(cid, str(form.get("title", ""))[:100], str(form.get("description", "")).replace("\r\n", "\n")[:5000], s["u"])
        except (PipelineError, ValidationError, ValueError) as exc:
            msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            return RedirectResponse(f"/clips/{cid}?error={msg[:200]}", status_code=303)
        return RedirectResponse(f"/clips/{cid}", status_code=303)

    @app.get("/media/{sha}")
    def media(request: Request, sha: str) -> FileResponse:
        need(request)
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise HTTPException(404)
        referenced = store.conn.execute("SELECT 1 FROM clips WHERE clip_sha=? OR preview_sha=? OR instr(posters, ?) > 0",
                                        (sha, sha, f'": "{sha}"')).fetchone()
        if not referenced:
            raise HTTPException(404)
        try:
            path = store.find_media(sha)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404) from exc
        return FileResponse(path, media_type="image/jpeg" if path.suffix == ".jpg" else "video/mp4")

    # ------------------------------------------------------------------ audit, settings, analytics
    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request) -> HTMLResponse:
        s = need(request)
        ok, bad = store.audit.verify()
        return page(request, "audit.html", s, entries=store.audit.entries(limit=300), ok=ok, bad=bad, head=store.audit.head())

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        s = need(request)
        try:
            has_token = open_vault(store.root).load(TOKEN_NAME) is not None
        except VaultError:
            has_token = None
        ready, why = live_ready(store, settings)
        proposals = store.rows("weight_proposals", order="id DESC")
        return page(request, "settings.html", s, has_token=has_token, ready=ready, why=why, settings=settings,
                    weights=normalise(store.get("weights") or settings.scoring.weights), proposals=proposals)

    @app.post("/settings/kill")
    async def kill(request: Request) -> Response:
        s = need(request)
        form = await need_csrf(request, s)
        store.set_kill_switch(form.get("state") == "on", s["u"])
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/revoke")
    async def revoke_token(request: Request) -> Response:
        s = need(request)
        await need_csrf(request, s)
        from ..publish.youtube import revoke

        ok = revoke(open_vault(store.root))
        store.audit.append(s["u"], "oauth_revoke", "youtube", {"google_ok": ok})
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/weights/{pid}")
    async def weight_decision(request: Request, pid: int) -> Response:
        s = need(request)
        form = await need_csrf(request, s)
        row = store.conn.execute("SELECT proposed, status, current FROM weight_proposals WHERE id=?", (pid,)).fetchone()
        if not row or row[1] != "pending":
            raise HTTPException(404)
        approve = form.get("decision") == "approve"
        live = normalise(store.get("weights") or settings.scoring.weights)
        if approve and any(abs(live[k] - v) > 1e-6 for k, v in normalise(json.loads(row[2])).items()):
            # Proposed against weights that have since changed: applying it could exceed the ±0.05 step.
            with store.lock, store.conn:
                store.conn.execute("UPDATE weight_proposals SET status='stale', decided_by=? WHERE id=?", (s["u"], pid))
            store.audit.append(s["u"], "weights_stale", f"proposal-{pid}", {})
            return RedirectResponse("/settings", status_code=303)
        with store.lock, store.conn:
            store.conn.execute("UPDATE weight_proposals SET status=?, decided_by=? WHERE id=?",
                               ("approved" if approve else "rejected", s["u"], pid))
        if approve:
            store.set("weights", normalise(json.loads(row[0])))
        store.audit.append(s["u"], "weights_" + ("approved" if approve else "rejected"), f"proposal-{pid}", {"proposed": json.loads(row[0])})
        return RedirectResponse("/settings", status_code=303)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request) -> HTMLResponse:
        s = need(request)
        data = store.get("analytics") or {}
        charts = []
        for cid, a in data.items():
            tau, r = a["tau"], a["ratio"]
            t_drop, drop = biggest_drop(tau, r, skip=0.1)
            notes = [(t_drop, f"steepest drop after the hook ({drop:.0%}/step)")] + [tuple(n) for n in a.get("notes", [])]
            charts.append({"clip": cid, "svg": retention_svg(tau, r, notes), "apv": area_under_retention(tau, r),
                           "stats": a.get("stats", {}), "illustrative": a.get("illustrative", False)})
        return page(request, "analytics.html", s, charts=charts)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"ok": "true"}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"] if exc.headers else "/login", status_code=303)
        return Response(str(exc.detail or exc.status_code), status_code=exc.status_code)

    return app
