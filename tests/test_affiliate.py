"""Milestone 3: the affiliate registry, spoken-disclosure detection, CLI and dashboard flows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_gate import reg, said

from fairclip import cli
from fairclip.affiliate import (
    AffiliateRegistry,
    detect_spoken,
    import_registry,
    load_registry,
    update_registry,
)
from fairclip.web import security as sec
from fairclip.web.app import create_app, parse_span, parse_times

ROOT = Path(__file__).resolve().parents[1]
PW = "correct horse battery staple"
TODAY = datetime.now(UTC).date().isoformat()


def base(**over):
    d = json.loads(reg().model_dump_json())
    return d | over


# ------------------------------------------------------------------------------------ registry
@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["programs"][0].update(link_prefixes=["https://example.com"]), "must end the host"),
    (lambda d: d["programs"][0].update(terms_url="http://example.com/terms"), "https"),
    (lambda d: d["links"][0].update(url="https://bit.ly/abc"), "shortener"),
    (lambda d: d["links"][0].update(url="https://example.com/other/kit"), "doesn't start with"),
    (lambda d: d["links"][0].update(url="https://example.com/kits/go?url=https://evil.example"), "redirect"),
    (lambda d: d["links"][0].update(program_id="nope"), "unknown program"),
    (lambda d: d["links"][0].update(product="Something else"), "evaluated products"),
    (lambda d: d["products"][0].update(how_evaluated="great!"), "at least 30"),
    (lambda d: d["products"][0].update(evaluated_on="2999-01-01"), "future"),
    (lambda d: d["products"].append(dict(d["products"][0], name="KIT")), "unique"),
    (lambda d: d["programs"][0].update(name="Kits‮"), "hidden"),
])
def test_registry_rejects_bad_entries(mutate, match):
    d = base()
    mutate(d)
    with pytest.raises(ValidationError, match=match):
        AffiliateRegistry.model_validate(d)


def test_prefix_needs_host_slash_so_lookalike_hosts_fail():
    d = base()
    d["links"][0]["url"] = "https://example.com.evil.example/kits/bridge-kit"
    with pytest.raises(ValidationError):
        AffiliateRegistry.model_validate(d)


def test_usable_links_follow_program_and_product_state():
    r = reg()
    assert r.usable_links() == ["https://example.com/kits/bridge-kit"]
    r.programs[0].active = False
    assert r.usable_links() == []
    r.programs[0].active, r.products[0].retired = True, True
    assert r.usable_links() == []


def test_update_is_validated_as_a_whole_and_audited(store):
    import_registry(store, base(), "owner")

    def bad(r):
        r.links[0].program_id = "ghost"
        return r
    with pytest.raises(ValidationError):
        update_registry(store, bad, "owner", "link", {})
    assert load_registry(store).links[0].program_id == "example-kits"  # unchanged
    assert [e.action for e in store.audit.entries(limit=5)][0] == "affiliate_import"


# ------------------------------------------------------------------------------------ spoken detection
def test_detect_spoken_times_come_from_the_transcript():
    words = said("welcome back quick note I earn a commission if you buy the kit")
    hits = detect_spoken(words)
    assert hits == [(round(words[4][0], 2), "I earn a commission")]


@pytest.mark.parametrize("text", ["I paid nothing for it", "their earnings grew", "a sponsor of the arts", "I earn a living teaching",
                                  "this video is not sponsored by anyone", "I do not earn a commission", "we don't earn a commission",
                                  "my drone was out of commission", "time to decommission the bridge", "no commissions here"])
def test_detect_spoken_ignores_lookalikes(text):
    assert detect_spoken(said(text)) == []


# ------------------------------------------------------------------------------------ CLI
def test_cli_import_show_review(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "fairclip.toml"
    cfg.write_text('data_dir = "."\n')
    old = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    f = tmp_path / "reg.json"
    f.write_text(json.dumps(base(programs=[dict(base()["programs"][0], terms_reviewed_on=old)])))
    assert cli.main(["--config", str(cfg), "affiliate", "import", str(f)]) == 0
    assert cli.main(["--config", str(cfg), "affiliate", "show"]) == 0
    assert "re-read terms" in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "affiliate", "review-terms", "example-kits"]) == 0
    capsys.readouterr()
    cli.main(["--config", str(cfg), "affiliate", "show"])
    assert "re-read terms" not in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "affiliate", "review-terms", "nope"]) == 1


# ------------------------------------------------------------------------------------ dashboard
@pytest.fixture
def dash(store, settings):
    sec.set_password(store, PW)
    c = TestClient(create_app(settings, store, extra_hosts=("testserver",)))
    c.post("/login", data={"password": PW})
    csrf = c.get("/affiliate").text.split('name="csrf" value="')[1].split('"')[0]
    return c, csrf


def test_dashboard_builds_a_registry_end_to_end(dash, store):
    c, csrf = dash
    r = c.post("/affiliate/program", data={"csrf": csrf, "id": "kits", "name": "Kits Co", "terms_url": "https://kits.example/terms",
                                           "link_prefixes": "https://kits.example/p/", "required_wording": "I earn from qualifying purchases"},
               follow_redirects=False)
    assert r.headers["location"] == "/affiliate"
    c.post("/affiliate/product", data={"csrf": csrf, "name": "Truss kit", "how_evaluated": "Built and load-tested it with my class twice."})
    c.post("/affiliate/link", data={"csrf": csrf, "url": "https://kits.example/p/truss", "program_id": "kits", "product": "Truss kit"})
    r = load_registry(store)
    assert r.usable_links() == ["https://kits.example/p/truss"] and r.program("kits").required_wording
    page = c.get("/affiliate").text
    assert "Truss kit" in page and "I re-read the terms today" in page


def test_dashboard_refuses_bad_input_with_a_message(dash, store):
    c, csrf = dash
    r = c.post("/affiliate/product", data={"csrf": csrf, "name": "X kit", "how_evaluated": "nice"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and load_registry(store).products == []
    assert c.post("/affiliate/link", data={"url": "https://x.example/"}).status_code == 403  # no CSRF token


def test_dashboard_terms_and_pause_toggle(dash, store):
    c, csrf = dash
    old = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    import_registry(store, base(programs=[dict(base()["programs"][0], terms_reviewed_on=old)]), "owner")
    assert "re-read them" in c.get("/affiliate").text
    c.post("/affiliate/terms-reviewed", data={"csrf": csrf, "id": "example-kits"})
    assert load_registry(store).programs[0].terms_reviewed_on.isoformat() == TODAY
    c.post("/affiliate/pause", data={"csrf": csrf, "id": "example-kits"})
    assert load_registry(store).usable_links() == []


def test_affiliate_page_escapes_owner_text(dash, store):
    c, _ = dash
    import_registry(store, base(products=[{"name": "<script>alert(1)</script>", "evaluated_on": "2026-01-01",
                                           "how_evaluated": "Tested it carefully for a whole week at home."}], links=[]), "owner")
    assert "<script>alert(1)" not in c.get("/affiliate").text


@pytest.mark.parametrize("text,ok", [("17, 32.5", [17.0, 32.5]), ("", []), ("1 2 3", [1.0, 2.0, 3.0])])
def test_parse_times(text, ok):
    assert parse_times(text) == ok


def test_parse_span():
    assert parse_span("0.3-5") == [0.3, 5.0] and parse_span(" ") is None
    for bad in ("5-1", "a-b", "3"):
        with pytest.raises(ValueError):
            parse_span(bad)


def test_web_demo_uses_the_same_phrase_rules():
    """site/js/disclosure.js mirrors the gate; its regex must be byte-identical to the Python one."""
    import re

    from fairclip.affiliate import PLAIN_DISCLOSURE
    js = (ROOT / "site" / "js" / "disclosure.js").read_text()
    pattern = re.search(r"const PLAIN = String\.raw`([^`]*)`", js).group(1)
    assert pattern == PLAIN_DISCLOSURE.pattern
    from fairclip.affiliate import BENIGN, DENIAL_AFTER, NEGATION
    for name, rx in (("NEGATION", NEGATION), ("DENIAL_AFTER", DENIAL_AFTER), ("BENIGN", BENIGN)):
        assert re.search(rf"const {name} = String\.raw`([^`]*)`", js).group(1) == rx.pattern, name



# ------------------------------------------------------------------------------------ review of 0.3.0
@pytest.mark.parametrize("text", ["I earn a commission if you buy", "This video is sponsored by Acme", "I'm paid by Acme for this",
                                  "paid link below", "We are compensated for this review"])
def test_real_disclosures_still_count(text):
    from fairclip.affiliate import says_paid
    assert says_paid(text)


def test_registered_link_without_affiliate_plan_is_blocked():
    """HIGH 1: a registered link in a clip with no affiliate plan (so no disclosures) must not pass."""
    from conftest import SHA, base_plan, make_ctx

    from fairclip.gate import run_gate
    va = [{"kind": "context_card", "text": "Kit I used for this build: https://example.com/kits/bridge-kit if you want one.", "start": 1.0, "end": 12.0}]
    ctx = make_ctx(plan=base_plan(value_add=va))
    ctx.registry = reg()
    codes = {r.code for r in run_gate(ctx, SHA).blocks}
    assert "AFFILIATE_LINK_OUTSIDE_PLAN" in codes


def test_paused_program_link_blocked_even_if_config_allowlists_it():
    from conftest import SHA, base_plan, make_ctx

    from fairclip.gate import run_gate
    va = [{"kind": "context_card", "text": "Kit I used for this build: https://example.com/kits/bridge-kit if you want one.", "start": 1.0, "end": 12.0}]
    ctx = make_ctx(plan=base_plan(value_add=va))  # default settings allowlist contains this URL
    ctx.registry = reg(program={"active": False})
    assert "AFFILIATE_LINK_OUTSIDE_PLAN" in {r.code for r in run_gate(ctx, SHA).blocks}


def test_negated_disclosure_is_weak_wording():
    """HIGH 2: 'I do not earn a commission' on screen is not a disclosure."""
    from conftest import SHA
    from test_gate import aff_ctx

    from fairclip.gate import run_gate
    rep = run_gate(aff_ctx(disclosure_text="I do not earn a commission from these links."), SHA)
    assert "AFFILIATE_WEAK_WORDING" in {r.code for r in rep.blocks}


def test_confirmed_time_must_match_where_it_is_said():
    """MEDIUM 2: plan says 10 s, transcript says 18.5 s: it doesn't count."""
    from conftest import SHA
    from test_gate import reg_ctx

    from fairclip.gate import run_gate
    words = said("welcome back today we build a bridge", start=0.5) + said("I earn a commission on the kit", start=18.5)
    rep = run_gate(reg_ctx(start_card=None, spoken_disclosure_at=10.0, words=words), SHA)
    codes = {r.code for r in rep.results}
    assert "AFFILIATE_START_DISCLOSURE_MISSING" in codes and "AFFILIATE_SPOKEN_NOT_HEARD" in codes


def test_resaving_program_and_product_keeps_state(dash, store):
    """MEDIUM 3: editing a note must not un-pause, re-date or un-retire anything."""
    c, csrf = dash
    old = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    d = base(programs=[dict(base()["programs"][0], terms_reviewed_on=old, active=False, joined_on="2026-01-05")])
    d["products"][0]["retired"] = True
    import_registry(store, d, "owner")
    c.post("/affiliate/program", data={"csrf": csrf, "id": "example-kits", "name": "Example Kits", "terms_url": "https://example.com/kits/terms",
                                       "link_prefixes": "https://example.com/kits/", "notes": "fixed a typo"})
    c.post("/affiliate/product", data={"csrf": csrf, "name": "Kit", "how_evaluated": "Built it twice with my class and timed the assembly again."})
    r = load_registry(store)
    p = r.program("example-kits")
    assert (p.active, p.terms_reviewed_on.isoformat(), p.joined_on.isoformat(), p.notes) == (False, old, "2026-01-05", "fixed a typo")
    assert r.product("Kit").retired is True and r.product("Kit").evaluated_on.isoformat() == "2026-02-01"


def test_pausing_a_program_stops_approval_and_upload(store, settings, tmp_path):
    """MEDIUM 4: registry changes after the gate ran still stop the clip."""
    from conftest import base_plan
    from test_gate import aff
    from test_publish import add_clip

    from fairclip.approval import ApprovalError, decide
    from fairclip.models import Decision
    from fairclip.publish.publisher import publish_approved
    import_registry(store, base(), "owner")
    plan = base_plan(clip_id="clip-aff-0001", tags=["affiliate"], affiliate=aff())
    cid, _ = add_clip(store, tmp_path, cid="clip-aff-0001", plan=plan)
    update_registry(store, lambda r: (setattr(r.programs[0], "active", False), r)[1], "owner", "pause", {})
    with pytest.raises(ApprovalError, match="registry changed"):
        decide(store, settings, Decision(clip_id=cid, approved=True, reviewer="o"))
    update_registry(store, lambda r: (setattr(r.programs[0], "active", True), r)[1], "owner", "pause", {})
    decide(store, settings, Decision(clip_id=cid, approved=True, reviewer="o"))
    update_registry(store, lambda r: (setattr(r.products[0], "retired", True), r)[1], "owner", "retire", {})
    sent = []
    res = publish_approved(store, settings, uploader=lambda b, p: sent.append(p) or "vid")
    assert not sent and "registry changed" in str(res)


def test_future_review_date_rejected():
    with pytest.raises(ValidationError, match="future"):
        AffiliateRegistry.model_validate(base(programs=[dict(base()["programs"][0], terms_reviewed_on="2999-01-01")]))


@pytest.mark.parametrize("url", ["https://example.com/kits/x?URL=HTTPS://evil.net", "https://example.com/kits/x?redir=https%3A%2F%2Fevil.net",
                                 "https://exаmple.com/kits/x"])
def test_disguised_links_cannot_be_registered(url):
    d = base()
    d["links"][0]["url"] = url
    with pytest.raises(ValidationError):
        AffiliateRegistry.model_validate(d)


def test_approval_cannot_race_an_edit(store, settings, tmp_path):
    from test_publish import add_clip

    from fairclip.approval import ApprovalError, decide
    from fairclip.models import Decision
    cid, _ = add_clip(store, tmp_path)
    assert store.set_if_status("clips", cid, {"awaiting_approval"}, status="planned")  # an edit lands first
    with pytest.raises(ApprovalError):
        decide(store, settings, Decision(clip_id=cid, approved=True, reviewer="o"))
    assert store.row("clips", cid)["status"] == "planned"


CASES = {
    "I earn a commission if you buy, at no extra cost to you.": True,
    "This isn\u2019t sponsored by anyone, and I earn nothing.": False,
    "This video is not, in any way, sponsored by them.": False,
    "I'm paid nothing for these links.": False,
    "We're paid by no one.": False,
    "Not sponsored. I earn a commission if you buy through my links.": True,
    "No matter what, I earn a commission if you buy.": True,
}


@pytest.mark.parametrize("text,expected", sorted(CASES.items()))
def test_denials_and_benign_phrases(text, expected):
    from fairclip.affiliate import says_paid
    assert says_paid(text) is expected


def test_web_checker_agrees_with_python_on_denials(tmp_path):
    """Run the real disclosure.js detector in Node on the same sentences."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (ROOT / "site" / "js" / "disclosure.js").read_text()
    body = js.split('"use strict";', 1)[1].split("  function run()")[0].replace("(() => {", "", 1)
    harness = body + """
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map((t) => detect(timed(t, 0)).length > 0)));
"""
    (tmp_path / "h.js").write_text(harness)
    texts = sorted(CASES)
    out = subprocess.run(["node", str(tmp_path / "h.js"), json.dumps(texts)], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [CASES[t] for t in texts]


def test_recheck_catches_link_moved_to_another_product(store):
    from test_gate import aff

    from fairclip.affiliate import registry_problems
    from fairclip.models import AffiliatePlan
    d = base()
    d["products"].append({"name": "Other", "evaluated_on": "2026-02-01", "how_evaluated": "Used it on three builds over a month."})
    import_registry(store, d, "owner")
    plan = AffiliatePlan.model_validate(aff())
    assert registry_problems(plan, load_registry(store)) == []
    update_registry(store, lambda r: (setattr(r.links[0], "product", "Other"), r)[1], "owner", "link", {})
    assert "now registered" in registry_problems(plan, load_registry(store))[0]
