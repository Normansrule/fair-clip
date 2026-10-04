"""Stage 8 dashboard: pipeline status, side-by-side review, compliance report, approve/reject.

Runs on 127.0.0.1 only. Server-rendered HTML (Jinja2 autoescaping on) with no inline script.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ..affiliate import (
    TERMS_STALE_DAYS,
    AffiliateRegistry,
    Product,
    Program,
    RegisteredLink,
    load_registry,
    update_registry,
)
from ..analytics import area_under_retention, biggest_drop
from ..analytics_api import ANALYTICS_TOKEN, propose_from_analytics, pull, training_rows
from ..approval import ApprovalError, decide, human_required
from ..config import Settings
from ..gate.reasons import GROUP_ORDER, REASONS
from ..models import ClipPlan, Decision, GateReport, Visibility
from ..pipeline import STAGES, Pipeline, PipelineError
from ..publish.body import paid_promotion_required
from ..publish.publisher import live_ready
from ..publish.youtube import TOKEN_NAME
from ..redact import redact
from ..scoring import MIN_CLIPS_FOR_UPDATE, normalise
from ..store import Store
from ..vault import VaultError, open_vault
from . import security as sec
from . import studio_api
from .charts import retention_svg, score_bars_svg

HERE = Path(__file__).resolve().parent


def parse_times(text: str) -> list[float]:
    """'17, 32.5' -> [17.0, 32.5] (clip seconds, at most 10)."""
    out = [float(x) for x in re.split(r"[,\s]+", text.strip()) if x]
    if len(out) > 10 or any(not 0 <= t <= 3600 for t in out):
        raise ValueError("recommendation times: up to 10 numbers of seconds, e.g. '17, 32.5'")
    return out


def parse_span(text: str) -> list[float] | None:
    """'0.3-5' -> [0.3, 5.0]; empty -> None."""
    if not text.strip():
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*", text)
    if not m or float(m.group(2)) <= float(m.group(1)):
        raise ValueError("start card: write it as 'from-to' in seconds, e.g. '0.3-5'")
    return [float(m.group(1)), float(m.group(2))]


def create_app(settings: Settings, store: Store | None = None, extra_hosts: tuple[str, ...] = (),
               setup_token: str | None = None) -> FastAPI:
    """`setup_token` (from `fair-clip app`): on first run, instead of printing a generated password,
    allow ONE visit to /setup?t=<token> to choose it. The token lives only in memory and dies on use."""
    store = store or Store(settings.data_path)
    sessions = sec.Sessions(sec.session_key(store.root), store)
    pipeline = Pipeline(settings, store)
    throttle = sec.Throttle()
    hosts = sec.allowed_hosts(settings.dashboard.port) - {"testserver"} | set(extra_hosts)
    setup = {"token": setup_token if setup_token and not store.get("dashboard_pw") else None}
    setup_lock = threading.Lock()
    setup_throttle = sec.Throttle()  # separate from login, so a stale setup tab can't lock you out of /login
    first_pw = None if setup["token"] else sec.ensure_password(store)
    if first_pw:
        print("\n  First run: dashboard password (shown once, change it with `fair-clip set-password`):\n"
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

    def setup_ok(t: str) -> bool:
        return bool(setup["token"] and t and hmac.compare_digest(setup["token"], t) and not store.get("dashboard_pw"))

    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request) -> Response:
        t = request.query_params.get("t", "")
        if not setup_ok(t):
            return RedirectResponse("/login", status_code=303)
        return page(request, "setup.html", None, token=t, error=None)

    @app.post("/setup")
    def setup_submit(request: Request, t: str = Form(...), password: str = Form(...), password2: str = Form(...)) -> Response:
        who = request.client.host if request.client else "?"
        if setup_throttle.blocked(who) or not setup_ok(t):
            setup_throttle.fail(who)
            return RedirectResponse("/login", status_code=303)
        if password != password2:
            return page(request, "setup.html", None, token=t, error="The two passwords differ.")
        if len(password) < 12:
            return page(request, "setup.html", None, token=t, error="password must be at least 12 characters")
        with setup_lock:  # check-and-consume atomically: two racing posts can't both set a password
            if not setup_ok(t):
                return RedirectResponse("/login", status_code=303)
            setup["token"] = None  # single use
            sec.set_password(store, password)
        store.audit.append("owner", "set_password", "dashboard", {"via": "first-run setup link"})
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
        reg = load_registry(store)
        try:
            heard = pipeline.heard_disclosures(cid)
        except (PipelineError, OSError, ValueError):
            heard = []
        return page(request, "clip.html", s, row=row, plan=plan, gate=gate, grouped=grouped,
                    paid=paid_promotion_required(plan), human_only=human_required(plan, settings),
                    visibilities=[v.value for v in Visibility], error=request.query_params.get("error"),
                    registry=reg, usable=set(reg.usable_links()), heard=heard,
                    chosen={lk.url for lk in (plan.affiliate.links if plan.affiliate else [])})

    @app.post("/clips/{cid}/affiliate")
    async def clip_affiliate(request: Request, cid: str) -> Response:
        """Edit the clip's affiliate section from registered links, then re-render and re-gate."""
        s = need(request)
        form = await request.form()
        if not sec.csrf_ok(s, str(form.get("csrf", ""))):
            raise HTTPException(status_code=403, detail="CSRF token missing or wrong")
        reg = load_registry(store)
        try:
            spoken = str(form.get("spoken_at", "") or "")
            urls = [str(u) for u in form.getlist("link")][:10]
            links = []
            for u in urls:
                lk = reg.link(u)
                if lk is None:
                    raise ValueError(f"{u} isn't a registered link")
                links.append({"program_id": lk.program_id, "url": lk.url, "product": lk.product})
            rec_times = parse_times(str(form.get("recommendation_times", "")))
            changes: dict[str, Any] = {
                "tags": sorted(set(ClipPlan.model_validate(store.row("clips", cid)["plan"]).tags) | {"affiliate"}),
                "affiliate": {
                    "links": links,
                    "disclosure_text": str(form.get("disclosure_text", "")).strip()[:200],
                    "start_card": parse_span(str(form.get("start_card", ""))),
                    "spoken_disclosure_at": float(spoken) if spoken else None,  # only what the owner ticked
                    "recommendation_times": rec_times,
                    # one readable card per recommendation: from 0.5 s before to 3.5 s after
                    "recommendation_cards": [[max(0.0, t - 0.5), t + 3.5] for t in rec_times],
                },
            }
            await run_in_threadpool(pipeline.edit_plan, cid, changes, s["u"])
        except (PipelineError, ValidationError, ValueError, KeyError, TypeError) as exc:
            msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            return RedirectResponse(f"/clips/{cid}?error={quote(redact(msg)[:200])}", status_code=303)
        return RedirectResponse(f"/clips/{cid}#affiliate", status_code=303)

    # ------------------------------------------------------------------ affiliate registry
    @app.get("/affiliate", response_class=HTMLResponse)
    def affiliate_page(request: Request) -> HTMLResponse:
        s = need(request)
        today = datetime.now(UTC).date()
        return page(request, "affiliate.html", s, reg=load_registry(store), today=today, stale_days=TERMS_STALE_DAYS,
                    error=request.query_params.get("error", "")[:300])

    @app.post("/affiliate/{action}")
    async def affiliate_change(request: Request, action: str) -> Response:
        s = need(request)
        form = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in (await need_csrf(request, s)).items()}
        today = datetime.now(UTC).date()

        def change(reg: AffiliateRegistry) -> AffiliateRegistry:
            if action == "program":
                # Editing keeps what only the dedicated buttons may change: paused state and the
                # "terms re-read" date. A NEW program counts as read today (you just joined it).
                old = reg.program(str(form.get("id", "")))
                prog = Program.model_validate({
                    "id": form.get("id", ""), "name": form.get("name", ""), "terms_url": form.get("terms_url", ""),
                    "joined_on": form.get("joined_on") or (old.joined_on.isoformat() if old else today.isoformat()),
                    "terms_reviewed_on": old.terms_reviewed_on.isoformat() if old else today.isoformat(),
                    "active": old.active if old else True,
                    "link_prefixes": [x.strip() for x in str(form.get("link_prefixes", "")).splitlines() if x.strip()],
                    "required_wording": form.get("required_wording", ""), "notes": form.get("notes", "")})
                reg.programs = [p for p in reg.programs if p.id != prog.id] + [prog]
            elif action == "terms-reviewed":
                prog = reg.program(str(form.get("id", "")))
                if prog is None:
                    raise ValueError("no such program")
                prog.terms_reviewed_on = today
            elif action == "pause":
                prog = reg.program(str(form.get("id", "")))
                if prog is None:
                    raise ValueError("no such program")
                prog.active = not prog.active
            elif action == "product":
                old_p = reg.product(str(form.get("name", "")))
                prod = Product.model_validate({
                    "name": form.get("name", ""),
                    "evaluated_on": form.get("evaluated_on") or (old_p.evaluated_on.isoformat() if old_p else today.isoformat()),
                    "how_evaluated": form.get("how_evaluated", ""), "retired": old_p.retired if old_p else False})
                reg.products = [p for p in reg.products if p.name.lower() != prod.name.lower()] + [prod]
            elif action == "retire":
                prod = reg.product(str(form.get("name", "")))
                if prod is None:
                    raise ValueError("no such product")
                prod.retired = not prod.retired
            elif action == "link":
                lk = RegisteredLink.model_validate({"url": form.get("url", ""), "program_id": form.get("program_id", ""),
                                                    "product": form.get("product", ""), "added_on": today.isoformat()})
                reg.links = [x for x in reg.links if x.url != lk.url] + [lk]
            elif action == "unlink":
                reg.links = [x for x in reg.links if x.url != form.get("url")]
            else:
                raise HTTPException(404)
            return reg

        try:
            update_registry(store, change, s["u"], action.replace("-", "_"),
                            {k: v for k, v in form.items() if k in {"id", "name", "url", "program_id", "product"}})
        except (ValidationError, ValueError) as exc:
            msg = "; ".join(e["msg"] for e in exc.errors()[:2]) if isinstance(exc, ValidationError) else str(exc)
            return RedirectResponse("/affiliate?error=" + quote(msg[:300]), status_code=303)
        return RedirectResponse("/affiliate", status_code=303)

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
            vault = open_vault(store.root)
            has_token = vault.load(TOKEN_NAME) is not None
            has_analytics = vault.load(ANALYTICS_TOKEN) is not None
        except VaultError:
            has_token = has_analytics = None
        ready, why = live_ready(store, settings)
        live = normalise(store.get("weights") or settings.scoring.weights)
        proposals = []
        for row in store.rows("weight_proposals", order="id DESC"):
            prop, cur = row.get("proposed") or {}, row.get("current") or {}
            row["diff"] = [(k, float(cur.get(k, 0)), float(prop.get(k, 0))) for k in live]
            proposals.append(row)
        return page(request, "settings.html", s, has_token=has_token, has_analytics=has_analytics, ready=ready, why=why,
                    settings=settings, weights=live, proposals=proposals, training_n=len(training_rows(store)),
                    min_n=MIN_CLIPS_FOR_UPDATE, error=request.query_params.get("error", "")[:200])

    @app.post("/settings/weights-propose")
    async def weights_propose(request: Request) -> Response:
        s = need(request)
        await need_csrf(request, s)
        try:
            propose_from_analytics(store, normalise(store.get("weights") or settings.scoring.weights), s["u"])
        except ValueError as exc:
            return RedirectResponse(f"/settings?error={quote(str(exc)[:200])}", status_code=303)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/analytics-revoke")
    async def analytics_revoke(request: Request) -> Response:
        s = need(request)
        await need_csrf(request, s)
        from ..publish.youtube import revoke

        ok = revoke(open_vault(store.root), token_name=ANALYTICS_TOKEN)
        store.audit.append(s["u"], "oauth_revoke", "youtube-analytics", {"google_ok": ok})
        return RedirectResponse("/settings", status_code=303)

    @app.post("/analytics/pull")
    async def analytics_pull(request: Request) -> Response:
        """Read-only pull with the separate analytics token (runs off the event loop)."""
        s = need(request)
        await need_csrf(request, s)

        def run() -> dict[str, Any]:
            import httpx
            from google.auth.transport.requests import Request as GRequest

            from ..publish.youtube import credentials

            creds = credentials(Path(settings.publish.oauth_client_file), open_vault(store.root), token_name=ANALYTICS_TOKEN)
            creds.refresh(GRequest())
            with httpx.Client(timeout=30) as http:
                return pull(store, http, creds.token, actor=s["u"])

        try:
            res = await run_in_threadpool(run)
        except ImportError:
            return RedirectResponse("/analytics?msg=" + quote("install the youtube extra: pip install '.[youtube]'"), status_code=303)
        except Exception as exc:  # noqa: BLE001 - shown to the owner, redacted
            return RedirectResponse("/analytics?msg=" + quote(redact(str(exc))[:200]), status_code=303)
        msg = f"pulled {res['clips']} clip(s); {res['no_retention_yet']} without a retention curve yet"
        if res["errors"]:
            msg += f"; {len(res['errors'])} error(s): " + res["errors"][0]
        return RedirectResponse("/analytics?msg=" + quote(msg[:300]), status_code=303)

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
            tau, r = a.get("tau") or [], a.get("ratio") or []
            if len(tau) < 3 or len(tau) != len(r):  # new uploads: totals only, no curve yet
                charts.append({"clip": cid, "svg": "", "apv": None, "stats": a.get("stats", {}),
                               "illustrative": a.get("illustrative", False)})
                continue
            t_drop, drop = biggest_drop(tau, r, skip=0.1)
            notes = [(t_drop, f"steepest drop after the hook ({drop:.0%}/step)")] + [tuple(n) for n in a.get("notes", [])]
            charts.append({"clip": cid, "svg": retention_svg(tau, r, notes), "apv": area_under_retention(tau, r),
                           "stats": a.get("stats", {}), "illustrative": a.get("illustrative", False)})
        try:
            connected = open_vault(store.root).load(ANALYTICS_TOKEN) is not None
        except VaultError:
            connected = None
        return page(request, "analytics.html", s, charts=charts, connected=connected,
                    msg=request.query_params.get("msg", "")[:300])

    studio_api.register(app, store=store, pipeline=pipeline, settings=settings, session=session)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"ok": "true"}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"] if exc.headers else "/login", status_code=303)
        return Response(str(exc.detail or exc.status_code), status_code=exc.status_code)

    return app
