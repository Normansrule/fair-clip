"""The ten stages, wired together. Each stage records its status and log lines for the dashboard."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import rights as rights_mod
from . import studio
from .affiliate import detect_spoken, load_registry
from .approval import maybe_auto_approve
from .config import Settings
from .gate import History, load_screen_terms
from .links import entry_problems
from .llm import LLMAdapter
from .media import analyze, edit, probe
from .media.sandbox import MediaRunner, MediaToolError, SandboxUnavailable, TrustManifest
from .media.timeline import TimelineMap
from .models import (
    Candidate,
    CheckResult,
    ClipPlan,
    GateReport,
    Metadata,
    RightsRecord,
    Sentence,
    Transcript,
)
from .scoring import default_topics, rank_candidates
from .store import CLIP_STATES, Store, now, sha256_file
from .transcribe import load_sidecar, whisper_transcribe

log = logging.getLogger("fair-clip")
REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DATA = Path(__file__).resolve().parent / "data"
STAGES = ["rights", "transcribe", "highlights", "edit", "value_add", "metadata", "gate", "approval", "publish", "analytics"]


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, settings: Settings, store: Store | None = None, llm: LLMAdapter | None = None) -> None:
        self.s = settings
        self.store = store or Store(settings.data_path)
        self.trust = TrustManifest(self.store.root / "trusted-media.json")
        for ex in ("demo", "studio"):  # the bundled CC0 examples are ours: they may run without Docker
            self.trust.seed_from_sums(REPO_ROOT / "examples" / ex / "SHA256SUMS")
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
        if existing and existing.get("status") in self.FROZEN:
            raise PipelineError("clip already approved/published; plans are frozen after approval")
        fields = {"plan": plan.model_dump(mode="json"), "status": "planned"}
        if existing:  # checked and written together, so an approval can't slip in between
            if not self.store.set_if_status("clips", plan.clip_id, set(CLIP_STATES) - self.FROZEN, **fields):
                raise PipelineError("clip was approved while you edited it; plans are frozen after approval")
        else:
            self.store.upsert("clips", plan.clip_id, source_id=plan.source_id, created=now(), **fields)
        self.store.audit.append(actor, "plan", plan.clip_id, {"start": plan.start, "end": plan.end, "template": plan.template,
                                                               "value_add": len(plan.value_add), "tags": plan.tags})

    def edit_plan(self, clip_id: str, changes: dict[str, Any], actor: str) -> GateReport:
        """Change part of a clip's plan (e.g. its affiliate section from the dashboard), then render
        and gate it again. Frozen (approved/published) clips refuse, like everywhere else."""
        row = self.store.row("clips", clip_id)
        if row is None:
            raise PipelineError(f"no clip {clip_id}")
        if row.get("status") in self.FROZEN:
            raise PipelineError(f"clip is '{row['status']}': approved/published clips are frozen")
        plan = ClipPlan.model_validate(row["plan"] | changes)
        self.save_plan(plan, actor=actor)
        self.store.audit.append(actor, "plan_edit", clip_id, {"fields": sorted(changes)})
        return self.render_and_gate(clip_id, actor=actor)

    def heard_disclosures(self, clip_id: str) -> list[tuple[float, str]]:
        """Disclosure phrases actually said in this clip, on the clip's own timeline."""
        row = self.store.row("clips", clip_id)
        if row is None:
            raise PipelineError(f"no clip {clip_id}")
        plan = ClipPlan.model_validate(row["plan"])
        src = self.store.row("sources", plan.source_id) or {}
        if not src.get("transcript"):
            return []
        tr = Transcript.model_validate(src["transcript"])
        rms = np.load(self.analysis_dir(plan.source_id) / "rms_db.npy")
        words, tmap, _ = self._window(plan, tr, rms)
        return detect_spoken([(tmap.clamp_to_clip(w.start), tmap.clamp_to_clip(w.end), w.text) for w in words])

    # ------------------------------------------------------------------ stage 4-7
    FROZEN = {"approved", "uploading", "published", "dry_run_published", "deferred_quota"}

    def _load(self, clip_id: str) -> tuple[dict[str, Any], ClipPlan, dict[str, Any], RightsRecord | None, Transcript, np.ndarray]:
        row = self.store.row("clips", clip_id)
        if row is None:
            raise PipelineError(f"no clip {clip_id}")
        if row.get("status") in self.FROZEN:
            raise PipelineError(f"clip is '{row['status']}': approved/published clips are frozen; plan a new clip instead")
        if self.store.kill_switch:
            raise PipelineError("kill switch is on")
        plan = ClipPlan.model_validate(row["plan"])
        src = self.store.row("sources", plan.source_id)
        if src is None:
            raise PipelineError("source missing")
        rec = RightsRecord.model_validate(src["rights"]) if src.get("rights") else None
        tr = Transcript.model_validate(src["transcript"])
        rms = np.load(self.analysis_dir(plan.source_id) / "rms_db.npy")
        return row, plan, src, rec, tr, rms

    @staticmethod
    def _window(plan: ClipPlan, tr: Transcript, rms: np.ndarray) -> tuple[list, TimelineMap, list[Sentence]]:
        return studio.clip_window(plan, tr, rms)

    def render_and_gate(self, clip_id: str, actor: str = "cli") -> GateReport:
        _row, plan, src, rec, tr, rms = self._load(clip_id)
        stored = self.store.find_media(src["media_sha"])
        sid = plan.source_id
        self.store.stage(sid, "edit", "running", f"{clip_id}: cutting pauses > {plan.remove_silences_longer_than}s")
        words, tmap, sents = self._window(plan, tr, rms)
        topics = self.s.channel.topics or default_topics(tr)
        ass = studio.compose_overlay(plan, words, tmap, sents, rec, self.s.channel.name, topics)
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
        md, notes, summary, extra = studio.compose_metadata(plan, tmap, sents, rec, topics, self.llm)
        self.store.stage(sid, "metadata", "done", f"{clip_id}: " + "; ".join(notes))
        fp = {"dhash": analyze.frame_dhashes(self.runner, self.store.find_media(clip_sha), self.work),
              "shingles": studio.text_fingerprint(words)}
        self.store.upsert("clips", clip_id, clip_sha=clip_sha, preview_sha=prev_sha, posters=posters, fingerprint=fp)
        return self._gate(clip_id, plan, src, rec, words, tmap, md, summary, clip_sha, fp, extra, actor, "rendered + gated")

    def regate(self, clip_id: str, title: str, description: str, actor: str) -> GateReport:
        """The owner edited the title/description on the review page: rebuild the request and gate again.

        An edited description is treated entirely as owner/model text (checked hardest), since its
        frame is no longer the one our code assembled.
        """
        row, plan, src, rec, tr, rms = self._load(clip_id)
        if not row.get("clip_sha"):
            raise PipelineError("render the clip first")
        words, tmap, _ = self._window(plan, tr, rms)
        old = Metadata.model_validate(row["metadata"])
        md = Metadata(title=title, description=description, tags=old.tags, chapters=old.chapters)  # validates limits
        summary = description if description != old.description else (row.get("summary") or "")
        self.store.audit.append(actor, "edit_metadata", clip_id, {"title": title, "description_changed": description != old.description})
        return self._gate(clip_id, plan, src, rec, words, tmap, md, summary or "", row["clip_sha"], row.get("fingerprint") or {}, [], actor, "metadata edited + re-gated")

    def _gate(self, clip_id: str, plan: ClipPlan, src: dict[str, Any], rec: RightsRecord | None, words: list, tmap: TimelineMap,
              md: Metadata, summary: str, clip_sha: str, fp: dict[str, Any], extra: list[CheckResult], actor: str, note: str) -> GateReport:
        report, body = studio.gate_clip(plan=plan, rights=rec, md=md, summary=summary, words=words, tmap=tmap, settings=self.s,
                                        media_sha256=src["media_sha"], clip_sha256=clip_sha, fingerprint=fp,
                                        history=self.history(plan, clip_id), kill_switch=self.store.kill_switch,
                                        screen_terms=load_screen_terms(self.screen_terms_path()),
                                        registry=load_registry(self.store), extra=extra)
        status = "awaiting_approval" if report.passed else "blocked"
        self.store.upsert("clips", clip_id, metadata=md.model_dump(mode="json"), summary=summary, body=body,
                          gate=report.model_dump(mode="json"), status=status, decision=None)
        sid = plan.source_id
        self.store.stage(sid, "gate", "done" if report.passed else "blocked",
                         f"{clip_id}: {len(report.blocks)} block(s), {len(report.warnings)} warning(s)")
        self.store.stage(sid, "approval", "waiting" if report.passed else "n/a", f"{clip_id}: {status}")
        self.store.audit.append(actor, "gate", clip_id, {"passed": report.passed, "blocks": [b.code for b in report.blocks],
                                                         "warnings": [w.code for w in report.warnings], "clip_sha256": clip_sha,
                                                         "metadata_sha256": report.metadata_sha256})
        self.store.clip_log(clip_id, f"{note}: {status}")
        if report.passed and maybe_auto_approve(self.store, self.s, clip_id):
            self.store.clip_log(clip_id, "auto-approved by policy (private upload)")
        return report

    def screen_terms_path(self) -> Path:
        """Your own list in the data folder overrides the bundled default (src/fairclip/data)."""
        own = self.store.root / "screen_terms.txt"
        return own if own.exists() else PACKAGE_DATA / "screen_terms.txt"

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
