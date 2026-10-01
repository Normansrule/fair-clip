from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clipwarden.config import Settings  # noqa: E402
from clipwarden.gate import GateContext, History  # noqa: E402
from clipwarden.metadata import assemble_description  # noqa: E402
from clipwarden.models import ClipPlan, Metadata, RightsRecord, ValueAddElement  # noqa: E402
from clipwarden.publish.body import build_body  # noqa: E402
from clipwarden.store import Store  # noqa: E402

TRANSCRIPT = ("So what actually threatens a bridge like this? Usually it is not weight. It is wind. "
              "The deck was thin and flexible, so the wind made it flutter.")
SHA = "a" * 64


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=str(tmp_path / "data"), link_allowlist=["https://example.com/kits/bridge-kit"])
    s.channel.name = "Bridge Basics"
    return s


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.data_path)


def own_rights(**kw) -> RightsRecord:
    base = {"record_id": "own-001", "basis": "own_content", "source_title": "My talk", "rights_holder": "Me",
            "evidence": "I filmed it", "source_sha256": SHA}
    return RightsRecord.model_validate(base | kw)


def base_plan(**kw) -> ClipPlan:
    base = {
        "clip_id": "clip-test-01", "source_id": "src-test", "start": 10.0, "end": 40.0,
        "value_add": [ValueAddElement(kind="context_card", text="Flutter is a feedback loop where each twist feeds the next one.",
                                      start=1.0, end=12.0).model_dump()],
    }
    return ClipPlan.model_validate(base | kw)


def make_ctx(plan: ClipPlan | None = None, rights: RightsRecord | None | str = "default", title: str | None = None,
             transcript: str = TRANSCRIPT, settings: Settings | None = None, history: History | None = None,
             description: str | None = None, body_patch: dict | None = None, **kw) -> GateContext:
    plan = plan or base_plan()
    rec = own_rights() if rights == "default" else rights
    s = settings or Settings(link_allowlist=["https://example.com/kits/bridge-kit"])
    s.channel.name = "Bridge Basics"
    desc = description if description is not None else assemble_description(plan, "A plain summary of the clip.", rec or own_rights(), "", [])
    md = Metadata(title=title or "So what actually threatens a bridge like this? It is wind.", description=desc, tags=["bridge", "wind"])
    body = build_body(md, plan, rec or own_rights())
    for k, v in (body_patch or {}).items():
        body[k].update(v)
    return GateContext(plan=plan, rights=rec, metadata=md, body=body, transcript_text=transcript, clip_duration=30.0,
                       fingerprint=kw.pop("fingerprint", {"dhash": [], "shingles": []}), settings=s,
                       history=history or History(), kill_switch=kw.pop("kill", False),
                       screen_terms=kw.pop("screen_terms", {"hard": ["kys"], "soft": ["damn"]}),
                       media_sha256=kw.pop("media_sha", SHA), now=kw.pop("now", datetime.now(UTC)))
