"""Stage 7: the compliance gate. Automated, blocking, and deliberately boring.

Every check is a small pure function over a GateContext. `run_gate` runs them all (it doesn't
stop at the first failure, so the reviewer sees everything at once) and returns a GateReport.
A clip with any BLOCK result can't reach human approval, let alone YouTube.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..affiliate import (
    PLAIN_DISCLOSURE,
    TERMS_STALE_DAYS,
    AffiliateRegistry,
    detect_spoken,
    says_paid,
)
from ..config import Settings
from ..links import check_links, find_links
from ..llm import injection_signals
from ..media.analyze import hamming
from ..metadata import MARKETS_DISCLAIMER
from ..models import (
    AffiliatePlan,
    Author,
    CheckResult,
    ClipPlan,
    GateReport,
    Metadata,
    RightsRecord,
    Severity,
)
from ..publish.body import body_hash, paid_promotion_required, synthetic_required
from ..rights import rights_problems
from ..scoring import tokens
from .reasons import REASONS

AFFILIATE_START_WINDOW = 10.0  # s: "near the start"
RECOMMENDATION_TOLERANCE = 3.0  # s: card must be on screen within ±3 s of the recommendation
MIN_CARD_SECONDS = 2.0  # a disclosure card shorter than this can't be read
# Links our own code adds (license deeds for attribution); everything else must be allowlisted.
SYSTEM_LINK = re.compile(r"^https://creativecommons\.org/(?:licenses|publicdomain)/[a-z0-9.\-/]+$")


@dataclass
class History:
    """What this channel already published (from the local store), newest first."""

    titles: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    fingerprints: list[dict[str, Any]] = field(default_factory=list)
    today_count: int = 0
    today_same_source: int = 0


@dataclass
class GateContext:
    plan: ClipPlan
    rights: RightsRecord | None
    metadata: Metadata
    body: dict[str, Any]
    transcript_text: str
    clip_duration: float
    fingerprint: dict[str, Any]
    settings: Settings
    history: History
    kill_switch: bool
    screen_terms: dict[str, list[str]]
    media_sha256: str
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    summary: str = ""  # the model/extractive part of the description (the rest is code-assembled)
    words: list[tuple[float, float, str]] = field(default_factory=list)  # (start, end, text) on the clip timeline
    registry: AffiliateRegistry | None = None  # the owner's affiliate registry (None: plan-only checks, e.g. unit tests)

    def allowlist(self) -> list[str]:
        """Config allowlist, plus registered links of active programs, but ONLY those this clip's
        affiliate plan lists (so a registered link can't ride along in a clip with no disclosures)."""
        planned = {lk.url for lk in self.plan.affiliate.links} if self.plan.affiliate else set()
        usable = set(self.registry.usable_links()) if self.registry else set()
        return list(self.settings.link_allowlist) + sorted(planned & usable)

    def owner_text(self) -> str:
        """Everything a person or model wrote: title, summary, tags, value-add. Checked hardest."""
        return "\n".join([self.metadata.title, self.summary, " ".join(self.metadata.tags), *(v.visible_text() for v in self.plan.value_add)])

    def said_between(self, a: float, b: float) -> str:
        return " ".join(t for s, e, t in self.words if e >= a and s <= b)


def _r(code: str, message: str = "") -> CheckResult:
    group, sev, name, fix = REASONS[code]
    return CheckResult(code=code, check=group, severity=Severity(sev), message=(message or name) + f" — fix: {fix}")


def _ok(group: str, message: str) -> CheckResult:
    return CheckResult(code="OK", check=group, severity=Severity.PASS, message=message)


def shingles(text: str, n: int = 4) -> set[str]:
    t = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(t[i : i + n]) for i in range(max(0, len(t) - n + 1))}


def union_len(intervals: list[tuple[float, float]]) -> float:
    total, cur_a, cur_b = 0.0, None, None
    for a, b in sorted(intervals):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a  # type: ignore[operator]
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a  # type: ignore[operator]
    return total


# ----------------------------------------------------------------------------------- checks


def check_safety(c: GateContext) -> list[CheckResult]:
    return [_r("KILL_SWITCH_ON")] if c.kill_switch else [_ok("Safety", "kill switch off")]


def check_rights(c: GateContext) -> list[CheckResult]:
    if c.rights is None:
        return [_r("RIGHTS_MISSING")]
    probs = rights_problems(c.rights, media_sha256=c.media_sha256, monetized=c.settings.channel.monetized, today=c.now.date())
    out = [_r(code, msg) for code, msg in probs]
    if not c.rights.obtained_from_rights_holder:
        out.append(_r("RIGHTS_PLATFORM_COPY"))
    if not probs:
        out.append(_ok("Rights", f"rights record {c.rights.record_id} ({c.rights.basis.value}) covers this use"))
    return out


def check_value_add(c: GateContext) -> list[CheckResult]:
    g = c.settings.gate
    out: list[CheckResult] = []
    counted = [v for v in c.plan.value_add if v.author is Author.OWNER or v.owner_approved]
    for v in c.plan.value_add:
        if v.author is Author.AI_DRAFT and not v.owner_approved:
            out.append(_r("AI_DRAFT_UNAPPROVED", f"AI draft '{v.text[:40]}…' is not approved, so it doesn't count"))
    if not counted:
        return out + [_r("VALUE_ADD_MISSING")]
    D = max(c.clip_duration, 1e-6)
    cover = union_len([(max(0, v.start), min(D, v.end)) for v in counted if v.end > 0 and v.start < D]) / D
    if cover < g.min_value_add_ratio:
        out.append(_r("VALUE_ADD_INSUFFICIENT", f"value-add on screen for {cover:.0%} of the clip; minimum is {g.min_value_add_ratio:.0%}"))
    nwords = sum(len(v.text.split()) for v in counted)
    if nwords < g.min_value_add_words:
        out.append(_r("VALUE_ADD_THIN", f"{nwords} words of value-add; minimum is {g.min_value_add_words}"))
    tr = shingles(c.transcript_text)
    va = shingles(" ".join(v.text for v in counted))
    copied = len(va & tr) / len(va) if va else 0.0
    if copied > g.max_value_add_transcript_overlap:
        out.append(_r("VALUE_ADD_NOT_ORIGINAL", f"{copied:.0%} of the value-add phrases are copied from the transcript"))
    if not any(r.severity is Severity.BLOCK for r in out):
        out.append(_ok("Value-add", f"{len(counted)} element(s), {cover:.0%} coverage, {nwords} words, {copied:.0%} copied"))
    return out


def _card_ok(card: tuple[float, float] | None, duration: float, start_by: float) -> bool:
    """A card counts if it starts in time and is readable (>= MIN_CARD_SECONDS) inside the clip."""
    if card is None:
        return False
    a, b = max(0.0, card[0]), min(duration, card[1])
    return a <= start_by and b - a >= MIN_CARD_SECONDS


PAID_SPOKEN = re.compile(r"\b(?:sponsored|paid promotion|paid partnership|paid (?:me|us) to|thanks to \w+ for sponsoring)\b", re.I)


SPOKEN_TOLERANCE = 1.5  # s: the phrase must start this close to the time the plan claims


def _spoken(c: GateContext, at: float | None, rx: re.Pattern[str]) -> bool:
    """A confirmed spoken disclosure counts only if the timed transcript has the phrase starting
    within ±1.5 s of the claimed time AND inside the first 10 s. Denials ("not sponsored") don't count."""
    if at is None or at > AFFILIATE_START_WINDOW:
        return False
    if c.words:
        return any(t <= AFFILIATE_START_WINDOW and abs(t - at) <= SPOKEN_TOLERANCE for t, _ in detect_spoken(c.words, rx))
    return says_paid(c.transcript_text, rx)  # no word timings available (e.g. unit tests)


def check_disclosures(c: GateContext) -> list[CheckResult]:
    out = []
    st = c.body.get("status", {})
    if c.rights and synthetic_required(c.plan, c.rights) and st.get("containsSyntheticMedia") is not True:
        out.append(_r("SYNTHETIC_DISCLOSURE_MISSING"))
    if paid_promotion_required(c.plan):
        out.append(_r("PAID_PROMOTION_STUDIO_STEP"))
        if "includes paid promotion" not in c.metadata.description.lower() and not (c.plan.markets and c.plan.markets.paid_by):
            out.append(_r("PAID_PROMOTION_DESCRIPTION_MISSING"))
        # The FTC wants the disclosure IN the video, not only in the description or a platform checkbox.
        shown = _card_ok(c.plan.paid_disclosure_card, c.clip_duration, AFFILIATE_START_WINDOW)
        if not (shown or _spoken(c, c.plan.paid_disclosure_spoken_at, PAID_SPOKEN)):
            out.append(_r("PAID_PROMOTION_INVIDEO_MISSING"))
    if not any(r.severity is Severity.BLOCK for r in out):
        flags = f"synthetic={st.get('containsSyntheticMedia')}, kids={st.get('selfDeclaredMadeForKids')}, paid={paid_promotion_required(c.plan)}"
        out.append(_ok("Disclosures", f"upload flags consistent ({flags})"))
    return out


FABRICATED = [
    r"only \d+ (?:left|remaining|in stock)", r"selling out", r"almost gone", r"limited time", r"last chance",
    r"(?:ends|expires) (?:today|tonight|soon)", r"guaranteed (?:results?|to work)", r"\d+% off (?:today|now) only",
    r"customers? (?:say|love|rave)", r"(?:\d+|thousands of) (?:5|five)[- ]star reviews?", r"doctors? (?:hate|recommend)",
    r"clinically proven", r"lose \d+ ?(?:lbs|pounds|kg) in", r"testimonial",
]
_FAB = [re.compile(p, re.I) for p in FABRICATED]


def check_affiliate(c: GateContext) -> list[CheckResult]:
    p = c.plan
    if "affiliate" not in p.tags and p.affiliate is None:
        return []
    if p.affiliate is None:
        return [_r("AFFILIATE_PLAN_MISSING")]
    af, out = p.affiliate, []
    D = c.clip_duration
    spoken = _spoken(c, af.spoken_disclosure_at, PLAIN_DISCLOSURE)
    shown = _card_ok(af.start_card, D, AFFILIATE_START_WINDOW)
    heard = detect_spoken(c.words) if c.words else []
    early = [h for h in heard if h[0] <= AFFILIATE_START_WINDOW]
    if af.spoken_disclosure_at is not None and not spoken and c.words:
        if af.spoken_disclosure_at > AFFILIATE_START_WINDOW:
            where = " (that's after the first 10 s)"
        elif heard:
            where = "; the transcript has it at " + ", ".join(f"{t:.1f} s" for t, _ in heard[:3])
        else:
            where = "; the transcript has no disclosure phrase"
        out.append(_r("AFFILIATE_SPOKEN_NOT_HEARD", f"plan says you say it at {af.spoken_disclosure_at:.1f} s{where}"))
    if spoken:
        out.append(_ok("Affiliate", f"spoken disclosure confirmed at {af.spoken_disclosure_at:.1f} s and found in the timed transcript"))
    elif af.spoken_disclosure_at is None and early:
        # Never counted automatically: a guest, a co-host or the source speaker may be the one
        # talking, and the words might be about something else. The owner confirms it.
        out.append(_r("AFFILIATE_SPOKEN_UNCONFIRMED", f"'{early[0][1]}' is said at {early[0][0]:.1f} s; confirm it in the plan if it's your disclosure"))
    elif af.spoken_disclosure_at is None and heard and not shown:
        out.append(_r("AFFILIATE_SPOKEN_TOO_LATE", f"first said at {heard[0][0]:.1f} s"))
    if not (spoken or shown):
        out.append(_r("AFFILIATE_START_DISCLOSURE_MISSING"))
    elif not (spoken and shown):
        out.append(_r("AFFILIATE_SINGLE_CHANNEL"))
    if af.links and not af.recommendation_times:
        out.append(_r("AFFILIATE_RECOMMENDATION_UNMARKED"))
    cards = [(max(0.0, a), min(D, b)) for a, b in af.recommendation_cards]
    cards = [(a, b) for a, b in cards if b - a >= MIN_CARD_SECONDS]
    for t in af.recommendation_times:
        if not any(a - RECOMMENDATION_TOLERANCE <= t <= b + RECOMMENDATION_TOLERANCE for a, b in cards):
            out.append(_r("AFFILIATE_CARD_MISSING", f"no readable (≥{MIN_CARD_SECONDS:.0f} s) disclosure card within ±{RECOMMENDATION_TOLERANCE:.0f} s of the recommendation at {t:.1f} s"))
    desc = c.metadata.description
    links = find_links(desc)
    if links:
        first = desc.find(links[0])
        pos = desc.find(af.disclosure_text)
        if pos < 0 or pos > first:
            out.append(_r("AFFILIATE_DESCRIPTION_ORDER"))
    if not says_paid(af.disclosure_text):
        out.append(_r("AFFILIATE_WEAK_WORDING", f"'{af.disclosure_text}' doesn't say you get paid"))
    if c.registry is None:  # plan-only mode (no registry loaded)
        evaluated = {x.lower() for x in af.products_evaluated}
        for link in af.links:
            if link.product.lower() not in evaluated:
                out.append(_r("AFFILIATE_UNEVALUATED_PRODUCT", f"'{link.product}' is not in products_evaluated"))
    else:
        out += _check_registry(c, af)
    owner_text = c.owner_text() + "\n" + desc
    for rx in _FAB:
        m = rx.search(owner_text)
        if m:
            out.append(_r("AFFILIATE_FABRICATED_CLAIM", f"'{m.group(0)}'"))
    if p.made_for_kids:
        out.append(_r("AFFILIATE_KIDS"))
    if not any(r.severity is Severity.BLOCK for r in out):
        out.append(_ok("Affiliate", "disclosures present at start, near recommendations and above the first link"))
    return out


def _check_registry(c: GateContext, af: AffiliatePlan) -> list[CheckResult]:
    """Each link must be one you registered, under a program you joined (active, terms re-read
    recently), for a product you evaluated; the program's required wording must be in the disclosure."""
    reg, out, warned = c.registry, [], set()
    assert reg is not None
    today = c.now.date()
    for link in af.links:
        prog, prod, entry = reg.program(link.program_id), reg.product(link.product), reg.link(link.url)
        if prog is None or not prog.active:
            out.append(_r("AFFILIATE_PROGRAM_UNKNOWN", f"'{link.program_id}' is not an active program in your registry"))
            continue
        if entry is None or entry.program_id != prog.id or entry.product.lower() != link.product.lower():
            out.append(_r("AFFILIATE_LINK_UNREGISTERED", f"{link.url} isn't registered for {prog.id} / '{link.product}'"))
        if prod is None or prod.retired:
            out.append(_r("AFFILIATE_UNEVALUATED_PRODUCT", f"'{link.product}' is not an evaluated product in your registry"))
        if prog.id not in warned:
            warned.add(prog.id)
            if prog.terms_age_days(today) > TERMS_STALE_DAYS:
                out.append(_r("AFFILIATE_TERMS_STALE", f"{prog.name}: terms last reviewed {prog.terms_reviewed_on} ({prog.terms_age_days(today)} days ago)"))
            if prog.required_wording and prog.required_wording.lower() not in af.disclosure_text.lower():
                out.append(_r("AFFILIATE_REQUIRED_WORDING_MISSING", f"{prog.name} requires: '{prog.required_wording}'"))
    return out


def check_links_(c: GateContext) -> list[CheckResult]:
    text = "\n".join([c.metadata.description, c.owner_text()])
    out_of_plan = []
    if c.registry:  # anything that looks like an affiliate link must be declared in the plan
        planned = {lk.url for lk in c.plan.affiliate.links} if c.plan.affiliate else set()
        registered = {lk.url for lk in c.registry.links}
        prefixes = [px.lower() for prog in c.registry.programs for px in prog.link_prefixes]
        for u in find_links(text):
            if u not in planned and (u in registered or any(u.lower().startswith(px) for px in prefixes)):
                out_of_plan.append(_r("AFFILIATE_LINK_OUTSIDE_PLAN", f"'{u}' is an affiliate link but this clip's affiliate plan doesn't list it"))
    bad, cloaked = check_links(text, c.allowlist())
    bad = [u for u in bad if not SYSTEM_LINK.match(u)]
    out = out_of_plan + [_r("LINK_NOT_ALLOWLISTED", f"'{u}' is not on the allowlist (exact match required)") for u in bad]
    out += [_r("LINK_CLOAKED", f"'{u}': {why}") for u, why in cloaked]
    return out or [_ok("Links", f"{len(find_links(text))} link(s), all allowlisted")]


# Explicit advice and pump/urgency language block wherever they appear (owner text or transcript).
ADVICE = [
    r"\byou (?:should|need to|must) (?:buy|sell|short|invest in|get into)\b", r"\b(?:go|just) buy (?:it|this|now)\b",
    r"\bbuy now\b", r"\bstrong buy\b", r"\bbuy signal\b", r"\bsell (?:it |this )?now\b", r"\bcan'?t lose\b",
    r"\bguaranteed (?:returns?|profits?|gains?|income)\b", r"\brisk[- ]free\b", r"\b100 ?% (?:safe|sure|certain)\b",
    r"\bdouble your money\b", r"\bwill (?:definitely|surely|certainly) (?:go up|rise|moon|pump)\b",
]
PUMP = [
    r"\bto the moon\b", r"\bmoon(?:ing|shot)\b", r"🚀", r"\b(?:get|buy) in (?:now|early|before)\b",
    r"\bbefore it'?s too late\b", r"\bdon'?t miss (?:out|this)\b", r"\bact (?:now|fast)\b", r"\bnext \d+x\b",
    r"\b\d{2,}x (?:gains?|returns?)\b", r"\bexplod(?:e|ing) (?:soon|tomorrow)\b",
]
# Words that are fine in education ("what a short squeeze looks like") but need a human look.
TERMS = [r"\bshort squeeze\b", r"\bparabolic\b", r"\bpenny stocks?\b", r"\bhidden gem\b", r"\bload(?:ing)? up\b", r"\bprice target\b"]
_ADV = [re.compile(p, re.I) for p in ADVICE]
_PUMP = [re.compile(p, re.I) for p in PUMP]
_TERMS = [re.compile(p, re.I) for p in TERMS]
# Numbers WITH their magnitude: "$1.9B", "3.5M", "50x", "12%", "1.2 billion".
NUMBER = re.compile(r"(?<![\w.:/])[-+]?[$€£]?\d[\d,]*(?:\.\d+)?(?:\s?(?:%|bps|x|k|m|bn|b|t|thousand|million|billion|trillion))?(?![\w:/])", re.I)
_DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?Z?)?\b|\b\d{1,2}:\d{2}\b")
_MAG = {"thousand": "k", "million": "m", "billion": "b", "bn": "b", "trillion": "t"}


def norm_number(n: str) -> str:
    k = n.lower().replace(",", "").replace(" ", "").lstrip("+").replace("$", "").replace("€", "").replace("£", "")
    for word, short in _MAG.items():
        if k.endswith(word):
            k = k[: -len(word)] + short
    return k


def numbers(text: str, skip_years: bool = True) -> set[str]:
    out = set()
    for n in NUMBER.findall(_DATES.sub(" ", text)):
        k = norm_number(n)
        if skip_years and re.fullmatch(r"(19|20)\d\d", k):
            continue
        out.add(k)
    return out


def check_markets(c: GateContext) -> list[CheckResult]:
    p = c.plan
    if "markets" not in p.tags and p.markets is None:
        return []
    if p.markets is None:
        return [_r("FINANCE_PLAN_MISSING")]
    m, out = p.markets, []
    corpus = "\n".join([c.transcript_text, c.owner_text()])
    for rx in _ADV:
        if hit := rx.search(corpus):
            out.append(_r("FINANCE_ADVICE_LANGUAGE", f"'{hit.group(0)}'"))
    for rx in _PUMP:
        if hit := rx.search(corpus):
            out.append(_r("FINANCE_PUMP_PATTERN", f"'{hit.group(0)}'"))
    for rx in _TERMS:
        if hit := rx.search(c.owner_text()):  # the owner chose to say it: block
            out.append(_r("FINANCE_PUMP_PATTERN", f"'{hit.group(0)}' in your own text"))
        elif hit := rx.search(c.transcript_text):  # said in the source: a person decides
            out.append(_r("FINANCE_TERM_REVIEW", f"'{hit.group(0)}' is said in the clip"))
    if not (m.disclaimer_on_screen and m.disclaimer_in_description and MARKETS_DISCLAIMER in c.metadata.description):
        out.append(_r("FINANCE_DISCLAIMER_MISSING"))
    if m.positions_held and not m.positions_disclosed:
        out.append(_r("FINANCE_POSITION_UNDISCLOSED"))
    if ("sponsored" in p.tags or p.paid_promotion or m.paid_by) and not (m.paid_by and m.payment_amount):
        out.append(_r("FINANCE_PAID_UNDISCLOSED"))
    sourced = {norm_number(d.value) for d in m.data_points}
    for k in sorted(numbers(c.owner_text()) - sourced):
        out.append(_r("FINANCE_UNSOURCED_NUMBER", f"'{k}' in your title/summary/tags/cards has no data source"))
    for k in sorted(numbers(c.transcript_text) - sourced):
        if len(k.strip("%")) > 1:
            out.append(_r("FINANCE_UNSOURCED_NUMBER", f"'{k}' is said in the clip but has no data point"))
            break
    for d in m.data_points:
        if not d.source_url.startswith("https://"):
            out.append(_r("FINANCE_UNSOURCED_NUMBER", f"'{d.label}' source is not an https link"))
        age = c.now - (d.retrieved_at if d.retrieved_at.tzinfo else d.retrieved_at.replace(tzinfo=UTC))
        if age > timedelta(hours=c.settings.gate.markets_max_data_age_hours):
            out.append(_r("FINANCE_STALE_DATA", f"'{d.label}' is {age.total_seconds() / 3600:.0f} h old"))
    if not any(r.severity is Severity.BLOCK for r in out):
        out.append(_ok("Markets", f"{len(m.data_points)} cited data point(s); disclaimer and disclosures present"))
    return out


CLICKBAIT = [
    r"you won'?t believe", r"\bshocking\b", r"gone wrong", r"\binsane\b", r"\bdestroy(?:s|ed)\b", r"\bexposed\b",
    r"must see", r"!!+", r"\?\?+", r"\bgone viral\b", r"\bnobody (?:is talking|tells you)\b", r"\bthe truth about\b",
]
_CB = [re.compile(p, re.I) for p in CLICKBAIT]


def tag_chars(tags: list[str]) -> int:
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def stem(t: str) -> str:
    """Very light stemming for tag/hashtag de-duplication (never for grounding decisions)."""
    t = t.removesuffix("'s").removesuffix("s'")
    for suf in ("ing", "ed", "s"):
        if t.endswith(suf) and not t.endswith("ss") and len(t) - len(suf) >= 3:
            t = t[: -len(suf)].rstrip("-")
            break
    return t[:-1] if t.endswith("e") and len(t) > 4 else t  # bridge/bridges/bridged -> bridg


def _bases(w: str) -> set[str]:
    """The word itself plus what it could be an inflection of: bridges -> bridge, swayed -> sway,
    caring -> care. Bases shorter than 4 letters don't count (news is not new, cars is not car)."""
    w = w.removesuffix("'s").removesuffix("s'")
    out = {w}
    for suf in ("s", "es", "ed", "d", "ing"):
        if w.endswith(suf) and not (suf == "s" and w.endswith("ss")):
            b = w[: -len(suf)]
            if len(b) >= 4:
                out.add(b)
            if suf in ("ed", "ing") and len(b) >= 3:
                out.add(b + "e")  # caring -> care, danced -> dance
    return out


def ungrounded_words(text: str, allowed: set[str]) -> list[str]:
    """Words of `text` that weren't said, allowing simple inflections of words that were."""
    pool = set().union(*(_bases(a) for a in allowed)) if allowed else set()
    return [t for t in tokens(text) if not (_bases(t) & pool)]


def without_phrase(text: str, phrase: str) -> str:
    """The channel name may appear in a title, but its words never count as 'said in the clip'."""
    return re.sub(re.escape(phrase.strip()), " ", text, flags=re.I) if phrase.strip() else text


def _grounding(text: str, allowed: set[str]) -> float:
    tt = tokens(text)
    return (len(tt) - len(ungrounded_words(text, allowed))) / len(tt) if tt else 1.0


def check_metadata(c: GateContext) -> list[CheckResult]:
    md, out = c.metadata, []
    if len(md.title) > 100 or len(md.description.encode("utf-8")) > 5000 or tag_chars(md.tags) > 500:
        out.append(_r("METADATA_LIMITS"))
    va_text = " ".join(v.visible_text() for v in c.plan.value_add)
    allowed = set(tokens(c.transcript_text)) | set(tokens(va_text))
    grounded = _grounding(without_phrase(md.title, c.settings.channel.name), allowed)
    if grounded < 0.7:
        out.append(_r("METADATA_UNGROUNDED", f"only {grounded:.0%} of title words appear in the clip"))
    for label, text in (("summary", c.summary), ("tags", " ".join(md.tags))):
        g = _grounding(text, allowed)
        if g < 0.7:
            out.append(_r("METADATA_UNGROUNDED", f"only {g:.0%} of {label} words appear in the clip"))
    said = numbers(c.transcript_text, skip_years=False) | numbers(va_text, skip_years=False)
    for label, text in (("title", md.title), ("summary", c.summary), ("tags", " ".join(md.tags))):
        for k in sorted(numbers(text, skip_years=False) - said):
            out.append(_r("METADATA_UNGROUNDED", f"number '{k}' in the {label} is never said in the clip"))
    for rx in _CB:
        if (hit := rx.search(md.title)) and not rx.search(c.transcript_text):
            out.append(_r("METADATA_CLICKBAIT", f"'{hit.group(0)}'"))
    words = [w for w in re.findall(r"[A-Za-z]{3,}", md.title)]
    if words and sum(w.isupper() for w in words) / len(words) > 0.3:
        out.append(_r("METADATA_CLICKBAIT", "mostly ALL CAPS"))
    return out or [_ok("Metadata", f"title {len(md.title)} chars, {grounded:.0%} grounded; description {len(md.description.encode())} bytes")]


def check_uniqueness(c: GateContext) -> list[CheckResult]:
    g, ch, h, out = c.settings.gate, c.settings.channel, c.history, []
    if h.today_count >= ch.daily_cap:
        out.append(_r("DAILY_CAP_REACHED", f"{h.today_count} of {ch.daily_cap} clips already scheduled today"))
    if h.today_same_source >= ch.max_clips_per_source_per_day:
        out.append(_r("VARIETY_SAME_SOURCE", f"{h.today_same_source} clip(s) from this source today"))
    my_frames = c.fingerprint.get("dhash", [])
    my_text = set(c.fingerprint.get("shingles", []))
    for fp in h.fingerprints:
        theirs = fp.get("dhash", [])
        if my_frames and theirs:
            close = sum(1 for a in my_frames if any(hamming(a, b) <= g.duplicate_hamming_threshold for b in theirs))
            if close / len(my_frames) >= 0.75:
                out.append(_r("DUPLICATE_OF_OWN_UPLOAD", f"{close}/{len(my_frames)} frames match an earlier upload"))
                break
        ts_ = set(fp.get("shingles", []))
        if my_text and ts_ and len(my_text & ts_) / len(my_text | ts_) >= g.duplicate_text_jaccard:
            out.append(_r("DUPLICATE_OF_OWN_UPLOAD", "transcript matches an earlier upload"))
            break
    mine = set(tokens(c.metadata.title))
    for t in h.titles[:10]:
        other = set(tokens(t))
        if mine and other and len(mine & other) / len(mine | other) >= g.variety_title_jaccard:
            out.append(_r("VARIETY_TITLE", f"too close to '{t[:60]}'"))
            break
    if len(h.templates) >= 5 and all(t == c.plan.template for t in h.templates[:5]):
        out.append(_r("VARIETY_TEMPLATE", f"last 5 uploads all used '{c.plan.template}'"))
    return out or [_ok("Uniqueness", f"not a duplicate; {h.today_count}/{ch.daily_cap} today")]


def load_screen_terms(path: Path) -> dict[str, list[str]]:
    terms: dict[str, list[str]] = {"hard": [], "soft": []}
    if not path.exists():  # never screen silently with an empty list
        raise FileNotFoundError(f"screen-terms list missing: {path}")
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                tier, term = line.split(":", 1)
                if tier.strip() in terms and term.strip():
                    terms[tier.strip()].append(term.strip().lower())
    return terms


def _term_hit(term: str, text: str) -> bool:
    return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", text, re.I) is not None


def check_screen(c: GateContext) -> list[CheckResult]:
    owner_text = "\n".join([c.metadata.description, c.owner_text()])
    out = []
    for term in c.screen_terms.get("hard", []):
        if _term_hit(term, owner_text) or _term_hit(term, c.transcript_text):
            out.append(_r("SCREEN_BLOCKED_TERM", f"'{term}'"))
    for term in c.screen_terms.get("soft", []):
        if _term_hit(term, owner_text) or _term_hit(term, c.transcript_text):
            out.append(_r("SCREEN_REVIEW", f"'{term}'"))
    return out or [_ok("Screen", "no screened terms")]


def check_untrusted(c: GateContext) -> list[CheckResult]:
    out = [_r("PROMPT_INJECTION_SUSPECTED", f"transcript contains '{s}'") for s in injection_signals(c.transcript_text)]
    out += [_r("SPOKEN_LINK", f"'{u}'") for u in find_links(c.transcript_text)]
    return out or [_ok("Untrusted input", "no instruction-like text or spoken links in the transcript")]


CHECKS = [check_safety, check_rights, check_value_add, check_disclosures, check_affiliate, check_links_,
          check_markets, check_metadata, check_uniqueness, check_screen, check_untrusted]


def run_gate(c: GateContext, clip_sha256: str) -> GateReport:
    results: list[CheckResult] = []
    for fn in CHECKS:
        results.extend(fn(c))
    md_sha = body_hash(c.body)
    return GateReport(clip_id=c.plan.clip_id, passed=not any(r.severity is Severity.BLOCK for r in results),
                      results=results, clip_sha256=clip_sha256, metadata_sha256=md_sha, created_at=c.now)
