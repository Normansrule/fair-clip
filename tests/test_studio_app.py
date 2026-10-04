"""The Studio in the app (/studio/ + its JSON API) and the browser bridge (studio_web), end to end."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fairclip.config import Settings
from fairclip.store import Store
from fairclip.web import security as sec
from fairclip.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples" / "studio"
FONTS = ROOT / "src" / "fairclip" / "data" / "fonts"
PW = "correct horse battery staple"
CAPS = (EX / "captions.srt").read_bytes()
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.fixture
def app(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"))
    store = Store(s.data_path)
    sec.set_password(store, PW)
    c = TestClient(create_app(s, store, extra_hosts=("testserver",)))
    return c, store


def login(c):
    c.post("/login", data={"password": PW})
    page = c.get("/studio/").text
    return page.split('name="fairclip-csrf" content="')[1].split('"')[0]


def meta():
    p = json.loads((EX / "project.json").read_text())
    return json.dumps({"brand": p["brand"], "rights": p["rights"], "duration": 120})


def test_studio_needs_login_and_csrf(app):
    c, _ = app
    assert c.get("/studio/", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/api/studio/make", json={}).status_code == 401
    csrf = login(c)
    page = c.get("/studio/")
    assert '<meta name="fairclip-backend" content="app">' in page.text and csrf
    assert "media-src 'self' blob:" in page.headers["content-security-policy"]
    assert c.get("/studio/studio.js").status_code == 200
    r = c.post("/api/studio/analyze", files={"video": ("v.mp4", b"x")}, data={"meta": meta()})
    assert r.status_code == 403  # no CSRF header
    r = c.post("/api/studio/make", json={"session": "nope"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404


def test_bad_requests_are_400s_not_500s(app):
    c, _ = app
    h = {"X-CSRF-Token": login(c)}
    assert c.post("/api/studio/title-check", content=b"[1]", headers=h | {"content-type": "application/json"}).status_code == 400
    r = c.post("/api/studio/analyze", files={"video": ("..", b"x")}, data={"meta": "[]"}, headers=h)
    assert r.status_code == 400 and "form" in r.json()["error"]
    r = c.post("/api/studio/analyze", files={"video": ("v.mp4", b"x")}, data={"meta": meta()},
               headers=h | {"content-length": str(10**13)})
    assert r.status_code in (400, 413)
    big = b"1\n00:00:00,000 --> 00:00:01,000\nx\n\n" * 400_000  # ~14 MB of captions
    r = c.post("/api/studio/analyze", files={"video": ("v.mp4", b"x"), "captions": ("c.srt", big)}, data={"meta": meta()}, headers=h)
    assert r.status_code == 400 and "10 MB" in r.json()["error"]


def test_safe_upload_names():
    from fairclip.web.studio_api import safe_name

    assert safe_name("..") == "video.mp4" and safe_name("../../etc/passwd") == "passwd" and safe_name(".bashrc") == "bashrc"
    assert safe_name("my clip (1).mp4") == "my_clip_1_.mp4"


def test_untrusted_upload_needs_the_sandbox(app):
    from fairclip.media.sandbox import docker_available

    if docker_available(Settings().media.image):
        pytest.skip("Docker sandbox present")
    c, _ = app
    csrf = login(c)
    data = (EX / "source.mp4").read_bytes() + b"\0"  # different bytes: not one of our trusted examples
    r = c.post("/api/studio/analyze", files={"video": ("mine.mp4", data, "video/mp4"), "captions": ("c.srt", CAPS)},
               data={"meta": meta()}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 503 and "sandbox" in r.json()["error"]


def test_app_studio_end_to_end(app):
    c, store = app
    csrf = login(c)
    h = {"X-CSRF-Token": csrf}
    r = c.post("/api/studio/analyze", files={"video": ("resonance.mp4", (EX / "source.mp4").read_bytes(), "video/mp4"),
                                             "captions": ("captions.srt", CAPS)}, data={"meta": meta()}, headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    project = json.loads((EX / "project.json").read_text())
    assert [(m["start"], m["end"]) for m in res["candidates"]] == [(p["moment"]["start"], p["moment"]["end"]) for p in project["clips"]]
    sid = res["session"]
    ex = project["clips"][0]
    take = {"text": ex["take"], "template": ex["template"], "data": ex["data"]}
    bad = c.post("/api/studio/title-check", json={"session": sid, "i": 0, "take": take, "title": "Aliens built 9 bridges"}, headers=h).json()
    assert bad["problems"] and c.post("/api/studio/title-check", json={"session": sid, "i": 0, "take": take, "title": ex["title"]},
                                      headers=h).json()["problems"] == []
    job = c.post("/api/studio/make", json={"session": sid, "quality": "fast",
                                           "clips": [{"i": 0, "take": take, "title": ex["title"]}, {"i": 1, "take": {"text": ""}}]},
                 headers=h).json()["job"]
    for _ in range(600):
        st = c.get(f"/api/studio/job/{job}").json()
        if st["state"] != "running":
            break
        time.sleep(0.5)
    assert st["state"] == "done", st
    ok, blocked = st["done"]
    assert ok["passed"] and ok["title"] == ex["title"] and not blocked["passed"]
    assert "VALUE_ADD_MISSING" in {b["code"] for b in blocked["blocks"]}
    clip = c.get(f"/api/studio/job/{job}/clip/0")
    assert clip.status_code == 200 and clip.content[4:8] == b"ftyp"
    assert c.get(f"/api/studio/job/{job}/clip/1").status_code == 404  # blocked: nothing to download
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/studio/job/{job}/zip").content))
    names = set(z.namelist())
    assert ok["filename"] in names and "ALL-TITLES-AND-DESCRIPTIONS.txt" in names and "upload-sheet.csv" in names
    assert ex["title"] in z.read("ALL-TITLES-AND-DESCRIPTIONS.txt").decode()
    actions = [e.action for e in store.audit.entries(20)]
    assert "studio_analyze" in actions and "studio_make" in actions


def test_browser_bridge_makes_the_same_clip(tmp_path):
    """studio_web is what Pyodide runs in the page; here CPython + local FFmpeg stand in for it."""
    from fairclip import studio_web as sw

    sw.hash_start()
    data = (EX / "source.mp4").read_bytes()
    for o in range(0, len(data), 1 << 20):
        sw.hash_update(data[o : o + (1 << 20)])
    assert sw.hash_done() == json.loads((EX / "rights.json").read_text())["source_sha256"]
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(EX / "source.mp4"), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    res = json.loads(sw.analyze(meta(), (EX / "captions.srt").read_text(), pcm))
    assert res["ok"] and len(res["candidates"]) == 3 and res["synthetic"]
    assert not json.loads(sw.analyze(meta(), "", pcm))["ok"]  # captions are required in the browser
    res = json.loads(sw.analyze(meta(), (EX / "captions.srt").read_text(), pcm))
    ex = json.loads((EX / "project.json").read_text())["clips"][2]
    take = json.dumps({"text": ex["take"], "template": ex["template"], "data": ex["data"]})
    p = json.loads(sw.plan(2, take, "source.mp4", "fast"))
    assert p["ok"] and p["args"][-1] == "@out/clip.mp4"
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "overlay.ass").write_text(p["ass"])
    args = [a.replace("@in/overlay.ass", f"{tmp_path}/work/overlay.ass").replace("@in/", f"{EX}/").replace("@out/", f"{tmp_path}/work/")
            .replace("@fonts/", f"{FONTS}/") for a in p["args"]]
    subprocess.run(["ffmpeg", "-loglevel", "error", *args], check=True, timeout=600)
    clip = (tmp_path / "work" / "clip.mp4").read_bytes()
    import hashlib

    done = json.loads(sw.finish(2, ex["title"], hashlib.sha256(clip).hexdigest()))
    assert done["ok"] and done["clip"]["passed"], done
    assert done["clip"]["filename"].startswith("03-37-dampers") and "660" in done["clip"]["title"]
    files = json.loads(sw.package("[2]"))["files"]
    assert set(files) >= {"README.txt", "ALL-TITLES-AND-DESCRIPTIONS.txt", "upload-sheet.csv"}
    bad = json.loads(sw.plan(0, json.dumps({"text": "x", "template": "key_number", "data": {"headline": "no digits"}}), "s.mp4"))
    assert not bad["ok"] and "missing its data" in bad["error"]
