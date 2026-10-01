"""The ten stages, wired together. Each stage records its status and log lines for the dashboard."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from . import rights as rights_mod
from .approval import maybe_auto_approve
from .config import Settings
from .gate import GateContext, History, load_screen_terms, run_gate, shingles
from .links import entry_problems
from .llm import LLMAdapter
from .media import analyze, edit, overlay, probe
from .media.sandbox import MediaRunner, MediaToolError, SandboxUnavailable, TrustManifest
from .media.timeline import TimelineMap, keep_intervals
from .metadata import build_metadata, pick_hook, shorten
from .models import (
    Candidate,
    CheckResult,
    ClipPlan,
    GateReport,
    Metadata,
    RightsRecord,
    Sentence,
    Severity,
    Transcript,
)
from .publish.body import build_body
from .scoring import default_topics, rank_candidates
from .store import Store, now, sha256_file
from .transcribe import load_sidecar, whisper_transcribe

log = logging.getLogger("clipwarden")
REPO_ROOT = Path(__file__).resolve().parents[2]
STAGES = ["rights", "transcribe", "highlights", "edit", "value_add", "metadata", "gate", "approval", "publish", "analytics"]


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, settings: Settings, store: Store | None = None, llm: LLMAdapter | None = None) -> None:
        self.s = settings
        self.store = store or Store(settings.data_path)
        self.trust = TrustManifest(self.store.root / "trusted-media.json")
        self.trust.seed_from_sums(REPO_ROOT / "examples" / "demo" / "SHA256SUMS")
        self.runner = MediaRunner(image=settings.media.image, mode=settings.media.runner, trust=self.trust,
                                  memory=settings.media.memory, cpus=settings.media.cpus,
                                  pids=settings.media.pids, timeout_s=settings.media.timeout_s)
        self.llm = llm
        self.work = Path(settings.media.jobs_dir) if settings.media.runner == "worker" and settings.media.jobs_dir else self.store.root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        bad = {u: entry_problems(u) for u in settings.link_allowlist if entry_problems(u)}
        if bad:
            raise PipelineError(f"unsafe link allowlist entries: {bad}")

    # ------------------------------------------------------------------ stage 1-3
    def ingest(self, media: Path, rights_file: Path, transcript_file: Path | None = None, actor: str = "cli") -> str:
        try:
            rec = rights_mod.load_rights(rights_file)
        except rights_mod.RightsError as exc:
            self.store.audit.append(actor, "ingest_refused", media.name, {"reason": str(exc)})
            raise
        probe.sniff_container(media)  # magic bytes on the host, before any decoder
        sha = sha256_file(media)
        try:
            rights_mod.check_intake(rec, sha, self.s.channel.monetized)
        except rights_mod.RightsError as exc:
            self.store.audit.append(actor, "ingest_refused", media.name, {"reason": str(exc), "rights_record": rec.record_id})
            raise
        source_id = f"src-{sha[:12]}"
        self.store.put_media(media)
        self.store.upsert("sources", source_id, created=now(), rights=rec.model_dump(mode="json"), media_sha=sha)
        self.store.stage(source_id, "rights", "done", f"{rec.basis.value} record {rec.record_id} accepted")
        self.store.audit.append(actor, "ingest", source_id, {"rights_record": rec.record_id, "basis": rec.basis.value, "sha256": sha})
        stored = self.store.find_media(sha)

        try:
            pr = probe.probe(self.runner, stored, work=self.work, max_bytes=self.s.media.max_input_bytes, max_duration=self.s.media.max_duration_s,
                             max_w=self.s.media.max_width, max_h=self.s.media.max_height)
        except (probe.MediaRejected, MediaToolError, SandboxUnavailable) as exc:
            self.store.stage(source_id, "rights", "blocked", f"media rejected: {str(exc)[:300]}")
            self.store.audit.append(actor, "media_rejected", source_id, {"reason": str(exc)[:500]})
            raise PipelineError(f"media rejected: {exc}") from exc
        self.store.upsert("sources", source_id, probe=pr.to_dict())
        self.store.stage(source_id, "rights", "done", f"probe ok via {self.runner.last_backend}: {pr.duration:.1f}s {pr.width}x{pr.height} {pr.vcodec}/{pr.acodec}")

        self.store.stage(source_id, "transcribe", "running")
        audio = analyze.extract_audio(self.runner, stored, self.work)
        adir = self.analysis_dir(source_id)
        np.save(adir / "rms_db.npy", audio.rms_db)
        if transcript_file:
            tr = load_sidecar(transcript_file)
            engine = "sidecar"
        else:
            pcm = self._pcm(stored)
            tr = whisper_transcribe(pcm)
            engine = tr.engine
        self.store.upsert("sources", source_id, transcript=tr.model_dump(mode="json"))
        self.store.stage(source_id, "transcribe", "done", f"{len(tr.words)} words, {len(tr.sentences)} sentences ({engine})")

        self.store.stage(source_id, "highlights", "running")
        cuts = analyze.scene_cuts(self.runner, stored, self.work)
        (adir / "cuts.json").write_text(json.dumps(cuts))
        topics = self.s.channel.topics or default_topics(tr)
        cands = rank_candidates(source_id, tr, audio, cuts, topics, self.weights(), min_s=self.s.scoring.clip_min_s,
                                max_s=self.s.scoring.clip_max_s, k=self.s.scoring.max_candidates)
        for c in cands:
            self.store.upsert("candidates", c.candidate_id, source_id=source_id, rank=c.rank, data=c.model_dump(mode="json"))
        self.store.stage(source_id, "highlights", "done", f"{len(cuts)} scene cuts; top {len(cands)} candidates ranked")
        return source_id

    def _pcm(self, stored: Path) -> np.ndarray:
        in_dir, out_dir, (src,) = analyze.job_dirs(self.work, [stored])
        self.runner.run("ffmpeg", ["-protocol_whitelist", "file", "-i", f"@in/{src.name}", "-vn", "-ac", "1", "-ar", "16000",
                                   "-f", "s16le", "@out/a.raw"], in_dir=in_dir, out_dir=out_dir, inputs=[src])
        return np.fromfile(out_dir / "a.raw", dtype="<i2")

    def analysis_dir(self, source_id: str) -> Path:
        d = self.store.root / "analysis" / source_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def weights(self) -> dict[str, float]:
        return self.store.get("weights") or self.s.scoring.weights

    # ------------------------------------------------------------------ plan
    def plan_from_candidate(self, candidate_id: str, actor: str = "cli", **overrides: Any) -> ClipPlan:
        row = self.store.row("candidates", candidate_id)
        if row is None:
            raise PipelineError(f"no candidate {candidate_id}")
        c = Candidate.model_validate(row["data"])
        base = {"clip_id": f"clip-{candidate_id.split('-c')[-1]}-{hashlib.sha1(candidate_id.encode()).hexdigest()[:6]}",  # noqa: S324 - id only
                "source_id": c.source_id, "start": c.start, "end": c.end}
        plan = ClipPlan.model_validate(base | overrides)
        self.save_plan(plan, actor=actor)
        return plan

    def save_plan(self, plan: ClipPlan, actor: str = "cli") -> None:
        existing = self.store.row("clips", plan.clip_id)
        if existing and existing.get("status") in {"approved", "uploading", "published", "dry_run_published"}:
            raise PipelineError("clip already approved/published; plans are frozen after approval")
        self.store.upsert("clips", plan.clip_id, source_id=plan.source_id, created=now(), plan=plan.model_dump(mode="json"), status="planned")
        self.store.audit.append(actor, "plan", plan.clip_id, {"start": plan.start, "end": plan.end, "template": plan.template,
                                                               "value_add": len(plan.value_add), "tags": plan.tags})

    # ------------------------------------------------------------------ stage 4-7
    def render_and_gate(self, clip_id: str, actor: str = "cli") -> GateReport:
        row = self.store.row("clips", clip_id)
        if row is None:
            raise PipelineError(f"no clip {clip_id}")
        if self.store.kill_switch:
            raise PipelineError("kill switch is on")
        plan = ClipPlan.model_validate(row["plan"])
        src = self.store.row("sources", plan.source_id)
        if src is None:
            raise PipelineError("source missing")
        rec = RightsRecord.model_validate(src["rights"]) if src.get("rights") else None
        tr = Transcript.model_validate(src["transcript"])
        stored = self.store.find_media(src["media_sha"])
        rms = np.load(self.analysis_dir(plan.source_id) / "rms_db.npy")

        sid = plan.source_id
        self.store.stage(sid, "edit", "running", f"{clip_id}: cutting pauses > {plan.remove_silences_longer_than}s")
        words = [w for w in tr.words if w.start >= plan.start - 1e-6 and w.end <= plan.end + 1e-6]
        tmap = TimelineMap(keep_intervals(tr.words, plan.start, plan.end, plan.remove_silences_longer_than, rms))
        sents = [s for s in tr.sentences if s.start >= plan.start - 1e-6 and s.end <= plan.end + 1e-6]
        topics = self.s.channel.topics or default_topics(tr)
        hook = pick_hook(sents, topics)
        attribution = rights_mod.attribution_line(rec) if rec else ""
        ass = overlay.build_ass(plan, words, tmap, hook_text=f"“{shorten(hook, 72)}”" if hook else "", channel=self.s.channel.name, attribution=attribution)
        cx = 0.5
        if plan.reframe.value == "crop":
            cx = analyze.motion_centroid_x(self.runner, stored, self.work, plan.start, plan.end)
        res = edit.render(self.runner, self.work, stored, plan, tmap, ass, crop_cx=cx)
        clip_sha = self.store.put_media(res.clip, ".mp4", move=True)
        prev_sha = self.store.put_media(res.preview, ".mp4", move=True)
        posters = {}
        for key, sha, t in (("clip", clip_sha, min(5.0, res.duration / 3)), ("before", prev_sha, min(5.0, (plan.end - plan.start) / 3))):
            img = edit.extract_frame(self.runner, self.work, self.store.find_media(sha), t, "poster.jpg")
            posters[key] = self.store.put_media(img, ".jpg", move=True)
        self.store.stage(sid, "edit", "done", f"{clip_id}: {res.duration:.1f}s clip, {res.removed_s:.1f}s of pauses removed ({res.backend})")
        self.store.stage(sid, "value_add", "done", f"{clip_id}: {len(plan.value_add)} element(s), template '{plan.template}'")

        # Metadata on the clip's own timeline (chapters must match what viewers see).
        mapped = [Sentence(start=tmap.clamp_to_clip(s.start), end=tmap.clamp_to_clip(s.end), text=s.text, words=[]) for s in sents]
        clip_plan_for_md = plan.model_copy(update={"start": 0.0, "end": max(tmap.duration, 5.0)})
        gate_extra: list[CheckResult] = []
        try:
            md, notes = build_metadata(clip_plan_for_md, mapped, rec, attribution, topics, self.llm) if rec else (None, [])
        except ValidationError as exc:
            md, notes = None, [f"metadata invalid: {exc.error_count()} errors"]
            gate_extra.append(CheckResult(code="METADATA_LIMITS", check="Metadata", severity=Severity.BLOCK,
                                          message="metadata failed YouTube limits — fix: shorten title/description"))
        if md is None:
            md = Metadata(title="(missing)", description="")
        self.store.stage(sid, "metadata", "done", f"{clip_id}: " + "; ".join(notes))
        body = build_body(md, plan, rec) if rec else {"snippet": {}, "status": {}}

        clip_path = self.store.find_media(clip_sha)
        fp = {"dhash": analyze.frame_dhashes(self.runner, clip_path, self.work),
              "shingles": sorted(hashlib.sha1(s.encode()).hexdigest()[:12] for s in shingles(" ".join(w.text for w in words), 5))}  # noqa: S324
        ctx = GateContext(plan=plan, rights=rec, metadata=md, body=body, transcript_text=" ".join(w.text for w in words),
                          clip_duration=tmap.duration, fingerprint=fp, settings=self.s, history=self.history(plan, clip_id),
                          kill_switch=self.store.kill_switch, screen_terms=load_screen_terms(self.screen_terms_path()),
                          media_sha256=src["media_sha"])
        report = run_gate(ctx, clip_sha)
        if gate_extra:
            report = report.model_copy(update={"results": gate_extra + report.results, "passed": False})
        status = "awaiting_approval" if report.passed else "blocked"
        self.store.upsert("clips", clip_id, clip_sha=clip_sha, preview_sha=prev_sha, posters=posters, metadata=md.model_dump(mode="json"),
                          body=body, gate=report.model_dump(mode="json"), status=status, fingerprint=fp)
        self.store.stage(sid, "gate", "done" if report.passed else "blocked",
                         f"{clip_id}: {len(report.blocks)} block(s), {len(report.warnings)} warning(s)")
        self.store.stage(sid, "approval", "waiting" if report.passed else "n/a", f"{clip_id}: {status}")
        self.store.audit.append(actor, "gate", clip_id, {"passed": report.passed, "blocks": [b.code for b in report.blocks],
                                                         "warnings": [w.code for w in report.warnings], "clip_sha256": clip_sha,
                                                         "metadata_sha256": report.metadata_sha256})
        self.store.clip_log(clip_id, f"rendered + gated: {status}")
        if report.passed and maybe_auto_approve(self.store, self.s, clip_id):
            self.store.clip_log(clip_id, "auto-approved by policy (private upload)")
        return report

    def screen_terms_path(self) -> Path:
        local = Path("config/screen_terms.txt")
        return local if local.exists() else REPO_ROOT / "config" / "screen_terms.txt"

    def history(self, plan: ClipPlan, clip_id: str) -> History:
        h = History()
        today = datetime.now(UTC).date().isoformat()
        rows = self.store.rows("clips", "status IN ('approved','uploading','published','dry_run_published') AND id != ?", (clip_id,), order="created DESC")
        for r in rows:
            md = r.get("metadata") or {}
            h.titles.append(md.get("title", ""))
            h.templates.append((r.get("plan") or {}).get("template", ""))
            if r.get("fingerprint"):
                h.fingerprints.append(r["fingerprint"])
            d = r.get("decision") or {}
            if str(d.get("publish_at") or d.get("decided_at") or "")[:10] == today:
                h.today_count += 1
                if r.get("source_id") == plan.source_id:
                    h.today_same_source += 1
        return h

    # ------------------------------------------------------------------ export for the Pages demo
    def export_demo(self, source_id: str, out: Path) -> dict[str, Any]:
        src = self.store.row("sources", source_id) or {}
        cands = [r["data"] for r in self.store.rows("candidates", "source_id=?", (source_id,), order="rank")]
        data = {"source_id": source_id, "probe": src.get("probe"), "weights": self.weights(), "candidates": cands,
                "stages": src.get("stages"), "exported": now()}
        out.write_text(json.dumps(data, indent=1))
        return data
