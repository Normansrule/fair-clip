"""Stage 6: title, description, tags and chapters, grounded in what was actually said.

The *frame* of the description (disclosures first, then summary, credit, links, sources) is
assembled by code, never by a model, so a model can't drop or reorder a disclosure. A model
(optional) may only propose the title, summary and tags; its output is validated like any
other untrusted input and then re-checked by the compliance gate.
"""

from __future__ import annotations

import re
from collections import Counter

from .llm import (
    SYSTEM_PROMPT,
    LLMAdapter,
    ModelOutputRejected,
    parse_model_metadata,
    wrap_untrusted,
)
from .models import Chapter, ClipPlan, Metadata, RightsRecord, Sentence
from .scoring import STOP, relevance, tokens

MARKETS_DISCLAIMER = (
    "Educational content only. Not investment advice, not a recommendation to buy or sell any "
    "security. Do your own research and consider a licensed professional."
)
SYNTHETIC_NOTE = "This video contains altered or synthetic content (disclosed to YouTube)."


def pick_hook(sentences: list[Sentence], topics: list[str]) -> str:
    """The clip's most on-topic sentence, verbatim (we never invent a hook)."""
    if not sentences:
        return ""
    best = max(sentences, key=lambda s: (relevance(s.text, topics), -s.start))
    return best.text.strip()


def shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text.replace("<", "").replace(">", "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def chapters_for(sentences: list[Sentence], clip_start: float, clip_len: float) -> list[Chapter]:
    """YouTube chapters need ≥ 3 entries, the first at 0:00, each ≥ 10 s. Short clips get none."""
    if clip_len < 60 or len(sentences) < 3:
        return []
    n = min(5, int(clip_len // 20))
    if n < 3:
        return []
    out, step = [], clip_len / n
    for i in range(n):
        t = i * step
        s = min(sentences, key=lambda x: abs((x.start - clip_start) - t))
        out.append(Chapter(at=round(t if i else 0.0, 1), title=shorten(s.text, 60)))
    if any(b.at - a.at < 10 for a, b in zip(out, out[1:], strict=False)):
        return []
    return out


def fmt_ts(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m}:{s:02d}"


def assemble_description(plan: ClipPlan, summary: str, rights: RightsRecord, attribution: str,
                         chapters: list[Chapter]) -> str:
    parts: list[str] = []
    if plan.affiliate:  # disclosure ABOVE the first link, in plain words
        parts.append(plan.affiliate.disclosure_text)
    if plan.paid_promotion or "sponsored" in plan.tags:
        parts.append("This video includes paid promotion" + (f" (sponsor: {plan.sponsor})." if plan.sponsor else "."))
    if plan.markets:
        parts.append(MARKETS_DISCLAIMER)
        if plan.markets.paid_by:
            parts.append(f"Paid promotion: {plan.markets.paid_by} paid {plan.markets.payment_amount or 'an undisclosed amount'} for this video.")
        if plan.markets.positions_held:
            parts.append("Disclosure: I hold positions in " + ", ".join(plan.markets.positions_held) + ".")
    if rights.synthetic_or_altered:
        parts.append(SYNTHETIC_NOTE)
    # The owner's own words lead: they are the reason this clip exists. Commentary and context
    # read as plain paragraphs; analysis and graphics keep a label.
    for va in plan.value_add:
        if va.author.value == "owner" or va.owner_approved:
            plain = va.kind.value in ("commentary", "context_card")
            parts.append(va.text if plain else f"{va.kind.value.replace('_', ' ').title()}: {va.text}")
    if summary:
        parts.append(f"In this clip: {summary}")
    parts.append(f"Clip from: {rights.source_title}")
    if attribution:
        parts.append(attribution)
    if plan.affiliate and plan.affiliate.links:
        parts.append("\n".join(f"{link.product}: {link.url}" for link in plan.affiliate.links))
    if plan.markets and plan.markets.data_points:
        parts.append("Sources:\n" + "\n".join(
            f"- {d.label}: {d.value} ({d.source_name}, retrieved {d.retrieved_at:%Y-%m-%d %H:%M} UTC) {d.source_url}"
            for d in plan.markets.data_points))
    if chapters:
        parts.append("\n".join(f"{fmt_ts(c.at)} {c.title}" for c in chapters))
    return "\n\n".join(p for p in parts if p).replace("<", "").replace(">", "")


_LEAD = re.compile(r"^(?:but|and|so|now|well|okay|then|also)\b[,]?\s+", re.I)
_WEAK = set("""once every might right side sideways actually really always never still even much many each other
    another something someone anything thing things little lot lots kind sort way ways start started make made
    said says tell told know knew think thought want wanted look looked come came went goes take took give gave
    first second third last next whole same different between through across around before after again
    began begin begins under over into onto upon while until since ever""".split())
_PROPER = re.compile(r"\b([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|\d+\b))*)")
_MONTHS = set("january february march april may june july august september october november december".split())


def _trim_lead(text: str) -> str:
    """Summaries read better without a leading 'But'/'So,' (the words stay verbatim otherwise)."""
    t = _LEAD.sub("", text.strip(), count=1)
    return t[:1].upper() + t[1:] if t else t


def pick_tags(clip_text: str, topics: list[str], k: int = 8) -> list[str]:
    """Tags a person would choose: the channel's topics that this clip is about, names said in it
    (London, Millennium Bridge), then its most frequent meaningful words."""
    from .gate import stem

    toks = tokens(clip_text)
    stems = {stem(t) for t in toks}
    out: list[str] = [t.lower() for t in topics if t and all(stem(x) in stems for x in tokens(t))]
    for m in _PROPER.finditer(clip_text):
        parts = m.group(1).split()
        starts_sentence = m.start() == 0 or re.search(r"[.!?…:]\s*$", clip_text[: m.start()]) is not None
        if starts_sentence and len(parts) == 1:
            continue  # "When", "Engineers": capitalised only because a sentence starts there
        while parts and (parts[0].lower() in _MONTHS or parts[0].lower() in STOP):
            parts = parts[1:]
        if parts and not parts[0].isdigit():
            out.append(" ".join(parts))
    counts = Counter(t for t in toks if len(t) >= 4 and t not in _WEAK and not t.endswith(("ed", "ing")) and t not in _MONTHS)
    out += [w for w, _ in counts.most_common(k * 2)]
    seen, tags = set(), []
    for t in out:
        key = stem(t.lower())
        if key not in seen and len(t) <= 30:
            seen.add(key)
            seen.update(stem(x) for x in t.lower().split())  # "Millennium Bridge" covers "millennium"
            tags.append(t)
    return tags[:k]



def build_metadata(plan: ClipPlan, sentences: list[Sentence], rights: RightsRecord, attribution: str,
                   topics: list[str], llm: LLMAdapter | None = None) -> tuple[Metadata, list[str], str]:
    """Returns (metadata, notes, summary). Notes record which engine produced what (audit log);
    the summary is returned separately so the gate can check the model-written part hardest."""
    notes: list[str] = []
    clip_text = " ".join(s.text for s in sentences)
    hook = pick_hook(sentences, topics)
    title = shorten(hook, 90)
    summary = shorten(_trim_lead(" ".join(s.text for s in sentences[:2])), 600)
    tags = pick_tags(clip_text, topics)
    if llm is not None:
        block, _ = wrap_untrusted(clip_text)
        try:
            proposed = parse_model_metadata(llm.complete(SYSTEM_PROMPT, block))
            title, summary, tags = proposed.title, proposed.description, proposed.tags[:8]
            notes.append("title/summary/tags proposed by model; validated by schema")
        except ModelOutputRejected as exc:
            notes.append(f"model output rejected ({exc}); fell back to extractive metadata")
    else:
        notes.append("extractive metadata (no model)")
    chapters = chapters_for(sentences, plan.start, plan.end - plan.start)
    desc = assemble_description(plan, summary, rights, attribution, chapters)
    md = Metadata(title=title, description=desc, tags=tags, chapters=chapters)
    return md, notes, summary
