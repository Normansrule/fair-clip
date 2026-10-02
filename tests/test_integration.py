"""End to end on the bundled CC0 sample: ingest -> rank -> render -> gate -> approve -> dry-run publish."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipwarden.approval import decide
from clipwarden.config import Settings
from clipwarden.models import Decision
from clipwarden.pipeline import Pipeline
from clipwarden.publish.publisher import publish_approved
from clipwarden.rights import RightsError

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo"
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not (DEMO / "source.mp4").exists(), reason="needs ffmpeg + sample")


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_refuses_without_rights(tmp_path: Path):
    p = Pipeline(Settings(data_dir=str(tmp_path / "d")))
    with pytest.raises(RightsError):
        p.ingest(DEMO / "source.mp4", tmp_path / "missing.json")
    assert p.store.audit.entries(limit=1)[0].action == "ingest_refused"
    assert not list((tmp_path / "d" / "media").rglob("*.mp4"))  # nothing stored


def test_refuses_rights_for_a_different_file(tmp_path: Path):
    rights = json.loads((DEMO / "rights.json").read_text()) | {"source_sha256": "0" * 64}
    (tmp_path / "r.json").write_text(json.dumps(rights))
    with pytest.raises(RightsError, match="hash"):
        Pipeline(Settings(data_dir=str(tmp_path / "d"))).ingest(DEMO / "source.mp4", tmp_path / "r.json")


def test_full_pipeline_dry_run(tmp_path: Path):
    s = Settings(data_dir=str(tmp_path / "d"))
    s.channel.topics = ["bridge", "cable", "tension", "compression", "tower", "wind", "deck"]
    p = Pipeline(s)
    sid = p.ingest(DEMO / "source.mp4", DEMO / "rights.json", DEMO / "transcript.json")
    cands = p.store.rows("candidates", "source_id=?", (sid,), order="rank")
    assert len(cands) >= 3 and cands[0]["data"]["score"] >= cands[-1]["data"]["score"]
    plan = p.plan_from_candidate(cands[0]["id"], **json.loads((DEMO / "plan.json").read_text()))
    rep = p.render_and_gate(plan.clip_id)
    assert rep.passed, [(r.code, r.message) for r in rep.blocks]

    row = p.store.row("clips", plan.clip_id)
    info = probe(p.store.find_media(row["clip_sha"]))
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    dur = float(info["format"]["duration"])
    assert dur < plan.end - plan.start  # pauses were cut
    assert row["body"]["status"]["containsSyntheticMedia"] is True  # synthetic narrator -> disclosed
    assert "Sample talk by clipwarden contributors, CC0-1.0" in row["metadata"]["description"]
    assert p.store.find_media(row["posters"]["clip"]).suffix == ".jpg"  # review-page poster frame

    decide(p.store, s, Decision(clip_id=plan.clip_id, approved=True, reviewer="tester", reason="ok"))
    assert publish_approved(p.store, s)[0]["status"] == "dry_run_published"
    ok, _ = p.store.audit.verify()
    actions = [e.action for e in reversed(p.store.audit.entries(limit=50))]
    assert ok and actions == ["ingest", "plan", "gate", "approve", "dry_run_publish"]

    # Same moment again: the gate must catch the near-duplicate of our own (dry-run) upload.
    plan2 = p.plan_from_candidate(cands[0]["id"], **(json.loads((DEMO / "plan.json").read_text()) | {"clip_id": "clip-again-01"}))
    rep2 = p.render_and_gate(plan2.clip_id)
    assert "DUPLICATE_OF_OWN_UPLOAD" in {r.code for r in rep2.blocks}
