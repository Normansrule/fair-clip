"""Headless-Chromium run of the Studio page, both backends (opt-in: slow, needs the vendored engines).

    python scripts/vendor_web.py && python scripts/build_studio.py
    FAIRCLIP_E2E=1 pytest tests/e2e -q

web: the GitHub Pages build (Pyodide + ffmpeg.wasm) under its real Content Security Policy.
app: the dashboard's /studio/ page against the app API.
Each run loads the example, finds moments, makes ONE clip and downloads the zip.
"""

from __future__ import annotations

import functools
import http.server
import io
import os
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
PW = "correct horse battery staple"
pytestmark = pytest.mark.skipif(os.environ.get("FAIRCLIP_E2E") != "1", reason="set FAIRCLIP_E2E=1 (slow)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Static(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript",
                      ".wasm": "application/wasm"}

    def log_message(self, *a):  # noqa: ANN002
        pass


def wait(fn, timeout: float, every: float = 0.5):  # noqa: ANN001
    t = time.time()
    while time.time() - t < timeout:
        if r := fn():
            return r
        time.sleep(every)
    raise TimeoutError


def run_studio(page, url: str) -> zipfile.ZipFile:  # noqa: ANN001
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    wait(lambda: "ready" in (page.locator("#engine").get_attribute("class") or "") or "couldn" in page.locator("#engine").text_content(), 240)
    assert "ready" in page.locator("#engine").get_attribute("class"), page.locator("#s1-error").text_content()
    page.click("#use-example")
    wait(lambda: "loaded" in page.locator("#use-example").text_content(), 120)
    page.fill("#b-clips", "1")
    page.click("#find")
    wait(lambda: page.locator("#s2").is_visible() or page.locator("#s1-error").is_visible(), 600)
    assert page.locator("#s2").is_visible(), page.locator("#s1-error").text_content()
    assert page.locator(".moment").count() == 1 and page.locator(".m-take").input_value()  # example take filled in
    page.click("#make")
    wait(lambda: page.locator("#actions").is_visible(), 1800, 2)
    assert "1 clip ready" in page.locator("#s3-title").text_content(), page.locator("#s3-status").text_content()
    with page.expect_download() as dl:
        page.click("#zip")
    z = zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes()))
    assert any(n.endswith(".mp4") for n in z.namelist()) and "ALL-TITLES-AND-DESCRIPTIONS.txt" in z.namelist()
    assert not errors, errors
    return z


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        yield b
        b.close()


def test_web_build(browser):
    if not (SITE / "studio" / "vendor" / "ffmpeg-core" / "ffmpeg-core.wasm").exists():
        pytest.skip("run scripts/vendor_web.py and scripts/build_studio.py first")
    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Static, directory=str(SITE)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        run_studio(browser.new_page(), f"http://127.0.0.1:{port}/studio/")
    finally:
        srv.shutdown()


def test_app(browser, tmp_path):
    import uvicorn

    from fairclip.config import Settings
    from fairclip.store import Store
    from fairclip.web import security as sec
    from fairclip.web.app import create_app

    port = free_port()
    s = Settings(data_dir=str(tmp_path / "d"))
    s.dashboard.port = port
    store = Store(s.data_path)
    sec.set_password(store, PW)
    server = uvicorn.Server(uvicorn.Config(create_app(s, store), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(lambda: server.started, 20, 0.1)
    try:
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{port}/login")
        page.fill('input[name="password"]', PW)
        page.click("button[type=submit], form button")
        run_studio(page, f"http://127.0.0.1:{port}/studio/")
    finally:
        server.should_exit = True
