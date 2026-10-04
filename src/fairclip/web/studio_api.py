"""The Studio in the app: /studio/ (the same page as the browser build) and its JSON API.

The page sends the video once; FFmpeg runs in the media sandbox (Docker or the media worker),
never on the host for untrusted files. Every request needs the dashboard session cookie, and
every POST the CSRF token in the X-CSRF-Token header. Nothing here publishes: the result is a
download, and every clip has passed the full compliance gate with the owner's own settings,
screen terms, kill switch, publishing history and affiliate registry.

Resources: one render at a time; each Studio session has its own folder (upload + every FFmpeg
job + the zip) that is deleted as a whole; at most KEEP_SESSIONS per user and a TTL for the rest.
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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import studio
from ..config import Settings
from ..media.sandbox import MediaToolError, SandboxUnavailable
from ..pipeline import REPO_ROOT, Pipeline
from ..store import Store
from ..studio_run import (
    Made,
    StudioError,
    analyze_source,
    gate_inputs,
    make_clip,
    moment_posters,
    owner_settings,
    package_files,
    write_zip,
)
from . import security as sec

HERE = Path(__file__).resolve().parent
UI = HERE / "static" / "studio"
EXAMPLE = REPO_ROOT / "examples" / "studio"
EXAMPLE_FILES = {"source.mp4": "video/mp4", "captions.srt": "text/plain; charset=utf-8", "project.json": "application/json"}
KEEP_SESSIONS = 3  # per user
SESSION_TTL_S = 12 * 3600
MAX_CAPTIONS = 10 * 1024 * 1024
FORM_SLACK = 64 * 1024  # multipart framing + the small fields
STUDIO_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
              "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _fail(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def safe_name(name: str | None) -> str:
    n = SAFE_NAME.sub("_", Path(name or "").name)[-80:].lstrip(".")
    return n or "video.mp4"


def _index(v: Any, n: int) -> int:
    """A moment index from the page: an int in range, else ValueError."""
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < n:
        raise ValueError("no such moment")
    return v


def register(app: FastAPI, *, store: Store, pipeline: Pipeline, settings: Settings,
             session: Callable[[Request], dict | None]) -> None:
    root = store.root / "studio"
    sessions: dict[str, dict[str, Any]] = {}
    jobs: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    render_slot = threading.Semaphore(1)  # one render at a time, whoever asks

    def csrf(request: Request, s: dict) -> bool:
        return sec.csrf_ok(s, request.headers.get("x-csrf-token"))

    def work_dir(sid: str) -> Path:
        # FFmpeg job folders live under the pipeline's work root (the shared volume in worker mode).
        return pipeline.work / f"studio-{sid}"

    def busy(sid: str) -> bool:
        return any(j["session"] == sid and j["state"] == "running" for j in jobs.values())

    def drop(sid: str) -> None:  # caller holds the lock
        sessions.pop(sid, None)
        for jid in [j for j, v in jobs.items() if v["session"] == sid]:
            jobs.pop(jid, None)
        shutil.rmtree(root / sid, ignore_errors=True)
        shutil.rmtree(work_dir(sid), ignore_errors=True)

    def prune(user: str | None) -> None:
        now = time.time()
        with lock:
            for sid, v in list(sessions.items()):
                if now - v["created"] > SESSION_TTL_S and not busy(sid):
                    drop(sid)
            mine = sorted((k for k, v in sessions.items() if v["user"] == user), key=lambda k: sessions[k]["created"])
            for sid in mine[:-KEEP_SESSIONS]:
                if not busy(sid):
                    drop(sid)

    @app.get("/studio")
    def studio_redirect() -> Response:
        return RedirectResponse("/studio/", status_code=307)

    @app.get("/studio/", response_class=HTMLResponse)
    def studio_page(request: Request) -> Response:
        s = session(request)
        if not s:
            return RedirectResponse("/login", status_code=303)
        page = (UI / "index.html").read_text("utf-8")
        page = page.replace('<meta name="fairclip-backend" content="web">', '<meta name="fairclip-backend" content="app">')
        page = page.replace('<meta name="fairclip-csrf" content="">', f'<meta name="fairclip-csrf" content="{html.escape(s["csrf"])}">')
        # The page plays the chosen video from a blob: URL before anything is sent, so media-src
        # needs blob: here (the dashboard's default CSP doesn't allow it).
        return HTMLResponse(page, headers={"Content-Security-Policy": STUDIO_CSP})

    @app.api_route("/studio/example/{name}", methods=["GET", "HEAD"])
    def studio_example(request: Request, name: str) -> Response:
        if not session(request):
            return Response(status_code=401)
        if name not in EXAMPLE_FILES or not (EXAMPLE / name).is_file():
            return Response(status_code=404)
        return FileResponse(EXAMPLE / name, media_type=EXAMPLE_FILES[name])

    @app.post("/api/studio/analyze")
    async def studio_analyze(request: Request) -> JSONResponse:
        s = session(request)
        if not s:
            return _fail("signed out", 401)
        if not csrf(request, s):
            return _fail("CSRF token missing or wrong", 403)
        limit = settings.media.max_input_bytes
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > limit + MAX_CAPTIONS + FORM_SLACK:  # refuse before anything is written to disk
            return _fail(f"that video is over the {limit / 1024**3:g} GB limit set in fairclip.toml", 413)
        form = await request.form(max_files=2, max_fields=4, max_part_size=FORM_SLACK)
        video, cap, meta = form.get("video"), form.get("captions"), form.get("meta")
        if not isinstance(video, UploadFile) or not isinstance(meta, str):
            return _fail("send a video and the form")
        try:
            m = json.loads(meta)
            if not isinstance(m, dict):
                raise ValueError("the form must be an object")
            brand = studio.Brand.model_validate(m.get("brand") or {})
            choice = studio.RightsChoice.model_validate(m.get("rights") or {})
        except (ValueError, ValidationError) as exc:
            return _fail(f"check the form: {str(exc)[:400]}")
        captions = ""
        if isinstance(cap, UploadFile):
            raw = await cap.read(MAX_CAPTIONS + 1)
            if len(raw) > MAX_CAPTIONS:
                return _fail("captions file larger than 10 MB")
            captions = raw.decode("utf-8", "replace")
        sid = secrets.token_urlsafe(12)
        d = root / sid
        d.mkdir(parents=True, exist_ok=True)
        work_dir(sid).mkdir(parents=True, exist_ok=True)
        try:
            dest, size = d / safe_name(video.filename), 0
            with dest.open("wb") as out:
                while chunk := await video.read(8 * 1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise StudioError(f"that video is over the {limit / 1024**3:g} GB limit set in fairclip.toml")
                    out.write(chunk)
            st = owner_settings(settings, brand)
            sess = await run_in_threadpool(analyze_source, dest, captions, brand, choice, runner=pipeline.runner,
                                           work=work_dir(sid), settings=st)
        except (StudioError, ValueError) as exc:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(work_dir(sid), ignore_errors=True)
            return _fail(str(exc)[:600])
        except SandboxUnavailable as exc:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(work_dir(sid), ignore_errors=True)
            return _fail(f"the media sandbox isn't available: {exc}", 503)
        except MediaToolError as exc:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(work_dir(sid), ignore_errors=True)
            return _fail(f"FFmpeg couldn't read that video: {str(exc)[-300:]}")
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(work_dir(sid), ignore_errors=True)
            raise
        posters = await run_in_threadpool(moment_posters, sess, runner=pipeline.runner, work=work_dir(sid))
        with lock:
            sessions[sid] = {"created": time.time(), "session": sess, "user": s.get("u"), "posters": posters}
        store.audit.append(s.get("u", "?"), "studio_analyze", f"src-{sess.sha[:12]}",
                           {"rights_record": sess.rights.record_id, "basis": sess.rights.basis.value, "sha256": sess.sha,
                            "moments": len(sess.candidates)})
        prune(s.get("u"))
        return JSONResponse({"ok": True, "session": sid, "candidates": studio.candidates_json(sess.candidates, sess.tr, brand),
                             "credit": studio.rights_mod.attribution_line(sess.rights, short=True),
                             "synthetic": sess.rights.synthetic_or_altered})

    def owned(sid: Any, s: dict) -> dict[str, Any] | None:
        with lock:
            v = sessions.get(sid) if isinstance(sid, str) else None
        return v if v and v["user"] == s.get("u") else None

    async def json_body(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body

    @app.post("/api/studio/title-check")
    async def studio_title(request: Request) -> JSONResponse:
        s = session(request)
        if not s:
            return _fail("signed out", 401)
        if not csrf(request, s):
            return _fail("CSRF token missing or wrong", 403)
        try:
            body = await json_body(request)
            v = owned(body.get("session"), s)
            if not v:
                return _fail("this Studio session has expired: start again from step 1", 404)
            sess = v["session"]
            i = _index(body.get("i"), len(sess.candidates))
            take = body.get("take")
            plan = studio.build_plan(sess.candidates[i], studio.take_from_json(take if isinstance(take, dict) else {}), sess.brand, i + 1)
        except (ValueError, ValidationError) as exc:
            return _fail(str(exc)[:400])
        title = str(body.get("title") or "")[:200]
        probs = studio.title_problems(title, plan, sess.tr, sess.rms, sess.brand.channel) if title.strip() else []
        return JSONResponse({"ok": True, "problems": probs})

    @app.post("/api/studio/make")
    async def studio_make(request: Request) -> JSONResponse:
        s = session(request)
        if not s:
            return _fail("signed out", 401)
        if not csrf(request, s):
            return _fail("CSRF token missing or wrong", 403)
        try:
            body = await json_body(request)
            v = owned(body.get("session"), s)
            if not v:
                return _fail("this Studio session has expired: start again from step 1", 404)
            sid, sess = body["session"], v["session"]
            if busy(sid):
                return _fail("this session is already making clips: wait for it to finish", 409)
            quality = body.get("quality") if body.get("quality") in ("fast", "high") else "high"
            clips = body.get("clips")
            if not isinstance(clips, list) or not clips:
                return _fail("pick at least one moment")
            todo = []
            for c in clips[:8]:
                if not isinstance(c, dict):
                    raise ValueError("bad clip entry")
                i = _index(c.get("i"), len(sess.candidates))
                take = studio.take_from_json(c.get("take") if isinstance(c.get("take"), dict) else {})
                studio.build_plan(sess.candidates[i], take, sess.brand, i + 1)  # every take is checked before any render
                todo.append((i, take, str(c.get("title") or "")[:200]))
        except (ValueError, ValidationError) as exc:
            return _fail(str(exc)[:500])
        jid = secrets.token_urlsafe(12)
        job: dict[str, Any] = {"session": sid, "user": s.get("u"), "state": "running", "done": [], "made": [],
                               "current": {"k": 0, "progress": None}, "files": None, "error": "", "zip": None}
        with lock:
            jobs[jid] = job
        work = work_dir(sid)
        gate = gate_inputs(pipeline)

        def run() -> None:
            try:
                with render_slot:
                    for k, (i, take, title) in enumerate(todo):
                        job["current"] = {"k": k, "progress": None}
                        m: Made = make_clip(sess, sess.candidates[i], take, title, i + 1, runner=pipeline.runner,
                                            work=work, quality=quality, gate=gate)
                        job["made"].append(m)
                        job["done"].append(m.finished.to_json() | {"text": studio.clip_text(m.finished)})
                    job["files"] = package_files(job["made"], sess.brand)
                    job["zip"] = write_zip(job["made"], sess.brand, root / sid / f"{jid}.zip")
                job["state"] = "done"
                store.audit.append(job["user"] or "?", "studio_make", f"src-{sess.sha[:12]}",
                                   {"clips": [{"clip_id": m.finished.plan.clip_id, "passed": m.finished.passed,
                                               "blocks": [r.code for r in m.finished.report.blocks]} for m in job["made"]]})
            except (StudioError, ValueError, OSError, MediaToolError, SandboxUnavailable) as exc:
                job["state"], job["error"] = "error", str(exc)[:600]
            except Exception as exc:
                job["state"], job["error"] = "error", f"unexpected error: {type(exc).__name__}"
                raise

        threading.Thread(target=run, daemon=True, name=f"studio-{jid}").start()
        return JSONResponse({"ok": True, "job": jid})

    def own_job(jid: str, s: dict | None) -> dict[str, Any] | None:
        with lock:
            j = jobs.get(jid)
        return j if s and j and j["user"] == s.get("u") else None

    @app.get("/api/studio/job/{jid}")
    def studio_job(request: Request, jid: str) -> JSONResponse:
        s = session(request)
        if not s:
            return _fail("signed out", 401)
        j = own_job(jid, s)
        if not j:
            return _fail("this job has expired: start again from step 1", 404)
        return JSONResponse({"ok": True, "state": j["state"], "done": j["done"], "current": j["current"],
                             "files": j["files"], "error": j["error"]})

    @app.get("/api/studio/job/{jid}/clip/{k}")
    def studio_clip(request: Request, jid: str, k: int) -> Response:
        j = own_job(jid, session(request))
        if not j or not 0 <= k < len(j["made"]) or j["made"][k].clip is None:
            return Response(status_code=404)
        m = j["made"][k]
        return FileResponse(m.clip, media_type="video/mp4", filename=m.finished.filename)

    @app.get("/api/studio/session/{sid}/poster/{i}")
    def studio_moment_poster(request: Request, sid: str, i: int) -> Response:
        s = session(request)
        v = owned(sid, s) if s else None
        posters = v.get("posters", []) if v else []
        if not 0 <= i < len(posters) or posters[i] is None or not posters[i].exists():
            return Response(status_code=404)
        return FileResponse(posters[i], media_type="image/jpeg")

    @app.get("/api/studio/job/{jid}/poster/{k}")
    def studio_clip_poster(request: Request, jid: str, k: int) -> Response:
        j = own_job(jid, session(request))
        if not j or not 0 <= k < len(j["made"]) or not j["made"][k].poster or not j["made"][k].poster.exists():
            return Response(status_code=404)
        return FileResponse(j["made"][k].poster, media_type="image/jpeg")

    @app.get("/api/studio/job/{jid}/zip")
    def studio_zip(request: Request, jid: str) -> Response:
        j = own_job(jid, session(request))
        if not j or j["state"] != "done" or not j["zip"] or not Path(j["zip"]).exists():
            return Response(status_code=404)
        return FileResponse(j["zip"], media_type="application/zip", filename="fair-clip-clips.zip")

    app.mount("/studio", StaticFiles(directory=UI), name="studio")
