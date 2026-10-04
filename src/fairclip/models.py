"""Strict data schemas shared by every pipeline stage.

Every model forbids unknown fields and bounds string lengths. Anything that crosses a
trust boundary (a rights file, a transcript, model output, a form post) is parsed into one of
these models before the pipeline touches it, so a malformed or oversized input fails loudly at
the edge instead of deep inside the pipeline.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Seconds = Annotated[float, Field(ge=0.0, le=6 * 3600.0)]
ShortText = Annotated[str, Field(max_length=200)]
LongText = Annotated[str, Field(max_length=5000)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)


def no_hidden_chars(v: str) -> str:
    """Reject control and invisible 'format' characters (e.g. U+202E right-to-left override, which can
    make 'moc.live' display as 'evil.com', or zero-width spaces that split a word past a filter)."""
    for c in v:
        if (ord(c) < 32 and c not in "\n\t") or unicodedata.category(c) == "Cf":
            raise ValueError(f"hidden/control character U+{ord(c):04X} is not allowed")
    return v


# --------------------------------------------------------------------------- rights (stage 1)


class RightsBasis(StrEnum):
    OWN_CONTENT = "own_content"  # the channel owner made it
    WRITTEN_PERMISSION = "written_permission"  # signed permission from the creator
    LICENSE = "license"  # a paid or negotiated license
    PUBLIC_DOMAIN = "public_domain"
    CREATIVE_COMMONS = "creative_commons"


class CCLicense(StrEnum):
    CC0 = "CC0-1.0"
    BY = "CC-BY-4.0"
    BY_SA = "CC-BY-SA-4.0"
    BY_NC = "CC-BY-NC-4.0"
    BY_NC_SA = "CC-BY-NC-SA-4.0"
    BY_ND = "CC-BY-ND-4.0"
    BY_NC_ND = "CC-BY-NC-ND-4.0"


class RightsRecord(Strict):
    """Proof that the channel owner may clip and publish this source. No record, no processing."""

    record_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{3,64}$")]
    basis: RightsBasis
    source_title: ShortText
    rights_holder: ShortText
    evidence: ShortText = Field(
        description="Where the proof lives: a file name, contract ID, or license URL."
    )
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    cc_license: CCLicense | None = None
    attribution: ShortText | None = None
    allows_youtube: bool = True
    allows_monetization: bool = True
    allows_modification: bool = True
    expires: date | None = None
    obtained_from_rights_holder: bool = Field(
        default=True,
        description="True when the original file came from the rights holder, not a platform rip.",
    )
    synthetic_or_altered: bool = Field(
        default=False,
        description="Source contains realistic AI-generated/altered content (cloned voice, etc.).",
    )
    notes: Annotated[str, Field(max_length=1000)] = ""

    @model_validator(mode="after")
    def _basis_consistency(self) -> RightsRecord:
        if self.basis is RightsBasis.CREATIVE_COMMONS and self.cc_license is None:
            raise ValueError("creative_commons basis requires cc_license")
        if self.cc_license and self.cc_license is not CCLicense.CC0 and not self.attribution:
            raise ValueError(f"{self.cc_license} requires an attribution line")
        return self


# --------------------------------------------------------------------------- transcript (stage 2)


class Word(Strict):
    text: Annotated[str, Field(min_length=1, max_length=64)]
    start: Seconds
    end: Seconds

    @model_validator(mode="after")
    def _order(self) -> Word:
        if self.end < self.start:
            raise ValueError("word end before start")
        return self


class Sentence(Strict):
    start: Seconds
    end: Seconds
    text: Annotated[str, Field(max_length=2000)]
    words: list[Word] = Field(max_length=500)


class Transcript(Strict):
    language: Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] = "en"
    engine: ShortText
    sentences: list[Sentence] = Field(max_length=20000)

    @property
    def words(self) -> list[Word]:
        return [w for s in self.sentences for w in s.words]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(w.text for w in self.words if w.start >= start - 1e-6 and w.end <= end + 1e-6)


# --------------------------------------------------------------------------- highlights (stage 3)


class Features(Strict):
    audio_energy_db: float  # mean RMS level in decibels relative to full scale (dBFS)
    speech_rate_wps: float  # words per second
    relevance: Annotated[float, Field(ge=0, le=1)]
    scene_density: Annotated[float, Field(ge=0, le=1)]  # normalised cuts per minute
    silence_ratio: Annotated[float, Field(ge=0, le=1)]


class Candidate(Strict):
    candidate_id: ShortText
    source_id: ShortText
    start: Seconds
    end: Seconds
    text: Annotated[str, Field(max_length=5000)]
    features: Features
    z: dict[str, float] = Field(default_factory=dict)
    score: float = 0.0
    rank: int = 0


# --------------------------------------------------------------------------- clip plan (stages 4-6)


class Author(StrEnum):
    OWNER = "owner"  # typed by the human channel owner
    AI_DRAFT = "ai_draft"  # drafted by a model; counts only once the owner approves it


class ValueAddKind(StrEnum):
    COMMENTARY = "commentary"
    CONTEXT_CARD = "context_card"
    GRAPHIC = "graphic"
    ANALYSIS = "analysis"


class TemplateKind(StrEnum):
    """How a value-add element is drawn (docs/TEMPLATES.md). Every template still carries the
    owner's own sentence in `text`; the template adds structure, never replaces the words."""

    CARD = "card"  # label + sentence (the default)
    KEY_NUMBER = "key_number"  # one big figure that counts up, with a label
    DEFINITION = "definition"  # a term, then the owner's definition
    STEPS = "steps"  # 2-5 numbered steps that appear one at a time
    COMPARE = "compare"  # two columns, A vs B
    BAR = "bar"  # 2-5 bars that grow to their values


TplText = Annotated[str, Field(min_length=1, max_length=60)]
ColText = Annotated[str, Field(min_length=1, max_length=28)]
TEMPLATE_TEXT_MAX = 140  # characters of owner sentence that fit under a template's graphic


class Bar(Strict):
    label: Annotated[str, Field(min_length=1, max_length=40)]
    value: Annotated[float, Field(ge=0.0, le=1e12)]

    @field_validator("value")
    @classmethod
    def _readable(cls, v: float) -> float:
        if 0 < v < 1e-6:
            raise ValueError("bar values below 0.000001 aren't readable on screen; change the unit instead")
        if len(fmt_value(v).replace(",", "").replace(".", "")) > 12:
            raise ValueError("bar value has too many digits to read on screen; round it or change the unit")
        return v


class TemplateData(Strict):
    headline: Annotated[str, Field(max_length=40)] = ""  # key_number: "42 m/s"; definition: the term
    items: list[TplText] = Field(default_factory=list, max_length=5)  # steps
    # Compare columns are half the panel wide, so their text is shorter.
    left_title: Annotated[str, Field(max_length=20)] = ""
    right_title: Annotated[str, Field(max_length=20)] = ""
    left: list[ColText] = Field(default_factory=list, max_length=4)
    right: list[ColText] = Field(default_factory=list, max_length=4)
    bars: list[Bar] = Field(default_factory=list, max_length=5)
    unit: Annotated[str, Field(max_length=12)] = ""

    def strings(self) -> list[str]:
        return [self.headline, *self.items, self.left_title, self.right_title, *self.left, *self.right,
                *(b.label for b in self.bars), self.unit]

    @model_validator(mode="after")
    def _clean(self) -> TemplateData:
        for s in self.strings():
            no_hidden_chars(s)
        return self


def fmt_value(v: float) -> str:
    """Bar values exactly as the owner typed them, shown on screen AND read by the gate:
    12, 3.5, 1,234.5, 0.00001. Shortest round-trip digits (repr), never exponent notation
    (the gate would read "1e-05" as the number 05), never float noise."""
    from decimal import Decimal

    d = Decimal(repr(float(v))).normalize()
    return format(d, ",f") if d != 0 else "0"


class ValueAddElement(Strict):
    kind: ValueAddKind
    text: Annotated[str, Field(min_length=1, max_length=400)]
    start: Seconds  # on the clip's own timeline (after jump cuts)
    end: Seconds
    author: Author = Author.OWNER
    owner_approved: bool = False
    template: TemplateKind = TemplateKind.CARD
    data: TemplateData | None = None

    _clean = field_validator("text")(classmethod(lambda cls, v: no_hidden_chars(v)))

    @model_validator(mode="after")
    def _order(self) -> ValueAddElement:
        if self.end <= self.start:
            raise ValueError("value-add element must have end > start")
        d, t = self.data or TemplateData(), self.template
        need = {
            TemplateKind.KEY_NUMBER: bool(d.headline) and any(ch.isdigit() for ch in d.headline),
            TemplateKind.DEFINITION: bool(d.headline),
            TemplateKind.STEPS: 2 <= len(d.items) <= 5,
            TemplateKind.COMPARE: bool(d.left_title and d.right_title and d.left and d.right),
            TemplateKind.BAR: 2 <= len(d.bars) <= 5 and max(b.value for b in d.bars) > 0,
        }
        if not need.get(t, True):
            raise ValueError(f"template '{t.value}' is missing its data (see docs/TEMPLATES.md)")
        # Layout budget: the panel holds the graphic AND your sentence (docs/TEMPLATES.md).
        if t is not TemplateKind.CARD and len(self.text) > TEMPLATE_TEXT_MAX:
            raise ValueError(f"with a template, keep your sentence to {TEMPLATE_TEXT_MAX} characters (it has {len(self.text)}); "
                             "split it across two elements")
        if t is TemplateKind.KEY_NUMBER and len(d.headline) > 14:
            raise ValueError("key_number headline: 14 characters at most (e.g. '$1.9B', '853 m')")
        if t is TemplateKind.DEFINITION and len(d.headline) > 24:
            raise ValueError("definition headline: 24 characters at most")
        if t is TemplateKind.STEPS and self.end - self.start < 1.2 * len(d.items):
            raise ValueError("steps need at least 1.2 s on screen per step")
        return self

    def visible_text(self) -> str:
        """Every word and figure a viewer can read for this element: what the gate checks."""
        if not self.data:
            return self.text
        d = self.data
        bars = [f"{b.label} {fmt_value(b.value)}{(' ' + d.unit) if d.unit else ''}" for b in d.bars]
        return "\n".join([self.text, *(s for s in d.strings() if s and s != d.unit), *bars,
                          d.unit if not d.bars and d.unit else ""]).strip()


class AffiliateLink(Strict):
    program_id: Annotated[str, Field(pattern=r"^[a-z0-9_-]{2,40}$")]
    url: Annotated[str, Field(max_length=500)]
    product: ShortText


class AffiliatePlan(Strict):
    links: list[AffiliateLink] = Field(default_factory=list, max_length=10)
    products_evaluated: list[ShortText] = Field(default_factory=list, max_length=20)
    spoken_disclosure_at: Seconds | None = None  # clip time where the owner says the disclosure
    start_card: tuple[Seconds, Seconds] | None = None  # on-screen disclosure near the start
    recommendation_cards: list[tuple[Seconds, Seconds]] = Field(default_factory=list, max_length=10)
    recommendation_times: list[Seconds] = Field(default_factory=list, max_length=10)
    disclosure_text: ShortText = "I earn a commission if you buy through these links."

    _clean = field_validator("disclosure_text")(classmethod(lambda cls, v: no_hidden_chars(v)))


class DataPoint(Strict):
    """A number shown in markets content, tied to where and when it came from."""

    label: ShortText
    value: ShortText
    source_name: ShortText
    source_url: Annotated[str, Field(max_length=500)]
    retrieved_at: datetime


class MarketsPlan(Strict):
    data_points: list[DataPoint] = Field(default_factory=list, max_length=50)
    positions_held: list[ShortText] = Field(default_factory=list, max_length=20)
    positions_disclosed: bool = False
    paid_by: ShortText | None = None  # issuer/promoter paying for this content, if any
    payment_amount: ShortText | None = None
    disclaimer_on_screen: bool = False
    disclaimer_in_description: bool = False


class ReframeMode(StrEnum):
    FIT = "fit"  # whole 16:9 frame over a blurred fill (good for slides)
    CROP = "crop"  # smart crop that follows the speaker


class ClipPlan(Strict):
    clip_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{3,64}$")]
    source_id: ShortText
    start: Seconds  # on the source timeline
    end: Seconds
    aspect: Literal["9:16", "16:9"] = "9:16"
    reframe: ReframeMode = ReframeMode.FIT
    remove_silences_longer_than: Annotated[float, Field(ge=0.2, le=5.0)] = 0.6
    hook_seconds: Annotated[float, Field(ge=0.0, le=3.0)] = 2.0
    template: ShortText = "context-card"
    value_add: list[ValueAddElement] = Field(default_factory=list, max_length=20)
    tags: list[Literal["markets", "affiliate", "sponsored"]] = Field(default_factory=list)
    affiliate: AffiliatePlan | None = None
    markets: MarketsPlan | None = None
    paid_promotion: bool = False
    sponsor: ShortText | None = None  # who paid, shown on screen and in the description
    paid_disclosure_card: tuple[Seconds, Seconds] | None = None  # on-screen "paid promotion" card (clip time)
    paid_disclosure_spoken_at: Seconds | None = None
    made_for_kids: bool = False
    synthetic_or_altered: bool = False  # e.g. an AI voice-over or a realistic generated graphic
    lower_third: ShortText = ""
    # The look (media/style.py): caption preset, brand accent, punch-in on jump cuts, progress bar.
    style: Literal["bold", "clean", "minimal"] = "bold"
    accent: Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#3DDC97"
    punch_in: bool = True
    progress_bar: bool = True

    @model_validator(mode="after")
    def _window(self) -> ClipPlan:
        if not (5.0 <= self.end - self.start <= 180.0):
            raise ValueError("clip length must be 5-180 s (Shorts cap is 3 minutes)")
        from .media.style import accent_problem

        if problem := accent_problem(self.accent):
            raise ValueError(problem)
        return self


# --------------------------------------------------------------------------- metadata (stage 6)


class Chapter(Strict):
    at: Seconds
    title: Annotated[str, Field(min_length=1, max_length=100)]


class Metadata(Strict):
    title: Annotated[str, Field(min_length=3, max_length=100)]  # YouTube rejects > 100 chars
    description: Annotated[str, Field(max_length=5000)]  # YouTube rejects > 5000 bytes (checked too)
    tags: list[Annotated[str, Field(max_length=30)]] = Field(default_factory=list, max_length=15)
    chapters: list[Chapter] = Field(default_factory=list, max_length=20)

    @field_validator("title", "description")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        no_hidden_chars(v)
        if "<" in v or ">" in v:  # YouTube rejects angle brackets in titles/descriptions
            raise ValueError("angle brackets are not allowed")
        return v

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        for t in v:
            no_hidden_chars(t)
        return v


# --------------------------------------------------------------------------- gate + approval


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    PASS = "pass"  # noqa: S105 - a severity, not a password


class CheckResult(Strict):
    code: ShortText
    check: ShortText
    severity: Severity
    message: Annotated[str, Field(max_length=1000)]


class GateReport(Strict):
    clip_id: ShortText
    passed: bool
    results: list[CheckResult]
    clip_sha256: ShortText
    metadata_sha256: ShortText
    created_at: datetime

    @property
    def blocks(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity is Severity.WARN]


class Visibility(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"  # immediately public after upload (still only after human approval)
    SCHEDULED_PUBLIC = "scheduled_public"  # private + publishAt; YouTube flips it to public


class Decision(Strict):
    clip_id: ShortText
    approved: bool
    reviewer: ShortText
    reason: Annotated[str, Field(max_length=1000)] = ""
    visibility: Visibility = Visibility.PRIVATE
    publish_at: datetime | None = None
    warnings_acknowledged: bool = False

    @model_validator(mode="after")
    def _reason(self) -> Decision:
        if not self.approved and len(self.reason.strip()) < 3:
            raise ValueError("a rejection needs a reason")
        if self.visibility is Visibility.SCHEDULED_PUBLIC and self.publish_at is None:
            raise ValueError("scheduled_public needs publish_at")
        return self
