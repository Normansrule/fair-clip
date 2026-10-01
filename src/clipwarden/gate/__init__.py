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

from ..config import Settings
from ..links import check_links, find_links
from ..llm import injection_signals
from ..media.analyze import hamming
from ..metadata import MARKETS_DISCLAIMER
from ..models import (
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
    if probs:
        return [_r(code, msg) for code, msg in probs]
    return [_ok("Rights", f"rights record {c.rights.record_id} ({c.rights.basis.value}) covers this use")]


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


def check_disclosures(c: GateContext) -> list[CheckResult]:
    out = []
    st = c.body.get("status", {})
    if c.rights and synthetic_required(c.plan, c.rights) and st.get("containsSyntheticMedia") is not True:
        out.append(_r("SYNTHETIC_DISCLOSURE_MISSING"))
    if paid_promotion_required(c.plan):
        out.append(_r("PAID_PROMOTION_STUDIO_STEP"))
        if "includes paid promotion" not in c.metadata.description.lower() and not (c.plan.markets and c.plan.markets.paid_by):
            out.append(_r("PAID_PROMOTION_DESCRIPTION_MISSING"))
    if not any(r.severity is Severity.BLOCK for r in out):
        flags = f"synthetic={st.get('containsSyntheticMedia')}, kids={st.get('selfDeclaredMadeForKids')}, paid={paid_promotion_required(c.plan)}"
        out.append(_ok("Disclosures", f"upload flags consistent ({flags})"))
    return out


PLAIN_DISCLOSURE = re.compile(r"\b(commission|paid|earn|money|compensat|sponsor)", re.I)
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
    spoken = af.spoken_disclosure_at is not None and af.spoken_disclosure_at <= AFFILIATE_START_WINDOW
    if spoken:  # a spoken disclosure has to actually be in the transcript
        spoken = bool(PLAIN_DISCLOSURE.search(c.transcript_text))
    shown = af.start_card is not None and af.start_card[0] <= AFFILIATE_START_WINDOW and af.start_card[1] - af.start_card[0] >= 2.0
    if not (spoken or shown):
        out.append(_r("AFFILIATE_START_DISCLOSURE_MISSING"))
    elif not (spoken and shown):
        out.append(_r("AFFILIATE_SINGLE_CHANNEL"))
    if af.links and not af.recommendation_times:
        out.append(_r("AFFILIATE_RECOMMENDATION_UNMARKED"))
    for t in af.recommendation_times:
        if not any(a - RECOMMENDATION_TOLERANCE <= t <= b + RECOMMENDATION_TOLERANCE for a, b in af.recommendation_cards):
            out.append(_r("AFFILIATE_CARD_MISSING", f"no disclosure card within ±{RECOMMENDATION_TOLERANCE:.0f} s of the recommendation at {t:.1f} s"))
    desc = c.metadata.description
    links = find_links(desc)
    if links:
        first = desc.find(links[0])
        pos = desc.find(af.disclosure_text)
        if pos < 0 or pos > first:
            out.append(_r("AFFILIATE_DESCRIPTION_ORDER"))
    if not PLAIN_DISCLOSURE.search(af.disclosure_text):
        out.append(_r("AFFILIATE_WEAK_WORDING", f"'{af.disclosure_text}' doesn't say you get paid"))
    evaluated = {x.lower() for x in af.products_evaluated}
    for link in af.links:
        if link.product.lower() not in evaluated:
            out.append(_r("AFFILIATE_UNEVALUATED_PRODUCT", f"'{link.product}' is not in products_evaluated"))
    owner_text = " ".join([c.metadata.title, desc, *(v.text for v in p.value_add)])
    for rx in _FAB:
        m = rx.search(owner_text)
        if m:
            out.append(_r("AFFILIATE_FABRICATED_CLAIM", f"'{m.group(0)}'"))
    if p.made_for_kids:
        out.append(_r("AFFILIATE_KIDS"))
    if not any(r.severity is Severity.BLOCK for r in out):
        out.append(_ok("Affiliate", "disclosures present at start, near recommendations and above the first link"))
    return out


def check_links_(c: GateContext) -> list[CheckResult]:
    text = "\n".join([c.metadata.title, c.metadata.description, *(v.text for v in c.plan.value_add)])
    bad, cloaked = check_links(text, c.settings.link_allowlist)
    out = [_r("LINK_NOT_ALLOWLISTED", f"'{u}' is not on the allowlist (exact match required)") for u in bad]
    out += [_r("LINK_CLOAKED", f"'{u}': {why}") for u, why in cloaked]
    return out or [_ok("Links", f"{len(find_links(text))} link(s), all allowlisted")]


ADVICE = [
    r"\byou should (?:buy|sell|short|invest in|get into)\b", r"\b(?:strong )?buy (?:it|this|now|signal)\b",
    r"\bsell (?:it |this )?now\b", r"\bprice target\b", r"\bcan'?t lose\b", r"\bguaranteed (?:returns?|profits?|gains?|income)\b",
    r"\brisk[- ]free\b", r"\b100 ?% (?:safe|sure|certain)\b", r"\bdouble your money\b", r"\bwill (?:definitely|surely|certainly) (?:go up|rise|moon|pump)\b",
]
PUMP = [
    r"\bto the moon\b", r"\bmoon(?:ing|shot)\b", r"🚀", r"\bshort squeeze\b", r"\b(?:get|buy) in (?:now|early|before)\b",
    r"\bbefore it'?s too late\b", r"\bdon'?t miss (?:out|this)\b", r"\bact (?:now|fast)\b", r"\bnext \d+x\b", r"\b\d{2,}x (?:gains?|returns?)\b",
    r"\bload(?:ing)? up\b", r"\bparabolic\b", r"\bpenny stock\b", r"\bhidden gem\b", r"\bexplod(?:e|ing) (?:soon|tomorrow)\b",
]
_ADV = [re.compile(p, re.I) for p in ADVICE]
_PUMP = [re.compile(p, re.I) for p in PUMP]
NUMBER = re.compile(r"(?<![\w:])[-+$€£]?\d[\d,]*(?:\.\d+)?%?(?![\w:])")


def check_markets(c: GateContext) -> list[CheckResult]:
    p = c.plan
    if "markets" not in p.tags and p.markets is None:
        return []
    if p.markets is None:
        return [_r("FINANCE_PLAN_MISSING")]
    m, out = p.markets, []
    corpus = "\n".join([c.transcript_text, c.metadata.title, c.metadata.description, *(v.text for v in p.value_add)])
    for rx in _ADV:
        if hit := rx.search(corpus):
            out.append(_r("FINANCE_ADVICE_LANGUAGE", f"'{hit.group(0)}'"))
    for rx in _PUMP:
        if hit := rx.search(corpus):
            out.append(_r("FINANCE_PUMP_PATTERN", f"'{hit.group(0)}'"))
    if not (m.disclaimer_on_screen and m.disclaimer_in_description and MARKETS_DISCLAIMER in c.metadata.description):
        out.append(_r("FINANCE_DISCLAIMER_MISSING"))
    if m.positions_held and not m.positions_disclosed:
        out.append(_r("FINANCE_POSITION_UNDISCLOSED"))
    if ("sponsored" in p.tags or p.paid_promotion or m.paid_by) and not (m.paid_by and m.payment_amount):
        out.append(_r("FINANCE_PAID_UNDISCLOSED"))
    sourced = {re.sub(r"[,$€£+]", "", d.value) for d in m.data_points}
    said = {re.sub(r"[,$€£+]", "", n) for n in NUMBER.findall(c.transcript_text)}
    owner_text = "\n".join([c.metadata.title, *(v.text for v in p.value_add)])
    for n in NUMBER.findall(owner_text):
        k = re.sub(r"[,$€£+]", "", n)
        if k not in sourced and k not in said:
            out.append(_r("FINANCE_UNSOURCED_NUMBER", f"'{n}' has no data source"))
    for n in NUMBER.findall(c.transcript_text):
        k = re.sub(r"[,$€£+]", "", n)
        is_year = re.fullmatch(r"(19|20)\d\d", k) is not None
        if k not in sourced and len(k.strip("%")) > 1 and not is_year:
            out.append(_r("FINANCE_UNSOURCED_NUMBER", f"'{n}' is said in the clip but has no data point"))
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


def check_metadata(c: GateContext) -> list[CheckResult]:
    md, out = c.metadata, []
    if len(md.title) > 100 or len(md.description.encode("utf-8")) > 5000 or tag_chars(md.tags) > 500:
        out.append(_r("METADATA_LIMITS"))
    allowed = set(tokens(c.transcript_text)) | set(tokens(" ".join(v.text for v in c.plan.value_add))) | set(tokens(c.settings.channel.name))
    tt = tokens(md.title)
    grounded = sum(1 for t in tt if t in allowed) / len(tt) if tt else 1.0
    if grounded < 0.7:
        out.append(_r("METADATA_UNGROUNDED", f"only {grounded:.0%} of title words appear in the clip"))
    said_numbers = set(NUMBER.findall(c.transcript_text))
    for n in NUMBER.findall(md.title):
        if n not in said_numbers:
            out.append(_r("METADATA_UNGROUNDED", f"number '{n}' in the title is never said in the clip"))
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
    owner_text = "\n".join([c.metadata.title, c.metadata.description, *(v.text for v in c.plan.value_add)])
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
