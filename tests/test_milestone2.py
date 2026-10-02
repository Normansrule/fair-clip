"""Milestone 2: value-add templates, read-only YouTube Analytics pull, live weight proposals, app mode."""

from __future__ import annotations

import json
import tomllib
from datetime import date
from pathlib import Path

import httpx
import pytest
from conftest import SHA, base_plan, make_ctx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clipwarden.analytics_api import (
    REPORTS_URL,
    AnalyticsError,
    fetch_clip,
    propose_from_analytics,
    pull,
    training_rows,
)
from clipwarden.gate import load_screen_terms, run_gate
from clipwarden.media.overlay import build_ass
from clipwarden.media.templates import EXAMPLES, count_up_frames, template_events
from clipwarden.models import TemplateKind, ValueAddElement
from clipwarden.scoring import FEATURES, MAX_STEP, MIN_CLIPS_FOR_UPDATE
from clipwarden.web import security as sec
from clipwarden.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


# ============================================================================== templates
@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_example_validates_and_draws(name):
    va = ValueAddElement.model_validate(EXAMPLES[name])
    ev = template_events(va, "9:16", 30.0, "X")
    assert ev and all(e.startswith("Dialogue:") for e in ev)
    assert any("\\p1" in e for e in ev)  # a drawn panel, not just text
    assert va.text in " ".join(ev).replace("\\N", " ")  # the owner's sentence is always shown


@pytest.mark.parametrize("tpl,data", [("key_number", {}), ("key_number", {"headline": "lots"}), ("definition", {}),
                                      ("steps", {"items": ["one"]}), ("compare", {"left_title": "A", "left": ["x"]}),
                                      ("bar", {"bars": [{"label": "a", "value": 1}]}), ("bar", {"bars": [{"label": "a", "value": 0}, {"label": "b", "value": 0}]})])
def test_template_needs_its_data(tpl, data):
    with pytest.raises(ValidationError):
        ValueAddElement(kind="graphic", template=tpl, data=data, text="An owner sentence long enough to count here.", start=0, end=9)


def test_steps_need_time_on_screen():
    with pytest.raises(ValidationError, match="1.2 s"):
        ValueAddElement(kind="analysis", template="steps", data={"items": ["a", "b", "c", "d"]},
                        text="Four steps in two seconds is too fast to read.", start=0, end=2)


def test_template_strings_cannot_inject_ass_or_hide_text():
    va = ValueAddElement(kind="analysis", template="steps", start=0, end=9, text="A normal owner sentence about these steps.",
                         data={"items": ["{\\alpha&HFF&}hidden", "two"]})
    joined = "\n".join(template_events(va, "9:16", 30.0, "X"))
    assert "{\\alpha&HFF&}" not in joined and "｛∖alpha&HFF&｝hidden" in joined
    with pytest.raises(ValidationError):
        ValueAddElement(kind="analysis", template="definition", start=0, end=9, text="A normal owner sentence here.",
                        data={"headline": "evil‮moc"})


def test_count_up_ends_on_exact_owner_text():
    f = count_up_frames("$1,250.5B")
    assert f[-1] == "$1,250.5B" and f[0] == "$0.0B" and len(f) > 3
    assert count_up_frames("about") == ["about"]


def test_horizontal_layout_stays_in_frame():
    va = ValueAddElement.model_validate(EXAMPLES["bar"])
    for e in template_events(va, "16:9", 30.0, "X"):
        if "\\pos(" in e:
            x, y = (int(v) for v in e.split("\\pos(")[1].split(")")[0].split(","))
            assert 0 <= x <= 1920 and 0 <= y <= 1080


def test_card_template_unchanged_and_template_in_full_overlay():
    plan = base_plan(value_add=[EXAMPLES["compare"] | {"start": 1.0, "end": 12.0}])

    class M:
        duration = 30.0

        def to_clip(self, t):
            return t

    ass = build_ass(plan, [], M(), hook_text="", channel="", attribution="")
    assert "Style: Tpl," in ass and "SIDE BY SIDE" in ass and "1940 deck" in ass


def test_gate_sees_template_text_links_and_numbers():
    link = EXAMPLES["steps"] | {"start": 1.0, "end": 12.0, "data": {"items": ["Buy the kit", "at evil dot com slash kit"]}}
    assert "LINK_NOT_ALLOWLISTED" in {r.code for r in run_gate(make_ctx(plan=base_plan(value_add=[link])), SHA).blocks}
    from test_gate import mk_ctx
    num = EXAMPLES["key_number"] | {"start": 1.0, "end": 12.0, "data": {"headline": "37% growth"}}
    rep = run_gate(mk_ctx(va=[num]), SHA)
    assert any(r.code == "FINANCE_UNSOURCED_NUMBER" and "37%" in r.message for r in rep.blocks)


def test_template_words_ground_the_title():
    va = EXAMPLES["definition"] | {"start": 1.0, "end": 12.0}
    ctx = make_ctx(plan=base_plan(value_add=[va]), title="Flutter: why the wind twists a bridge")
    assert "METADATA_UNGROUNDED" not in {r.code for r in run_gate(ctx, SHA).results}


def test_visible_text_lists_bar_values():
    va = ValueAddElement.model_validate(EXAMPLES["bar"])
    assert "Twice as deep 8 × stiffness" in va.visible_text()
    assert va.template is TemplateKind.BAR


# ============================================================================== analytics API
def fake_api(resp_for):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert str(req.url).startswith(REPORTS_URL)
        assert req.headers["authorization"] == "Bearer tok"
        return resp_for(dict(req.url.params))
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def ok_resp(params):
    if params.get("dimensions") == "elapsedVideoTimeRatio":
        rows = [[i / 10, 1.0 - i / 20, 0.5] for i in range(11)] + [[0.5, float("nan"), 0], [7, 0.3, 0]]  # junk dropped
        body = {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"},
                                  {"name": "relativeRetentionPerformance"}], "rows": rows}
        return httpx.Response(200, content=json.dumps(body, allow_nan=True), headers={"content-type": "application/json"})
    return httpx.Response(200, json={"columnHeaders": [{"name": n} for n in params["metrics"].split(",")],
                                     "rows": [[120, 30.5, 22.0, 61.0]]})


def test_fetch_clip_parses_and_validates():
    http, seen = fake_api(ok_resp)
    got = fetch_clip(http, "tok", "abcDEF12345", "2026-09-01", "2026-09-30")
    assert got["stats"] == {"views": 120, "estimatedMinutesWatched": 30.5, "averageViewDuration": 22.0, "averageViewPercentage": 61.0}
    assert len(got["tau"]) == 11 and got["tau"] == sorted(got["tau"])
    assert all(p["ids"] == "channel==MINE" and p["filters"] == "video==abcDEF12345" for p in (dict(r.url.params) for r in seen))


@pytest.mark.parametrize("status,body,match", [(401, "", "reconnect"), (429, "", "quota"), (500, "ya29.SECRETTOKENVALUE123", "error 500")])
def test_fetch_errors_are_clear_and_redacted(status, body, match):
    http, _ = fake_api(lambda p: httpx.Response(status, text=body))
    with pytest.raises(AnalyticsError, match=match) as ei:
        fetch_clip(http, "tok", "abc", "2026-09-01", "2026-09-30")
    assert "SECRETTOKENVALUE" not in str(ei.value)


def published(store, cid, vid, start=10.0, end=40.0, source="src-test"):
    store.upsert("clips", cid, source_id=source, status="published", plan=base_plan(clip_id=cid, source_id=source, start=start, end=end).model_dump(mode="json"),
                 upload={"at": "2026-09-01T10:00:00+00:00", "video_id": vid})


def test_pull_only_published_with_ids_and_audits(store):
    published(store, "clip-a-0001", "vidA1234567")
    published(store, "clip-b-0002", "bad id/../x")  # malformed id is skipped
    store.upsert("clips", "clip-c-0003", status="awaiting_approval")
    store.set("analytics", {"clip-old": {"tau": [0, 1], "ratio": [1, 0.5], "illustrative": True}})
    http, seen = fake_api(ok_resp)
    res = pull(store, http, "tok", today=date(2026, 9, 30))
    assert res == {"clips": 1, "no_retention_yet": 0, "errors": []} and len(seen) == 2
    data = store.get("analytics")
    assert data["clip-a-0001"]["illustrative"] is False and "clip-old" in data  # existing data kept
    assert store.audit.entries(limit=1)[0].action == "analytics_pull"


def test_pull_stops_on_quota(store):
    published(store, "clip-a-0001", "vidA1234567")
    published(store, "clip-b-0002", "vidB1234567")
    http, seen = fake_api(lambda p: httpx.Response(429))
    res = pull(store, http, "tok")
    assert len(seen) == 1 and "quota" in res["errors"][0]


# ============================================================================== live feedback loop
def seed_training(store, n, views=500):
    for i in range(n):
        start = 10.0 * i
        z = {k: ((i * 7 + j * 3) % 5 - 2) / 2 for j, k in enumerate(FEATURES)}
        store.upsert("candidates", f"src-test-c{i}", source_id="src-test", rank=i, data={"start": start, "end": start + 25, "z": z})
        published(store, f"clip-{i:02d}-x", f"vid{i:08d}", start=start, end=start + 25)
    store.set("analytics", {f"clip-{i:02d}-x": {"tau": [0, 1], "ratio": [1, 0.5], "illustrative": False,
                                               "stats": {"views": views, "averageViewPercentage": 40 + 3 * i}} for i in range(n)})


def test_training_rows_join_and_filter(store):
    seed_training(store, 4)
    data = store.get("analytics")
    data["clip-00-x"]["illustrative"] = True
    data["clip-01-x"]["stats"]["views"] = 10  # too few views
    store.set("analytics", data)
    rows = training_rows(store)
    assert [r["clip"] for r in rows] == ["clip-02-x", "clip-03-x"]
    assert rows[0]["outcome"] == pytest.approx(0.46)


def test_live_proposal_is_bounded_and_pending(store):
    seed_training(store, MIN_CLIPS_FOR_UPDATE)
    cur = {k: 0.2 for k in FEATURES}
    pid, prop = propose_from_analytics(store, cur, "owner")
    assert max(abs(prop[k] - cur[k]) for k in FEATURES) <= MAX_STEP + 1e-9
    row = store.rows("weight_proposals", "id=?", (pid,))[0]
    assert row["status"] == "pending" and row["evidence"]["from"] == "youtube-analytics"
    assert store.get("weights") is None  # nothing applied without a person


def test_live_proposal_refuses_thin_data(store):
    seed_training(store, MIN_CLIPS_FOR_UPDATE - 1)
    with pytest.raises(ValueError, match="at least"):
        propose_from_analytics(store, {k: 0.2 for k in FEATURES}, "owner")


# ============================================================================== app mode + packaging
def test_setup_link_is_single_use_and_only_on_first_run(store, settings):
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",), setup_token="tok-123"))
    assert store.get("dashboard_pw") is None  # no generated password printed when a setup link exists
    assert c.get("/setup?t=wrong", follow_redirects=False).headers["location"] == "/login"
    assert "differ" in c.post("/setup", data={"t": "tok-123", "password": "a" * 12, "password2": "b" * 12}).text
    r = c.post("/setup", data={"t": "tok-123", "password": "long enough pw", "password2": "long enough pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200  # logged in
    assert sec.check_password(store, "long enough pw")
    r = c.post("/setup", data={"t": "tok-123", "password": "x" * 14, "password2": "x" * 14}, follow_redirects=False)
    assert r.headers["location"] == "/login" and sec.check_password(store, "long enough pw")


def test_setup_link_ignored_once_a_password_exists(store, settings):
    sec.set_password(store, "existing password")
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",), setup_token="tok-123"))
    assert c.get("/setup?t=tok-123", follow_redirects=False).status_code == 303


def test_manifest_served_and_linked(store, settings):
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",)))
    m = c.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and json.loads(m.text)["display"] == "standalone"
    assert 'rel="manifest"' in c.get("/login").text


def test_package_data_ships_fonts_and_screen_terms():
    pd = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["clipwarden"]
    assert "data/fonts/*" in pd and "web/static/*" in pd
    terms = load_screen_terms(ROOT / "src" / "clipwarden" / "data" / "screen_terms.txt")
    assert terms["hard"] and terms["soft"]


def test_missing_screen_terms_fail_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_screen_terms(tmp_path / "nope.txt")


def test_runtime_lock_has_every_runtime_import():
    """Regression: httpx was imported at runtime but only locked for dev, so clean installs crashed."""
    lock = (ROOT / "requirements.lock").read_text()
    for pkg in tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]:
        assert f"\n{pkg}" in "\n" + lock, pkg


def test_settings_shows_tuning_progress(store, settings):
    seed_training(store, 3)
    sec.set_password(store, "correct horse battery staple")
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",)))
    c.post("/login", data={"password": "correct horse battery staple"})
    page = c.get("/settings").text
    assert "3 of the 10 needed" in page and "disabled" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = c.post("/settings/weights-propose", data={"csrf": csrf}, follow_redirects=False)
    assert "error=" in r.headers["location"]  # too few clips: refused with a message, nothing stored
    assert store.rows("weight_proposals") == []


def test_published_gate_counts_match_code():
    """README and the Pages site quote the gate size; they must match the registry."""
    from clipwarden.gate.reasons import REASONS
    n = len(REASONS)
    b = sum(1 for v in REASONS.values() if v[1] == "block")
    readme = (ROOT / "README.md").read_text()
    site = (ROOT / "site" / "index.html").read_text()
    assert f"{n} checks in 11 groups" in readme and f"**{b} block**" in readme and f"**{n - b} warn**" in readme
    assert f"{n} checks in 11 groups" in site and f"{b} block the clip, {n - b} are warnings" in site
    assert f"The full gate has {n}" in (ROOT / "site" / "demo.html").read_text()
