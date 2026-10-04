"""The Studio in the app: /studio/ (the same page as the browser build) and its JSON API.

The page sends the video once; FFmpeg runs in the media sandbox (Docker or the media worker),
never on the host for untrusted files. Every request needs the dashboard session cookie, and
every POST the CSRF token in the X-CSRF-Token header. Nothing here publishes: the result is a
download, and every clip has passed the full compliance gate.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .. import studio
from ..config import Settings
from ..media.sandbox import MediaToolError, SandboxUnavailable
from ..pipeline import REPO_ROOT, Pipeline
from ..store import Store
from ..studio_run import Made, StudioError, analyze_source, make_clip, package_files, zip_bytes
from . import security as sec

HERE = Path(__file__).resolve().parent
UI = HERE / "static" / "studio"
EXAMPLE = REPO_ROOT / "examples" / "studio"
EXAMPLE_FILES = {"source.mp4": "video/mp4", "captions.srt": "text/plain; charset=utf-8", "project.json": "application/json"}
KEEP_SESSIONS = 3
STUDIO_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
              "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _fail(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def register(app: FastAPI, *, store: Store, pipeline: Pipeline, settings: Settings,
             session: Callable[[Request], dict | None]) -> None:
    root = store.root / "studio"
    sessions: dict[str, dict[str, Any]] = {}
    jobs: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def who(request: Request) -> dict | None:
        return session(request)

    def csrf(request: Request, s: dict) -> bool:
        return sec.csrf_ok(s, request.headers.get("x-csrf-token"))

    def prune() -> None:
        with lock:
            for sid in sorted(sessions, key=lambda k: sessions[k]["created"])[:-KEEP_SESSIONS]:
                shutil.rmtree(root / sid, ignore_errors=True)
                sessions.pop(sid, None)
                for jid in [j for j, v in jobs.items() if v["session"] == sid]:
                    jobs.pop(jid, None)

    @app.get("/studio")
    def studio_redirect() -> Response:
        return RedirectResponse("/studio/", status_code=307)

    @app.get("/studio/", response_class=HTMLResponse)
    def studio_page(request: Request) -> Response:
        s = who(request)
        if not s:
            return RedirectResponse("/login", status_code=303)
        page = (UI / "index.html").read_text("utf-8")
        page = page.replace('<meta name="fairclip-backend" content="web">', '<meta name="fairclip-backend" content="app">')
        page = page.replace('<meta name="fairclip-csrf" content="">', f'<meta name="fairclip-csrf" content="{html.escape(s["csrf"])}">')
        # The page plays the chosen video from a blob: URL before anything is sent, so media-src
        # needs blob: here (the dashboard's default CSP doesn't allow it).
        return HTMLResponse(page, headers={"Content-Security-Policy": STUDIO_CSP})

    @app.get("/studio/example/{name}")
    def studio_example(request: Request, name: str) -> Response:
        if not who(request):
            return Response(status_code=401)
        if name not in EXAMPLE_FILES or not (EXAMPLE / name).is_file():
            return Response(status_code=404)
        return FileResponse(EXAMPLE / name, media_type=EXAMPLE_FILES[name])

    @app.post("/api/studio/analyze")
    async def studio_analyze(request: Request, video: UploadFile = File(...), captions: str = Form(""),  # noqa: B008
                             meta: str = Form(...)) -> JSONResponse:
        s = who(request)
        if not s:
            return _fail("signed out", 401)
        if not csrf(request, s):
            return _fail("CSRF token missing or wrong", 403)
        try:
            m = json.loads(meta)
            brand = studio.Brand.model_validate(m.get("brand") or {})
            choice = studio.RightsChoice.model_validate(m.get("rights") or {})
        except (ValueError, ValidationError) as exc:
            return _fail(f"check the form: {str(exc)[:400]}")
        if len(captions) > 10 * 1024 * 1024:
            return _fail("captions file larger than 10 MB")
        sid = secrets.token_urlsafe(12)
        d = root / sid
        d.mkdir(parents=True, exist_ok=True)
        name = SAFE_NAME.sub("_", Path(video.filename or "video.mp4").name)[-80:] or "video.mp4"
        dest = d / name
        limit, size = settings.media.max_input_bytes, 0
        with dest.open("wb") as out:
            while chunk := await video.read(8 * 1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close()
                    shutil.rmtree(d, ignore_errors=True)
                    return _fail(f"that video is over the {limit // 1024**3} GB limit set in fairclip.toml")
                out.write(chunk)
        st = studio.studio_settings(brand, monetized=settings.channel.monetized)
        st.media = settings.media
        try:
            sess = await run_in_threadpool(analyze_source, dest, captions, brand, choice, runner=pipeline.runner,
                                           work=pipeline.work, settings=st)
        except (StudioError, ValueError) as exc:
            shutil.rmtree(d, ignore_errors=True)
            return _fail(str(exc)[:600])
        except SandboxUnavailable as exc:
            shutil.rmtree(d, ignore_errors=True)
            return _fail(f"the media sandbox isn't available: {exc}", 503)
        except MediaToolError as exc:
            shutil.rmtree(d, ignore_errors=True)
            return _fail(f"FFmpeg couldn't read that video: {str(exc)[-300:]}")
        with lock:
            sessions[sid] = {"created": time.time(), "session": sess, "user": s.get("u")}
        store.audit.append(s.get("u", "?"), "studio_analyze", f"src-{sess.sha[:12]}",
                           {"rights_record": sess.rights.record_id, "basis": sess.rights.basis.value, "sha256": sess.sha,
                            "moments": len(sess.candidates)})
        prune()
        return JSONResponse({"ok": True, "session": sid, "candidates": studio.candidates_json(sess.candidates, sess.tr, brand),
                             "credit": studio.rights_mod.attribution_line(sess.rights, short=True),
                             "synthetic": sess.rights.synthetic_or_altered})

    def owned(sid: Any, s: dict) -> dict[str, Any] | None:
        with lock:
            v = sessions.get(sid) if isinstance(sid, str) else None
        return v if v and v["user"] == s.get("u") else None

    @app.post("/api/studio/title-check")
    async def studio_title(request: Request) -> JSONResponse:
        s = who(request)
        if not s:
            return _fail("signed out", 401)
        if not csrf(request, s):
            return _fail("CSRF token missing or wrong", 403)
        body = await request.json()
        v = owned(body.get("session"), s)
        if not v:
            return _fail("this Studio session has expired: start again from step 1", 404)
        sess = v["session"]
        try:
            i = int(body.get("i", -1))
            c = sess.candidates[i] if 0 <= i < len(sess.candidates) else None
            if c is None:
                return _fail("no such moment")
            plan = studio.build_plan(c, studio.take_from_json(body.get("take") or {}), sess.brand, i + 1)
        except (ValueError, ValidationError) as exc:
            return _fail(str(exc)[:400])
        title = str(body.get("title") or "")[:200]
        probs = studio.title_problems(title, plan, sess.tr, sess.rms, sess.brand.channel) if title.strip() else []
        return JSONResponse({"ok": True, "problems": probs})

    @app.post("/api/studio/make")
    async def studio_make(request: Request) -> JSONResponse:
        s = who(request)
        if not s:
            return _fail("signed out", 401)
        if not csrf(request, s):
            return _fail("CSRF token missing or wrong", 403)
        body = await request.json()
        v = owned(body.get("session"), s)
        if not v:
            return _fail("this Studio session has expired: start again from step 1", 404)
        sess = v["session"]
        quality = body.get("quality") if body.get("quality") in ("fast", "high") else "high"
        todo = []
        try:
            for c in (body.get("clips") or [])[:8]:
                i = int(c.get("i", -1))
                if not 0 <= i < len(sess.candidates):
                    return _fail("no such moment")
                todo.append((i, studio.take_from_json(c.get("take") or {}), str(c.get("title") or "")[:200]))
        except (ValueError, ValidationError, AttributeError) as exc:
            return _fail(str(exc)[:400])
        if not todo:
            return _fail("pick at least one moment")
        jid = secrets.token_urlsafe(12)
        job: dict[str, Any] = {"session": body["session"], "user": s.get("u"), "state": "running", "done": [], "made": [],
                               "current": {"k": 0, "progress": None}, "files": None, "error": ""}
        with lock:
            jobs[jid] = job

        def work() -> None:
            try:
                for k, (i, take, title) in enumerate(todo):
                    job["current"] = {"k": k, "progress": None}
                    m: Made = make_clip(sess, sess.candidates[i], take, title, i + 1, runner=pipeline.runner,
                                        work=pipeline.work, quality=quality)
                    job["made"].append(m)
                    job["done"].append(m.finished.to_json() | {"text": studio.clip_text(m.finished)})
                job["files"] = package_files(job["made"], sess.brand)
                job["state"] = "done"
                store.audit.append(job["user"] or "?", "studio_make", f"src-{sess.sha[:12]}",
                                   {"clips": [{"clip_id": m.finished.plan.clip_id, "passed": m.finished.passed,
                                               "blocks": [r.code for r in m.finished.report.blocks]} for m in job["made"]]})
            except (StudioError, ValueError, MediaToolError, SandboxUnavailable) as exc:
                job["state"], job["error"] = "error", str(exc)[:600]
            except Exception as exc:  # noqa: BLE001 - surfaced to the page, details in the server log
                job["state"], job["error"] = "error", f"unexpected error: {type(exc).__name__}"
                raise

        threading.Thread(target=work, daemon=True, name=f"studio-{jid}").start()
        return JSONResponse({"ok": True, "job": jid})

    def own_job(jid: str, s: dict) -> dict[str, Any] | None:
        with lock:
            j = jobs.get(jid)
        return j if j and j["user"] == s.get("u") else None

    @app.get("/api/studio/job/{jid}")
    def studio_job(request: Request, jid: str) -> JSONResponse:
        s = who(request)
        if not s:
            return _fail("signed out", 401)
        j = own_job(jid, s)
        if not j:
            return _fail("no such job", 404)
        return JSONResponse({"ok": True, "state": j["state"], "done": j["done"], "current": j["current"],
                             "files": j["files"], "error": j["error"]})

    @app.get("/api/studio/job/{jid}/clip/{k}")
    def studio_clip(request: Request, jid: str, k: int) -> Response:
        s = who(request)
        j = own_job(jid, s) if s else None
        if not j or not 0 <= k < len(j["made"]) or j["made"][k].clip is None:
            return Response(status_code=404)
        m = j["made"][k]
        return FileResponse(m.clip, media_type="video/mp4", filename=m.finished.filename)

    @app.get("/api/studio/job/{jid}/zip")
    def studio_zip(request: Request, jid: str) -> Response:
        s = who(request)
        j = own_job(jid, s) if s else None
        if not j or j["state"] != "done":
            return Response(status_code=404)
        with lock:
            sess = sessions.get(j["session"])
        brand = sess["session"].brand if sess else studio.Brand(channel="fair-clip")
        return Response(zip_bytes(j["made"], brand), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="fair-clip-clips.zip"'})

    app.mount("/studio", StaticFiles(directory=UI), name="studio")
