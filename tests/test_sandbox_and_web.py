"""Media sandbox policy and dashboard security."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_publish import add_clip

from clipwarden.media import sandbox as sb
from clipwarden.web import security as sec
from clipwarden.web.app import create_app

# ------------------------------------------------------------------------------- sandbox


def runner(tmp_path: Path, docker: bool, mode="auto"):
    r = sb.MediaRunner(image="img:1", mode=mode, trust=sb.TrustManifest(tmp_path / "trust.json"))
    r._docker = docker
    return r


def test_untrusted_media_never_runs_on_host(tmp_path: Path):
    f = tmp_path / "upload.mp4"
    f.write_bytes(b"anything")
    with pytest.raises(sb.SandboxUnavailable, match="untrusted"):
        runner(tmp_path, docker=False).run("ffprobe", ["@in/upload.mp4"], in_dir=tmp_path, out_dir=tmp_path / "o", inputs=[f])


def test_docker_mode_requires_docker_even_for_trusted(tmp_path: Path):
    f = tmp_path / "t.mp4"
    f.write_bytes(b"x")
    r = runner(tmp_path, docker=False, mode="docker")
    r.trust.add([sb.sha256_file(f)])
    with pytest.raises(sb.SandboxUnavailable):
        r.backend_for([f])


def test_only_ffmpeg_tools(tmp_path: Path):
    with pytest.raises(ValueError):
        runner(tmp_path, docker=True).run("bash", ["-c", "id"], in_dir=tmp_path, out_dir=tmp_path, inputs=[])  # type: ignore[arg-type]


def test_docker_command_is_locked_down(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    f = tmp_path / "in" / "a.mp4"
    f.parent.mkdir()
    f.write_bytes(b"x")
    r = runner(tmp_path, docker=True)
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    r.run("ffmpeg", ["-i", "@in/a.mp4", "@out/b.mp4"], in_dir=f.parent, out_dir=tmp_path / "out", inputs=[f])
    cmd = " ".join(seen["cmd"])
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit", "--memory 2g",
                 f"{f.parent.resolve()}:/in:ro", "-i /in/a.mp4 /out/b.mp4", "-nostdin"):
        assert flag in cmd, flag
    assert set(seen["env"]) == {"PATH", "LANG"}  # no secrets leak into the child environment


# ------------------------------------------------------------------------------- dashboard
PW = "correct horse battery staple"


@pytest.fixture
def client(store, settings):
    app = create_app(settings, store, extra_hosts=("testserver",))
    sec.set_password(store, PW)
    return TestClient(app)


def login(c):
    r = c.post("/login", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/settings").text
    return page.split('name="csrf" value="')[1].split('"')[0]


def test_requires_login_and_sets_headers(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    h = client.get("/login").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin"


def test_dns_rebinding_host_refused(client):
    assert client.get("/login", headers={"host": "attacker.example:8765"}).status_code == 400


def test_bad_password_throttled(client):
    for _ in range(5):
        assert "Wrong password" in client.post("/login", data={"password": "nope"}).text
    assert "Too many attempts" in client.post("/login", data={"password": PW}).text


def test_csrf_and_origin_enforced(client, store):
    token = login(client)
    assert client.post("/settings/kill", data={"state": "on"}).status_code == 403
    assert client.post("/settings/kill", data={"state": "on", "csrf": "forged"}).status_code == 403
    r = client.post("/settings/kill", data={"state": "on", "csrf": token}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and not store.kill_switch
    client.post("/settings/kill", data={"state": "on", "csrf": token})
    assert store.kill_switch


def test_approve_through_dashboard(client, store, tmp_path):
    cid, sha = add_clip(store, tmp_path)
    token = login(client)
    page = client.get(f"/clips/{cid}").text
    assert "Compliance report" in page and f"/media/{sha}" in page
    r = client.post(f"/clips/{cid}/decide", data={"csrf": token, "decision": "approve", "visibility": "private", "reason": "ok"}, follow_redirects=False)
    assert r.status_code == 303 and store.row("clips", cid)["status"] == "approved"
    assert store.audit.entries(limit=1)[0].actor == "owner"


def test_reject_without_reason_shows_error(client, store, tmp_path):
    cid, _ = add_clip(store, tmp_path)
    token = login(client)
    r = client.post(f"/clips/{cid}/decide", data={"csrf": token, "decision": "reject", "visibility": "private", "reason": ""}, follow_redirects=False)
    assert "error=" in r.headers["location"] and store.row("clips", cid)["status"] == "awaiting_approval"


def test_media_route_serves_only_referenced_hashes(client, store, tmp_path):
    cid, sha = add_clip(store, tmp_path)
    login(client)
    assert client.get(f"/media/{sha}").status_code == 200
    assert client.get("/media/" + "f" * 64).status_code == 404
    assert client.get("/media/..%2f..%2fclipwarden.sqlite3").status_code == 404


def test_autoescape_blocks_xss(client, store, tmp_path):
    from clipwarden.models import Metadata

    cid, _ = add_clip(store, tmp_path)
    md = Metadata(title="Normal title here", description="safe").model_dump(mode="json")
    md["description"] = "<script>alert(1)</script>"  # pretend it got into the DB somehow
    store.upsert("clips", cid, metadata=md)
    login(client)
    page = client.get(f"/clips/{cid}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


# ------------------------------------------------------------------------------- worker policy + hostile media
def test_worker_policy(tmp_path: Path):
    from clipwarden.media import worker

    j = tmp_path / "job-1"
    ok = worker.valid({"tool": "ffmpeg", "args": ["-i", f"{j}/in/a.mp4", f"{j}/out/b.mp4"]}, j)
    assert ok and ok[0] == "/usr/bin/ffmpeg" and "-nostdin" in ok
    for bad in ({"tool": "sh", "args": []}, {"tool": "ffmpeg", "args": ["-i", "https://evil/x"]},
                {"tool": "ffmpeg", "args": ["-i", "/etc/shadow"]}, {"tool": "ffmpeg", "args": [f"{j}/in/../../x"]},
                {"tool": "ffmpeg", "args": ["concat:a|b"]}, {"tool": "ffmpeg", "args": "not-a-list"}):
        assert worker.valid(bad, j) is None, bad
    j.mkdir()
    (j / "job.json").write_text('{"tool": "sh", "args": ["-c", "id"]}')
    worker.run_job(j)
    assert '"returncode": 98' in (j / "result.json").read_text()


@pytest.mark.skipif(not __import__("os").environ.get("CLIPWARDEN_TEST_IMAGE"), reason="set CLIPWARDEN_TEST_IMAGE to a built media image")
def test_hostile_media_is_contained(tmp_path: Path):
    """A file that looks like MP4 but is garbage: decoded only inside the sandbox, rejected, audited."""
    import json as _json
    import os

    from clipwarden.config import Settings
    from clipwarden.pipeline import Pipeline, PipelineError
    from clipwarden.store import sha256_file

    evil = tmp_path / "evil.mp4"
    evil.write_bytes(b"\0\0\0\x18ftypisom" + os.urandom(200_000))
    rights = {"record_id": "own-evil", "basis": "own_content", "source_title": "x", "rights_holder": "me", "evidence": "me",
              "source_sha256": sha256_file(evil)}
    (tmp_path / "r.json").write_text(_json.dumps(rights))
    s = Settings(data_dir=str(tmp_path / "d"))
    s.media.runner, s.media.image = "docker", os.environ["CLIPWARDEN_TEST_IMAGE"]
    p = Pipeline(s)
    with pytest.raises(PipelineError, match="media rejected"):
        p.ingest(evil, tmp_path / "r.json")
    assert p.store.audit.entries(limit=1)[0].action == "media_rejected"
