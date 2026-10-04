"""Audit log, rights, links, timeline, overlay, vault, redaction, probing, config."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from conftest import own_rights
from pydantic import ValidationError

from fairclip.audit import AuditLog
from fairclip.config import Settings, load_settings
from fairclip.links import check_links, entry_problems, find_links
from fairclip.media.overlay import build_ass, escape
from fairclip.media.probe import MediaRejected, parse_probe, sniff_container
from fairclip.media.timeline import TimelineMap, keep_intervals
from fairclip.models import ClipPlan, RightsRecord, Word
from fairclip.redact import redact
from fairclip.rights import RightsError, attribution_line, load_rights, rights_problems
from fairclip.vault import EncryptedFileVault, VaultError

# ------------------------------------------------------------------------------------ audit


def test_audit_chain_and_append_only(tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    conn = sqlite3.connect(db)
    log = AuditLog(conn)
    for i in range(5):
        log.append("me", "approve", f"clip-{i}", {"i": i})
    assert log.verify() == (True, None)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit SET actor='mallory' WHERE seq=2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit WHERE seq=2")
    # An attacker with raw file access drops the trigger and edits a row: the chain exposes it.
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit SET details='{\"i\":99}' WHERE seq=3")
    assert log.verify() == (False, 3)


# ------------------------------------------------------------------------------------ rights


def test_rights_missing_file_is_hard_stop(tmp_path: Path):
    with pytest.raises(RightsError, match="refuses"):
        load_rights(tmp_path / "nope.json")


def test_rights_cc_requires_license_and_attribution():
    with pytest.raises(ValidationError):
        RightsRecord.model_validate({"record_id": "abc", "basis": "creative_commons", "source_title": "t", "rights_holder": "h", "evidence": "e"})
    with pytest.raises(ValidationError):
        own_rights(basis="creative_commons", cc_license="CC-BY-4.0")


def test_rights_problems_and_attribution():
    r = own_rights(expires=date(2001, 1, 1), allows_youtube=False)
    codes = {c for c, _ in rights_problems(r, media_sha256="a" * 64, monetized=True)}
    assert {"RIGHTS_EXPIRED", "RIGHTS_SCOPE"} <= codes
    cc = own_rights(basis="creative_commons", cc_license="CC-BY-4.0", attribution="Talk by A. Person")
    line = attribution_line(cc)
    # CC BY 4.0 §3(a): creator, license with a link, and a note that the work was modified.
    assert line.startswith("Source: Talk by A. Person, CC-BY-4.0 https://creativecommons.org/licenses/by/4.0/")
    assert "edited" in line
    assert len(attribution_line(cc, short=True)) < len(line)
    assert attribution_line(own_rights()) == ""


def test_rights_unknown_fields_rejected(tmp_path: Path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(own_rights().model_dump(mode="json") | {"skip_gate": True}))
    with pytest.raises(RightsError):
        load_rights(p)


# ------------------------------------------------------------------------------------ links


def test_links_exact_match_only():
    allow = ["https://example.com/kits/bridge-kit"]
    bad, cloaked = check_links("buy https://example.com/kits/bridge-kit?x=1 or https://example.com/kits/bridge-kit", allow)
    assert bad == ["https://example.com/kits/bridge-kit?x=1"] and cloaked == []


@pytest.mark.parametrize("url,why", [
    ("http://example.com/a", "not https"),
    ("https://example.com@evil.net/a", "user-info"),
    ("https://bit.ly/x", "shortener"),
    ("https://xn--exmple-cua.com/", "punycode"),
    ("https://shop.example.com/r?url=https://evil.net", "redirect"),
])
def test_allowlist_entry_problems(url, why):
    assert any(why in p for p in entry_problems(url))


def test_find_bare_domains():
    assert find_links("go to evil-deals.net/buy now") == ["evil-deals.net/buy"]
    assert find_links("no links here, e.g. none") == []


# ------------------------------------------------------------------------------------ timeline


def W(t, a, b):
    return Word(text=t, start=a, end=b)


def test_keep_intervals_cuts_only_long_quiet_gaps():
    words = [W("a", 0.0, 0.5), W("b", 0.6, 1.0), W("c", 3.0, 3.5), W("d", 3.6, 4.0)]
    iv = keep_intervals(words, 0.0, 4.0, max_gap=0.6)
    assert len(iv) == 2 and iv[0].b == pytest.approx(1.12) and iv[1].a == pytest.approx(2.88)
    loud = np.full(100, -10.0)  # music in the gap: not quiet, so not cut
    assert len(keep_intervals(words, 0.0, 4.0, max_gap=0.6, rms_db=loud)) == 1


def test_timeline_map():
    words = [W("a", 0.0, 0.5), W("b", 3.0, 3.5)]
    tm = TimelineMap(keep_intervals(words, 0.0, 3.5, 0.6))
    assert tm.to_clip(0.0) == 0.0
    assert tm.to_clip(1.5) is None  # inside the removed pause
    assert tm.to_clip(3.0) == pytest.approx(0.62 + 0.12)
    assert tm.duration == pytest.approx(3.5 - (2.88 - 0.62))


# ------------------------------------------------------------------------------------ overlay


def test_escape_neutralises_ass_tags():
    out = escape(r"{\alpha&HFF&}secret\Nline")
    assert "{" not in out and "}" not in out and "\\" not in out


def test_ass_contains_disclosures_captions_and_attribution():
    plan = ClipPlan.model_validate({"clip_id": "c-001", "source_id": "s", "start": 0, "end": 10,
                                    "value_add": [{"kind": "commentary", "text": "My own point.", "start": 1, "end": 4}],
                                    "affiliate": {"start_card": [0.2, 3.0], "recommendation_cards": [[5, 7]]}})
    words = [W("hello", 0.5, 0.9), W("world", 1.0, 1.4)]
    ass = build_ass(plan, words, TimelineMap(keep_intervals(words, 0, 10, 5.0)), hook_text="hi", channel="Ch", attribution="Source: X (CC-BY-4.0)")
    assert ass.count(",Disclose,") == 2 and ass.count(",PCaption,") == 2  # default "bold" preset
    assert "Source: X (CC-BY-4.0)" in ass and "MY TAKE" in ass
    clean = build_ass(plan.model_copy(update={"style": "clean"}), words, TimelineMap(keep_intervals(words, 0, 10, 5.0)),
                      hook_text="hi", channel="Ch", attribution="Source: X (CC-BY-4.0)")
    assert clean.count(",Caption,") == 2 and "PCaption" not in clean  # the original look is still there


# ------------------------------------------------------------------------------------ vault + redaction


def test_vault_roundtrip_and_tamper(tmp_path: Path):
    v = EncryptedFileVault(tmp_path / "v.enc", passphrase="correct horse battery staple")
    v.save("yt", {"refresh_token": "1//abcdefghijklmnop"})
    assert stat.S_IMODE(os.stat(tmp_path / "v.enc").st_mode) == 0o600
    assert "abcdefghijklmnop" not in (tmp_path / "v.enc").read_text()
    assert v.load("yt") == {"refresh_token": "1//abcdefghijklmnop"}
    with pytest.raises(VaultError):
        EncryptedFileVault(tmp_path / "v.enc", passphrase="wrong passphrase!!").load("yt")
    data = json.loads((tmp_path / "v.enc").read_text())
    data["yt"]["ct"] = data["yt"]["ct"][:-4] + "AAAA"
    (tmp_path / "v.enc").write_text(json.dumps(data))
    with pytest.raises(VaultError):
        v.load("yt")
    with pytest.raises(VaultError):
        EncryptedFileVault(tmp_path / "w.enc", passphrase="short").save("x", {})


def test_redaction():
    line = "token ya29.a0AfH6SMBxxxxxxxxxxxx refresh 1//0gXXXXXXXXXXXXX secret GOCSPX-abcdefghijklmnop Bearer abcdefghijklmnopqrst"
    out = redact(line)
    assert "ya29.a0" not in out and "1//0g" not in out and "GOCSPX-a" not in out and "abcdefghijklmnopqrst" not in out


# ------------------------------------------------------------------------------------ probing


@pytest.mark.parametrize("name,head", [
    ("a.m3u8", b"#EXTM3U\n#EXT-X-VERSION:3\n"),
    ("a.mp4", b"#EXTM3U\n#EXT-X-VERSION:3\n"),  # playlist disguised as .mp4
    ("a.mp4", b"\x89PNG\r\n\x1a\n" + b"\0" * 8),
    ("a.mp4", b"short"),
])
def test_sniff_rejects(tmp_path: Path, name, head):
    p = tmp_path / name
    p.write_bytes(head)
    with pytest.raises(MediaRejected):
        sniff_container(p)


def test_sniff_accepts_mp4_and_mkv(tmp_path: Path):
    (tmp_path / "a.mp4").write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 8)
    (tmp_path / "b.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 12)
    assert sniff_container(tmp_path / "a.mp4") == "isobmff" and sniff_container(tmp_path / "b.mkv") == "matroska"


def probe_json(**kw):
    v = {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "avg_frame_rate": "25/1"}
    a = {"codec_type": "audio", "codec_name": "aac"}
    return json.dumps({"streams": [v | kw.get("v", {}), a | kw.get("a", {})], "format": {"duration": kw.get("dur", "60.0")}})


def test_parse_probe_limits():
    ok = parse_probe(probe_json(), "isobmff", max_duration=3600, max_w=3840, max_h=2160)
    assert ok.fps == 25 and ok.width == 1280
    for bad in (probe_json(v={"codec_name": "mjpeg"}), probe_json(a={"codec_name": "dts"}), probe_json(dur="99999"),
                probe_json(v={"width": 20000})):
        with pytest.raises(MediaRejected):
            parse_probe(bad, "isobmff", max_duration=3600, max_w=3840, max_h=2160)


# ------------------------------------------------------------------------------------ config floors/ceilings


def test_config_safety_limits(tmp_path: Path, monkeypatch):
    with pytest.raises(ValidationError):
        Settings.model_validate({"channel": {"daily_cap": 50}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"gate": {"min_value_add_ratio": 0.05}})
    cfg = tmp_path / "c.toml"
    cfg.write_text('[dashboard]\nhost = "0.0.0.0"\n')
    monkeypatch.delenv("FAIRCLIP_IN_CONTAINER", raising=False)
    with pytest.raises(ValueError, match="only allowed inside"):
        load_settings(cfg)
    with pytest.raises(ValidationError):
        Settings.model_validate({"surprise": 1})
