"""Configuration: one TOML file per deployment, parsed into a strict schema.

Safety-relevant settings have hard floors/ceilings that the config file cannot move. For
example, the daily publishing cap can be lowered but never set above HARD_DAILY_CAP, and human
approval can be switched off only for plain own-content clips (never markets or affiliate).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

HARD_DAILY_CAP = 10  # code-level ceiling; see docs/POLICY.md "mass production"
HARD_MIN_VALUE_ADD_RATIO = 0.15


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelConfig(_S):
    name: str = "My Channel"
    topics: list[str] = Field(default_factory=list, max_length=50)
    monetized: bool = True
    daily_cap: Annotated[int, Field(ge=1, le=HARD_DAILY_CAP)] = 3
    max_clips_per_source_per_day: Annotated[int, Field(ge=1, le=5)] = 2
    timezone: str = "UTC"


class GateConfig(_S):
    min_value_add_ratio: Annotated[float, Field(ge=HARD_MIN_VALUE_ADD_RATIO, le=1.0)] = 0.25
    min_value_add_words: Annotated[int, Field(ge=5, le=200)] = 8
    max_value_add_transcript_overlap: Annotated[float, Field(ge=0.1, le=0.9)] = 0.6
    markets_max_data_age_hours: Annotated[float, Field(gt=0, le=24 * 30)] = 72.0
    duplicate_hamming_threshold: Annotated[int, Field(ge=0, le=20)] = 10
    duplicate_text_jaccard: Annotated[float, Field(ge=0.3, le=1.0)] = 0.8
    variety_title_jaccard: Annotated[float, Field(ge=0.3, le=1.0)] = 0.6


class ScoringConfig(_S):
    clip_min_s: Annotated[float, Field(ge=5, le=60)] = 20.0
    clip_max_s: Annotated[float, Field(ge=10, le=180)] = 60.0
    max_candidates: Annotated[int, Field(ge=1, le=100)] = 10
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "energy": 0.25,
            "relevance": 0.35,
            "rate": 0.15,
            "scene": 0.10,
            "silence": 0.15,
        }
    )


class MediaConfig(_S):
    # docker: per-job `docker run` sandbox (host install; strongest isolation)
    # worker: long-running isolated media container sharing a jobs volume (docker-compose; no socket)
    # auto:   docker if the image exists; otherwise only the pinned trusted demo files may run on the host
    runner: Literal["docker", "auto", "worker"] = "auto"
    jobs_dir: str = ""  # worker mode: the shared volume path (same path in both containers)
    image: str = "clipwarden-media:0.1.0"
    memory: str = "2g"
    cpus: str = "2"
    pids: Annotated[int, Field(ge=32, le=4096)] = 256
    timeout_s: Annotated[int, Field(ge=10, le=7200)] = 900
    max_input_bytes: Annotated[int, Field(ge=1_000_000)] = 8 * 1024**3
    max_duration_s: Annotated[float, Field(gt=0)] = 4 * 3600
    max_width: int = 3840
    max_height: int = 2160


class PublishConfig(_S):
    mode: Literal["dry-run", "live"] = "dry-run"
    require_human_approval: bool = True
    oauth_client_file: str = "client_secret.json"
    # Narrowest scope that can upload. We set privacy + publishAt at insert time, after
    # approval, so we never need the broad "youtube" (manage account) scope.
    scopes: list[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/youtube.upload"]
    )


class DashboardConfig(_S):
    # Inside the compose app container we must listen on all container interfaces; compose then
    # publishes the port on the HOST's 127.0.0.1 only. Outside a container, 0.0.0.0 is refused.
    host: Literal["127.0.0.1", "::1", "0.0.0.0"] = "127.0.0.1"  # noqa: S104
    port: Annotated[int, Field(ge=1024, le=65535)] = 8765


class Settings(_S):
    data_dir: str = "./data"
    channel: ChannelConfig = Field(default_factory=ChannelConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    link_allowlist: list[str] = Field(default_factory=list, max_length=200)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    p = Path(path or os.environ.get("CLIPWARDEN_CONFIG", "clipwarden.toml"))
    if not p.exists():
        return Settings()
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    s = Settings.model_validate(raw)
    if s.dashboard.host == "0.0.0.0" and os.environ.get("CLIPWARDEN_IN_CONTAINER") != "1":  # noqa: S104
        raise ValueError("dashboard.host 0.0.0.0 is only allowed inside the compose container")
    if not Path(s.data_dir).is_absolute():
        s.data_dir = str((p.parent / s.data_dir).resolve())
    return s
