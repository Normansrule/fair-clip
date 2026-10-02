"""Regressions for the independent review of 0.2.0 (one test per finding)."""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import base_plan
from fastapi.testclient import TestClient
from test_milestone2 import fake_api, ok_resp, published

from fairclip import cli
from fairclip.analytics_api import pull, training_rows
from fairclip.gate import numbers
from fairclip.media.templates import (
    HORIZONTAL_BOX,
    VERTICAL_BOX,
    count_up_frames,
    template_events,
)
from fairclip.models import ValueAddElement, fmt_value
from fairclip.scoring import FEATURES
from fairclip.web import security as sec
from fairclip.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
SENT = "x" * 9 + " " + ("word " * 30)[:130]  # 140 characters: the maximum with a template
ITEM = "W" * 60


# --------------------------------------------------------------- 1. empty retention can't break /analytics
def test_analytics_page_survives_clip_without_curve(store, settings):
    store.set("analytics", {"clip-new": {"tau": [], "ratio": [], "stats": {"views": 3}, "illustrative": False}})
    sec.set_password(store, "correct horse battery staple")
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",)))
    c.post("/login", data={"password": "correct horse battery staple"})
    r = c.get("/analytics")
    assert r.status_code == 200 and "No retention curve yet" in r.text


def test_empty_pull_keeps_the_curve_we_had(store):
    published(store, "clip-a-0001", "vidA1234567")
    store.set("analytics", {"clip-a-0001": {"tau": [0, 0.5, 1], "ratio": [1, 0.8, 0.5], "illustrative": False}})

    def no_curve(params):
        if params.get("dimensions"):
            return httpx.Response(200, json={"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}], "rows": []})
        return ok_resp(params)
    http, _ = fake_api(no_curve)
    pull(store, http, "tok")
    assert store.get("analytics")["clip-a-0001"]["ratio"] == [1, 0.8, 0.5]


# --------------------------------------------------------------- 2. app refuses a port it doesn't own
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # uvicorn's failed bind, expected
def test_app_fails_loudly_when_port_is_taken(tmp_path, monkeypatch):
    squat = socket.socket()
    squat.bind(("127.0.0.1", 0))
    squat.listen(1)
    port = squat.getsockname()[1]
    cfg = tmp_path / "fairclip.toml"
    cfg.write_text(f'data_dir = "."\n[dashboard]\nhost = "127.0.0.1"\nport = {port}\n')
    opened = []
    monkeypatch.setattr(cli, "_open_browser", opened.append)
    try:
        assert cli.main(["--config", str(cfg), "app", "--no-browser"]) == 1
    finally:
        squat.close()
    assert opened == []


# --------------------------------------------------------------- 3. every command finds the app's data
def test_config_resolution_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAIRCLIP_CONFIG", raising=False)
    monkeypatch.setattr(cli, "APP_HOME", tmp_path / "apphome")
    assert cli.config_path(None) is None
    (tmp_path / "apphome").mkdir()
    (tmp_path / "apphome" / "fairclip.toml").write_text('data_dir = "."\n')
    assert cli.config_path(None) == tmp_path / "apphome" / "fairclip.toml"  # e.g. `fair-clip kill on` from ~
    (tmp_path / "fairclip.toml").write_text('data_dir = "."\n')
    assert cli.config_path(None) == Path("fairclip.toml")
    monkeypatch.setenv("FAIRCLIP_CONFIG", "/x/env.toml")
    assert cli.config_path(None) == Path("/x/env.toml")
    assert cli.config_path("explicit.toml") == Path("explicit.toml")


def test_kill_switch_from_home_reaches_the_app(tmp_path, monkeypatch):
    home = tmp_path / "apphome"
    home.mkdir()
    (home / "fairclip.toml").write_text('data_dir = "."\n')
    monkeypatch.setattr(cli, "APP_HOME", home)
    monkeypatch.delenv("FAIRCLIP_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["kill", "on"]) == 0
    from fairclip.store import Store
    assert Store(home).kill_switch and not (tmp_path / "data").exists()


# --------------------------------------------------------------- 4. curl | bash never trusts the cwd
def test_piped_installer_does_not_install_from_cwd(tmp_path):
    """Piped from inside a checkout, it must take the download path (which fails offline here)."""
    script = (ROOT / "scripts" / "install.sh").read_text()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "FAIRCLIP_VERSION": "v0.0.0",
           "FAIRCLIP_REPO": "invalid.invalid/none"}
    r = subprocess.run(["bash"], input=script, text=True, capture_output=True, cwd=ROOT / "docs", env=env, timeout=120)
    assert "installing from this checkout" not in r.stdout + r.stderr
    assert r.returncode != 0 and not (tmp_path / ".local" / "share" / "fair-clip" / "app").exists()


def test_installer_body_runs_only_when_fully_downloaded():
    lines = [ln for ln in (ROOT / "scripts" / "install.sh").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[-1].strip() == 'main "$@"'


# --------------------------------------------------------------- 5. malformed responses are per-clip errors
def test_malformed_report_is_one_clips_error(store):
    published(store, "clip-a-0001", "vidA1234567")
    published(store, "clip-b-0002", "vidB1234567")
    seen = {"n": 0}

    def flaky(params):
        seen["n"] += 1
        if params["filters"] == "video==vidA1234567":
            return httpx.Response(200, json={"columnHeaders": [{"name": "views"}], "rows": [["n/a"]]})
        return ok_resp(params)
    http, _ = fake_api(flaky)
    res = pull(store, http, "tok")
    assert res["clips"] == 1 and "unexpected report format" in res["errors"][0]
    assert "clip-b-0002" in store.get("analytics")


# --------------------------------------------------------------- 6. join uses the clip's own candidate
def test_join_ignores_nested_candidates(store):
    z_wrong = {k: 9.0 for k in FEATURES}
    z_right = {k: 1.0 for k in FEATURES}
    store.upsert("candidates", "src-test-c1", source_id="src-test", rank=1, data={"start": 12, "end": 20, "z": z_wrong})
    store.upsert("candidates", "src-test-c2", source_id="src-test", rank=2, data={"start": 10, "end": 40, "z": z_right})
    published(store, "clip-a-0001", "vidA1234567", start=10, end=40)
    store.set("analytics", {"clip-a-0001": {"illustrative": False, "stats": {"views": 900, "averageViewPercentage": 50}}})
    assert training_rows(store)[0]["z"] == z_right


# --------------------------------------------------------------- 7. count-up never ends on an unchecked figure
def _key(headline, start, end):
    return ValueAddElement(kind="context_card", template="key_number", start=start, end=end,
                           text="An owner sentence that explains the figure shown.", data={"headline": headline})


def _figure_events(ev):
    return [e for e in ev if "\\b1" in e and "BY THE NUMBERS" not in e and "\\p1" not in e]


def _t(s):
    h, m, x = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(x)


@pytest.mark.parametrize("start,end,dur", [(4.9, 6.0, 5.1), (0, 0.5, 30), (0, 10, 30)])
def test_exact_figure_holds_the_screen(start, end, dur):
    ev = _figure_events(template_events(_key("853 m", start, end), "9:16", dur, "X"))
    last = ev[-1].split(",")
    a, b = _t(last[1]), _t(last[2])
    assert last[-1].endswith("853 m") and b > a
    assert b - a >= min(0.8, min(end, dur) - start) - 0.011


def test_markets_and_ambiguous_figures_dont_count_up():
    assert len(_figure_events(template_events(_key("853 m", 0, 10), "9:16", 30, "X", count_up=False))) == 1
    for h in ("COVID-19 cases", "10/7", "2 of 3"):
        assert count_up_frames(h) == [h]
    plan = base_plan(tags=["markets"], value_add=[_key("$1.9B", 0, 10).model_dump()],
                     markets={"disclaimer_on_screen": True, "disclaimer_in_description": True})
    from fairclip.media.overlay import build_ass

    class M:
        duration = 30.0

        def to_clip(self, t):
            return t
    figs = [ln for ln in build_ass(plan, [], M(), hook_text="", channel="", attribution="").splitlines() if "$" in ln and "Tpl" in ln]
    assert len(figs) == 1 and figs[0].endswith("$1.9B")


# --------------------------------------------------------------- 8. worst-case text stays inside the panel
WORST = {
    "key_number": {"headline": "$1,234,567.89B"},
    "definition": {"headline": "D" * 24},
    "steps": {"items": [ITEM] * 5},
    "compare": {"left_title": "L" * 20, "right_title": "R" * 20, "left": ["W" * 28] * 4, "right": ["W" * 28] * 4},
    "bar": {"bars": [{"label": "B" * 40, "value": 1234567.5}] * 4 + [{"label": "B" * 40, "value": 0.000123}], "unit": "U" * 12},
}


@pytest.mark.parametrize("aspect,box", [("9:16", VERTICAL_BOX), ("16:9", HORIZONTAL_BOX)])
@pytest.mark.parametrize("tpl", sorted(WORST))
def test_worst_case_layout_fits(tpl, aspect, box):
    va = ValueAddElement(kind="graphic", template=tpl, data=WORST[tpl], text=SENT, start=0, end=30)
    for e in template_events(va, aspect, 30, "X"):
        pos = re.search(r"\\pos\((-?\d+),(-?\d+)\)", e)
        fs = re.search(r"\\fs(-?\d+)", e)
        x, y = int(pos.group(1)), int(pos.group(2))
        assert box.x <= x <= box.x + box.w and box.y <= y <= box.y + box.h, (tpl, e[:120])
        if fs:
            size = int(fs.group(1))
            assert size >= 12
            text = e.split("}", 1)[1] if "\\p1" not in e else ""
            for line in text.split("\\N"):
                if "\\an5" not in e:  # centred step numbers are tiny
                    assert x + 0.55 * size * len(line) <= box.x + box.w + 4, (tpl, aspect, size, line[:30])


def test_long_sentence_with_template_rejected():
    with pytest.raises(ValueError, match="140 characters"):
        ValueAddElement(kind="graphic", template="definition", data={"headline": "Flutter"}, text="y" * 141, start=0, end=9)


# --------------------------------------------------------------- plausible: bar values the gate can read
@pytest.mark.parametrize("v,shown", [(1e-05, "0.00001"), (1234.5, "1,234.5"), (12, "12"), (3.5, "3.5"), (0, "0")])
def test_bar_values_never_use_exponents(v, shown):
    assert fmt_value(v) == shown
    assert numbers(shown) <= {shown.replace(",", "")} | {"0"}


def test_failed_setup_attempts_dont_lock_login(store, settings):
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",), setup_token="tok-123"))
    for _ in range(6):
        c.post("/setup", data={"t": "wrong", "password": "x" * 12, "password2": "x" * 12})
    r = c.post("/setup", data={"t": "tok-123", "password": "long enough pw", "password2": "long enough pw"}, follow_redirects=False)
    assert r.headers["location"] == "/login"  # setup is throttled after repeated bad tokens...
    sec.set_password(store, "correct horse battery staple")
    c2 = TestClient(create_app(settings, store, extra_hosts=("testserver",)))
    assert c2.post("/login", data={"password": "correct horse battery staple"}, follow_redirects=False).status_code == 303


# --------------------------------------------------------------- re-review follow-ups
def test_bar_values_that_cant_be_read_are_rejected():
    with pytest.raises(ValueError, match="0.000001"):
        ValueAddElement(kind="graphic", template="bar", text="Owner sentence about these two bars here.", start=0, end=9,
                        data={"bars": [{"label": "a", "value": 1e-7}, {"label": "b", "value": 1}]})
    assert fmt_value(999999999.5) == "999,999,999.5"  # what the owner typed, no float noise


@pytest.mark.parametrize("h", ["1,2,3", "$ 1", "3 m"])
def test_odd_or_tiny_figures_dont_count_up(h):
    assert count_up_frames(h) == [h]


def test_store_update_is_atomic_across_connections(tmp_path):
    """Two Store objects = two connections, like the CLI and the dashboard in separate processes."""
    import threading

    from fairclip.store import Store
    a, b = Store(tmp_path / "d"), Store(tmp_path / "d")

    def bump(s, tag):
        for i in range(40):
            s.update("analytics", lambda cur, k=f"{tag}{i}": dict(cur or {}) | {k: 1})
    ts = [threading.Thread(target=bump, args=(s, t)) for s, t in ((a, "a"), (b, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(a.get("analytics")) == 80


def test_installer_has_a_lock_and_keeps_the_previous_version():
    s = (ROOT / "scripts" / "install.sh").read_text()
    assert "flock -n 9" in s and '"$old" != "$prev"' in s and "legacy-" in s
