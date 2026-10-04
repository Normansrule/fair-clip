"""Studio: the few-buttons flow, shared by the app and the browser build.

    1 · Add a video   →  rights choice + captions/transcript  →  find_clips()
    2 · Write your take  (one sentence per clip; the judgment stays yours)  →  build_plan()
    3 · Make & download  →  render_args() for FFmpeg  →  finish() (metadata + full gate)
                          →  upload_package() (titles, descriptions, checklist, CSV)

Every function here is pure: no database, no subprocess, no network. The app's Pipeline calls
compose_overlay / compose_metadata / gate_clip for its own renders, and the browser build runs this
same module under Pyodide with FFmpeg compiled to WebAssembly, so a clip made in the browser gets
exactly the same look, metadata rules and compliance gate as one made in the app.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import Field

from . import rights as rights_mod
from .config import Settings
from .gate import GateContext, History, load_screen_terms, run_gate, shingles
from .media import overlay
from .media.analyze import AudioSignal
from .media.edit import clip_args
from .media.style import DEFAULT_ACCENT, accent_problem
from .media.timeline import TimelineMap, keep_intervals
from .metadata import build_metadata, pick_hook, shorten
from .models import (
    Candidate,
    CCLicense,
    CheckResult,
    ClipPlan,
    GateReport,
    Metadata,
    RightsBasis,
    RightsRecord,
    Sentence,
    Severity,
    ShortText,
    Strict,
    TemplateKind,
    Transcript,
    Word,
)
from .publish.body import build_body
from .scoring import default_topics, rank_candidates, tokens

# ----------------------------------------------------------------------------- shared steps


def clip_window(plan: ClipPlan, tr: Transcript, rms: np.ndarray) -> tuple[list[Word], TimelineMap, list[Sentence]]:
    """The clip's words, its jump-cut timeline and its sentences (all on the SOURCE timeline)."""
    words = [w for w in tr.words if w.start >= plan.start - 1e-6 and w.end <= plan.end + 1e-6]
    tmap = TimelineMap(keep_intervals(tr.words, plan.start, plan.end, plan.remove_silences_longer_than, rms))
    sents = [s for s in tr.sentences if s.start >= plan.start - 1e-6 and s.end <= plan.end + 1e-6]
    return words, tmap, sents


def compose_overlay(plan: ClipPlan, words: list[Word], tmap: TimelineMap, sents: list[Sentence],
                    rights: RightsRecord | None, channel: str, topics: list[str]) -> str:
    """The ASS file: hook (a verbatim quote), captions, value-add, disclosures, credit."""
    hook = pick_hook(sents, topics)
    credit = rights_mod.attribution_line(rights, short=True) if rights else ""
    return overlay.build_ass(plan, words, tmap, hook_text=f"“{shorten(hook, 72)}”" if hook else "",
                             channel=channel, attribution=credit)


def compose_metadata(plan: ClipPlan, tmap: TimelineMap, sents: list[Sentence], rights: RightsRecord | None,
                     topics: list[str], llm: Any = None) -> tuple[Metadata, list[str], str, list[CheckResult]]:
    """Title, description (disclosures first), tags and chapters on the CLIP's own timeline."""
    from pydantic import ValidationError

    mapped = [Sentence(start=tmap.clamp_to_clip(s.start), end=tmap.clamp_to_clip(s.end), text=s.text, words=[]) for s in sents]
    plan_md = plan.model_copy(update={"start": 0.0, "end": max(tmap.duration, 5.0)})
    attribution = rights_mod.attribution_line(rights) if rights else ""
    extra: list[CheckResult] = []
    try:
        if rights:
            md, notes, summary = build_metadata(plan_md, mapped, rights, attribution, topics, llm)
            return md, notes, summary, extra
    except ValidationError as exc:
        extra.append(CheckResult(code="METADATA_LIMITS", check="Metadata", severity=Severity.BLOCK,
                                 message="metadata failed YouTube limits — fix: shorten title/description"))
        return Metadata(title="(missing)", description=""), [f"metadata invalid: {exc.error_count()} errors"], "", extra
    return Metadata(title="(missing)", description=""), [], "", extra


def text_fingerprint(words: list[Word]) -> list[str]:
    return sorted(hashlib.sha1(s.encode()).hexdigest()[:12] for s in shingles(" ".join(w.text for w in words), 5))  # noqa: S324


def gate_clip(*, plan: ClipPlan, rights: RightsRecord | None, md: Metadata, summary: str, words: list[Word],
              tmap: TimelineMap, settings: Settings, media_sha256: str, clip_sha256: str, fingerprint: dict[str, Any],
              history: History, kill_switch: bool, screen_terms: dict[str, list[str]], registry: Any = None,
              extra: list[CheckResult] | None = None) -> tuple[GateReport, dict[str, Any]]:
    """Run the full compliance gate on a rendered clip. Returns (report, exact upload request body)."""
    body = build_body(md, plan, rights) if rights else {"snippet": {}, "status": {}}
    ctx = GateContext(plan=plan, rights=rights, metadata=md, body=body, transcript_text=" ".join(w.text for w in words),
                      clip_duration=tmap.duration, fingerprint=fingerprint, settings=settings, history=history,
                      kill_switch=kill_switch, screen_terms=screen_terms, media_sha256=media_sha256, summary=summary,
                      registry=registry, words=[(tmap.clamp_to_clip(w.start), tmap.clamp_to_clip(w.end), w.text) for w in words])
    report = run_gate(ctx, clip_sha256)
    if extra:
        report = report.model_copy(update={"results": list(extra) + report.results, "passed": False})
    return report, body


def render_args(plan: ClipPlan, tmap: TimelineMap, *, src_name: str, ass_name: str = "overlay.ass",
                crop_cx: float = 0.5, quality: str = "high", out_name: str = "clip.mp4") -> list[str]:
    """FFmpeg arguments (the same builder the app's renderer uses: media.edit.clip_args)."""
    return clip_args(plan, tmap, src_name=src_name, ass_name=ass_name, crop_cx=crop_cx, quality=quality, out_name=out_name)


# ----------------------------------------------------------------------------- studio inputs


class Brand(Strict):
    channel: Annotated[str, Field(min_length=1, max_length=60)]
    accent: Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")] = DEFAULT_ACCENT
    style: Literal["bold", "clean", "minimal"] = "bold"
    aspect: Literal["9:16", "16:9"] = "9:16"
    reframe: Literal["fit", "crop"] = "fit"  # fit: whole frame over a blurred fill (slides); crop: fill the screen
    topics: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=30)
    clips: Annotated[int, Field(ge=1, le=8)] = 3
    min_s: Annotated[float, Field(ge=10, le=120)] = 20
    max_s: Annotated[float, Field(ge=15, le=180)] = 55


class RightsChoice(Strict):
    """The owner's answer to "where is this video from?" (step 1), turned into a rights record."""

    basis: RightsBasis
    title: ShortText = Field(min_length=1)
    holder: ShortText = Field(min_length=1)
    evidence: ShortText = ""  # required for permission/license: where the proof lives
    cc_license: CCLicense | None = None
    attribution: ShortText | None = None
    synthetic: bool = False  # realistic AI voice/visuals in the source
    obtained_from_rights_holder: bool = True
    allows_monetization: bool = True


def rights_record(choice: RightsChoice, media_sha256: str) -> RightsRecord:
    if choice.basis in (RightsBasis.WRITTEN_PERMISSION, RightsBasis.LICENSE) and not choice.evidence.strip():
        raise ValueError("a permission or license needs evidence: say where the signed permission or license is kept")
    evidence = choice.evidence or ("own recording, declared in fair-clip Studio" if choice.basis is RightsBasis.OWN_CONTENT else "")
    return RightsRecord.model_validate({
        "record_id": f"studio-{media_sha256[:12]}", "basis": choice.basis, "source_title": choice.title,
        "rights_holder": choice.holder, "evidence": evidence, "source_sha256": media_sha256,
        "cc_license": choice.cc_license, "attribution": choice.attribution, "synthetic_or_altered": choice.synthetic,
        "obtained_from_rights_holder": choice.obtained_from_rights_holder, "allows_monetization": choice.allows_monetization,
    })


def rights_check(rec: RightsRecord, monetized: bool = True) -> list[str]:
    """Problems that stop the Studio at step 1 (empty = go ahead)."""
    return [m for _c, m in rights_mod.rights_problems(rec, media_sha256=rec.source_sha256, monetized=monetized)]


def find_clips(source_id: str, tr: Transcript, rms_db: np.ndarray, cuts: list[float], brand: Brand,
               weights: dict[str, float] | None = None) -> list[Candidate]:
    """The best `brand.clips` moments (Equation 1), never overlapping by more than 30 %."""
    from .config import ScoringConfig

    topics = brand.topics or default_topics(tr)
    w = weights or ScoringConfig().weights
    return rank_candidates(source_id, tr, AudioSignal(np.asarray(rms_db, dtype=float)), cuts, topics, w,
                           min_s=brand.min_s, max_s=max(brand.max_s, brand.min_s + 5), k=brand.clips)


_FIGURE = re.compile(r"(?<![\w.])(?:[$€£]\s?)?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|km|m|metres|meters|kg|tonnes|tons|years|°C|°F|x|times|million|billion)?(?![\w])", re.I)
_STEPS = re.compile(r"\b(first|second|third|then|next|finally|lastly)\b", re.I)


def suggest_take(c: Candidate) -> dict[str, Any]:
    """A STARTING POINT for step 2, never the value-add itself: which template might fit and a
    question to answer. Facts it pre-fills (a figure) are copied from what is SAID in the clip;
    the sentence is always left for the owner to write."""
    figures = [m.group(0).strip().rstrip(",.") for m in _FIGURE.finditer(c.text) if len(m.group(0).strip()) <= 14 and any(ch.isdigit() for ch in m.group(0))]
    figures = [f for f in figures if not re.fullmatch(r"(19|20)\d\d", f)]  # years are context, not a key number
    if figures:
        return {"template": TemplateKind.KEY_NUMBER.value, "data": {"headline": figures[0]},
                "prompt": f"The clip says {figures[0]}. What does that number mean for your viewers? Put it in perspective in one sentence."}
    if len({m.lower() for m in _STEPS.findall(c.text)}) >= 3:  # "first … then … finally"
        return {"template": TemplateKind.CARD.value, "data": None,
                "prompt": "This moment walks through a process. In one sentence, what's the step people usually get wrong?"}
    return {"template": TemplateKind.CARD.value, "data": None,
            "prompt": "In one or two sentences: what should viewers take away, that the clip itself doesn't say? Context, a consequence, or your own view."}


def candidates_json(cands: list[Candidate], tr: Transcript, brand: Brand) -> list[dict[str, Any]]:
    """What step 2 shows for each moment (the same for the browser and the app)."""
    topics = brand.topics or default_topics(tr)
    out = []
    for i, c in enumerate(cands):
        sents = [s for s in tr.sentences if s.start >= c.start - 1e-6 and s.end <= c.end + 1e-6]
        out.append({"i": i, "start": round(c.start, 2), "end": round(c.end, 2), "text": c.text[:1200],
                    "hook": pick_hook(sents, topics), "suggestion": suggest_take(c)})
    return out


def take_from_json(take: dict[str, Any]) -> Take:
    """A Take from what the page sent (unknown keys ignored, everything validated)."""
    return Take.model_validate({k: take.get(k) for k in ("text", "template", "data", "kind") if take.get(k) is not None})


class Take(Strict):
    """The owner's value-add for one clip (step 2)."""

    text: Annotated[str, Field(max_length=400)] = ""
    template: TemplateKind = TemplateKind.CARD
    data: dict[str, Any] | None = None
    kind: Literal["commentary", "context_card", "graphic", "analysis"] = "context_card"
    include: bool = True


def build_plan(c: Candidate, take: Take, brand: Brand, n: int) -> ClipPlan:
    """A clip plan from a candidate and the owner's take. The take is on screen from just after
    the hook for about half the clip (the gate needs at least 25 %)."""
    if problem := accent_problem(brand.accent):
        raise ValueError(problem)
    length = c.end - c.start
    va = []
    if take.text.strip():
        start = 2.2
        end = min(length - 1.0, start + max(8.0, 0.5 * length))
        va.append({"kind": take.kind, "text": take.text.strip(), "start": start, "end": end, "author": "owner",
                   "template": take.template.value, "data": take.data if take.template is not TemplateKind.CARD else None})
    return ClipPlan.model_validate({
        "clip_id": f"clip-{n:02d}-{hashlib.sha1(c.candidate_id.encode()).hexdigest()[:6]}",  # noqa: S324 - an id, not security
        "source_id": c.source_id, "start": c.start, "end": c.end, "aspect": brand.aspect, "reframe": brand.reframe,
        "template": f"studio-{take.template.value}", "value_add": va, "style": brand.style, "accent": brand.accent,
        "lower_third": brand.channel,
    })


# ----------------------------------------------------------------------------- finish + package


@dataclass
class Finished:
    plan: ClipPlan
    metadata: Metadata
    report: GateReport
    body: dict[str, Any]
    duration: float
    filename: str
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.report.passed

    def to_json(self) -> dict[str, Any]:
        return {"clip_id": self.plan.clip_id, "filename": self.filename, "duration": round(self.duration, 2),
                "title": self.metadata.title, "description": self.metadata.description, "tags": self.metadata.tags,
                "passed": self.passed, "blocks": [r.model_dump(mode="json") for r in self.report.blocks],
                "warnings": [r.model_dump(mode="json") for r in self.report.warnings],
                "checklist": studio_checklist(self.body, self.plan)}


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", text.lower())).strip("-")
    return (s[:limit].rstrip("-") or "clip")


def finish(*, plan: ClipPlan, n: int, tr: Transcript, rms: np.ndarray, rights: RightsRecord, brand: Brand,
           settings: Settings, screen_terms: dict[str, list[str]], clip_sha256: str, history: History | None = None,
           fingerprint: dict[str, Any] | None = None, llm: Any = None, title: str = "") -> Finished:
    """After the clip is rendered: metadata (the owner's title if they wrote one), then the full
    compliance gate. Whatever the owner edits is gated exactly like what fair-clip proposed."""
    words, tmap, sents = clip_window(plan, tr, rms)
    topics = brand.topics or default_topics(tr)
    md, notes, summary, extra = compose_metadata(plan, tmap, sents, rights, topics, llm)
    if not extra:
        md, problem = _polish(md, title, brand, plan, " ".join(w.text for w in words))
        if problem:
            extra.append(CheckResult(code="METADATA_LIMITS", check="Metadata", severity=Severity.BLOCK, message=problem))
    fp = fingerprint or {"dhash": [], "shingles": text_fingerprint(words)}
    report, body = gate_clip(plan=plan, rights=rights, md=md, summary=summary, words=words, tmap=tmap, settings=settings,
                             media_sha256=rights.source_sha256 or "", clip_sha256=clip_sha256, fingerprint=fp,
                             history=history or History(), kill_switch=False, screen_terms=screen_terms, extra=extra)
    return Finished(plan, md, report, body, tmap.duration, f"{n:02d}-{slug(md.title)}.mp4", notes)


def hashtags(brand: Brand, vertical: bool, clip_text: str = "") -> str:
    """Up to three hashtags from the owner's topics (letters and digits only), plus #Shorts for
    vertical clips; topics this clip actually talks about come first. YouTube shows the first
    three above the title."""
    from .gate import stem

    stems = {stem(t) for t in tokens(clip_text)}
    ranked = sorted(brand.topics, key=lambda t: not (tokens(t) and all(stem(x) in stems for x in tokens(t))))
    tags = ["Shorts"] if vertical else []
    for t in ranked:
        tag = re.sub(r"[^0-9A-Za-z]", "", t.title())
        if tag and tag.lower() not in {x.lower() for x in tags}:
            tags.append(tag[:30])
        if len(tags) >= 3:
            break
    return " ".join(f"#{t}" for t in tags)


def _polish(md: Metadata, title: str, brand: Brand, plan: ClipPlan, clip_text: str = "") -> tuple[Metadata, str]:
    from pydantic import ValidationError

    upd: dict[str, Any] = {}
    if title.strip():
        upd["title"] = re.sub(r"\s+", " ", title).strip()
    if tags := hashtags(brand, plan.aspect == "9:16", clip_text):
        upd["description"] = md.description + "\n\n" + tags
    try:
        return Metadata.model_validate(md.model_dump() | upd), ""
    except ValidationError as exc:
        what = "title" if "title" in str(exc) else "description"
        return md, f"your {what} breaks YouTube's rules ({exc.errors()[0]['msg']}) — fix: edit the {what}"


def title_problems(title: str, plan: ClipPlan, tr: Transcript, rms: np.ndarray, channel: str) -> list[str]:
    """Live feedback while the owner types a title: the gate's grounding rule, in words. A title
    may only use words and numbers that are SAID in the clip or written in the owner's own take."""
    from .gate import numbers, ungrounded_words

    words, _tmap, _s = clip_window(plan, tr, rms)
    said = " ".join(w.text for w in words)
    va = " ".join(v.visible_text() for v in plan.value_add)
    allowed = set(tokens(said)) | set(tokens(va)) | set(tokens(channel))
    out = []
    tt = tokens(title)
    bad = ungrounded_words(title, allowed)
    if tt and (len(tt) - len(bad)) / len(tt) < 0.7:
        out.append("not said in the clip or your take: " + ", ".join(dict.fromkeys(bad)))
    if extra := sorted(numbers(title, skip_years=False) - numbers(said, skip_years=False) - numbers(va, skip_years=False)):
        out.append("numbers never said: " + ", ".join(extra))
    return out


def studio_settings(brand: Brand, monetized: bool = True) -> Settings:
    s = Settings()
    s.channel.name = brand.channel
    s.channel.topics = list(brand.topics)
    s.channel.monetized = monetized
    return s


def default_screen_terms() -> dict[str, list[str]]:
    from pathlib import Path

    return load_screen_terms(Path(__file__).resolve().parent / "data" / "screen_terms.txt")


def studio_checklist(body: dict[str, Any], plan: ClipPlan) -> list[str]:
    """What to set by hand when uploading in YouTube Studio (a file download carries no flags)."""
    st = body.get("status", {})
    out = [
        "Altered or synthetic content: " + ("YES (this clip contains realistic altered or synthetic media)" if st.get("containsSyntheticMedia") else "No"),
        "Made for kids: " + ("Yes" if st.get("selfDeclaredMadeForKids") else "No"),
        "Paid promotion: " + ("YES, tick 'includes paid promotion'" if plan.paid_promotion or "sponsored" in plan.tags else "No"),
        "Visibility: upload as Private first, watch it once on your phone, then publish.",
    ]
    return out


def clip_text(f: Finished) -> str:
    lines = ["TITLE", f.metadata.title, "", "DESCRIPTION", f.metadata.description, ""]
    if f.metadata.tags:
        lines += ["TAGS", ", ".join(f.metadata.tags), ""]
    lines += ["YOUTUBE STUDIO CHECKLIST", *(f"[ ] {c}" for c in studio_checklist(f.body, f.plan))]
    return "\n".join(lines) + "\n"


def upload_package(items: list[Finished], brand: Brand) -> dict[str, str]:
    """The text side of the download: one .txt per clip, all titles+descriptions in one file, a CSV
    for spreadsheets, and a README. Clips the gate blocked are listed with their reasons, not exported."""
    ok = [f for f in items if f.passed]
    files: dict[str, str] = {}
    for f in ok:
        files[f.filename.removesuffix(".mp4") + ".txt"] = clip_text(f)
    sep = "\n" + "=" * 72 + "\n"
    files["ALL-TITLES-AND-DESCRIPTIONS.txt"] = sep.join(f"{f.filename}\n\n{clip_text(f)}" for f in ok) or "No clips passed the gate.\n"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file", "title", "description", "tags", "altered_or_synthetic", "made_for_kids", "paid_promotion"])
    for f in ok:
        st = f.body.get("status", {})
        w.writerow([f.filename, f.metadata.title, f.metadata.description, ", ".join(f.metadata.tags),
                    "yes" if st.get("containsSyntheticMedia") else "no", "yes" if st.get("selfDeclaredMadeForKids") else "no",
                    "yes" if f.plan.paid_promotion else "no"])
    files["upload-sheet.csv"] = buf.getvalue()
    blocked = [f for f in items if not f.passed]
    readme = [f"{brand.channel}: {len(ok)} clip(s) ready, made with fair-clip.", "",
              "For each clip:",
              "  1. YouTube Studio → Create → Upload videos → pick the .mp4.",
              "  2. Copy the TITLE and DESCRIPTION from the clip's .txt file (or ALL-TITLES-AND-DESCRIPTIONS.txt).",
              "  3. Work through the YOUTUBE STUDIO CHECKLIST in that file (the altered-content and paid-promotion",
              "     boxes can only be ticked in Studio).",
              "  4. Upload as Private, watch it once, then publish.", ""]
    if blocked:
        readme.append("Not exported (the compliance gate blocked them):")
        for f in blocked:
            readme.append(f"  - {f.plan.clip_id}: " + "; ".join(f"{r.code}: {r.message}" for r in f.report.blocks))
    files["README.txt"] = "\n".join(readme) + "\n"
    return files
