"""Affiliate module (milestone 3): the owner's registry of programs, evaluated products and links,
plus spoken-disclosure detection timed against the transcript.

The registry is the source of truth the compliance gate checks against:

* **Programs** you actually joined, with the URL of their terms, when you last re-read those
  terms, the URL prefixes their links must start with, and any wording the program requires in
  your disclosure (copy it from the program's own terms).
* **Products** you actually evaluated: when, and in your own words how. The tool never writes
  this for you, and a product without it can't be recommended.
* **Links**: the exact URLs you may use, each tied to one program and one product. Registered
  links of active programs are allowlisted automatically; nothing else is.

Stored as one validated JSON document in the store (`affiliate_registry`), changed only through
`update_registry`, which validates the whole result, writes it atomically and audits the change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .links import entry_problems
from .models import ShortText, Strict, no_hidden_chars

if TYPE_CHECKING:  # the store needs sqlite3, which the browser build doesn't have
    from .store import Store

REGISTRY_KEY = "affiliate_registry"
TERMS_STALE_DAYS = 180  # re-read a program's terms at least twice a year

Slug = Annotated[str, Field(pattern=r"^[a-z0-9_-]{2,40}$")]


def _https_url(v: str, what: str) -> str:
    no_hidden_chars(v)
    u = urlsplit(v)
    if u.scheme != "https" or not u.hostname or any(c.isspace() for c in v) or u.username or u.password:
        raise ValueError(f"{what} must be a plain https:// URL")
    return v


class Program(Strict):
    id: Slug
    name: ShortText
    terms_url: Annotated[str, Field(max_length=500)]
    joined_on: date
    terms_reviewed_on: date
    link_prefixes: list[Annotated[str, Field(max_length=300)]] = Field(min_length=1, max_length=5)
    required_wording: Annotated[str, Field(max_length=200)] = ""
    active: bool = True
    notes: Annotated[str, Field(max_length=1000)] = ""

    @field_validator("terms_url")
    @classmethod
    def _terms(cls, v: str) -> str:
        return _https_url(v, "terms_url")

    @field_validator("link_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        for p in v:
            _https_url(p, "link prefix")
            # Must include the path slash after the host, so "https://shop.example" can't also
            # match "https://shop.example.evil.net/...".
            if urlsplit(p).path[:1] != "/":
                raise ValueError(f"link prefix '{p}' must end the host with '/' (e.g. https://shop.example/)")
        return v

    @field_validator("required_wording", "notes", "name")
    @classmethod
    def _clean(cls, v: str) -> str:
        return no_hidden_chars(v)

    @model_validator(mode="after")
    def _dates(self) -> Program:
        today = datetime.now(UTC).date()
        if self.terms_reviewed_on > today or self.joined_on > today:
            raise ValueError(f"{self.id}: dates can't be in the future")
        return self

    def terms_age_days(self, today: date) -> int:
        return (today - self.terms_reviewed_on).days


class Product(Strict):
    name: ShortText = Field(min_length=2)
    evaluated_on: date
    how_evaluated: Annotated[str, Field(min_length=30, max_length=1000)]  # your words: what you actually did
    retired: bool = False

    @field_validator("name", "how_evaluated")
    @classmethod
    def _clean(cls, v: str) -> str:
        return no_hidden_chars(v)


class RegisteredLink(Strict):
    url: Annotated[str, Field(max_length=500)]
    program_id: Slug
    product: ShortText
    added_on: date

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        _https_url(v, "link")
        if probs := entry_problems(v):
            raise ValueError("; ".join(probs))
        return v


class AffiliateRegistry(Strict):
    programs: list[Program] = Field(default_factory=list, max_length=100)
    products: list[Product] = Field(default_factory=list, max_length=500)
    links: list[RegisteredLink] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> AffiliateRegistry:
        today = datetime.now(UTC).date()
        ids = [p.id for p in self.programs]
        if len(ids) != len(set(ids)):
            raise ValueError("program ids must be unique")
        names = [p.name.lower() for p in self.products]
        if len(names) != len(set(names)):
            raise ValueError("product names must be unique")
        for p in self.products:
            if p.evaluated_on > today:
                raise ValueError(f"'{p.name}': evaluated_on is in the future")
        urls = [lk.url for lk in self.links]
        if len(urls) != len(set(urls)):
            raise ValueError("each link may be registered once")
        for lk in self.links:
            prog = self.program(lk.program_id)
            if prog is None:
                raise ValueError(f"link {lk.url}: unknown program '{lk.program_id}'")
            if not any(lk.url.startswith(px) for px in prog.link_prefixes):
                raise ValueError(f"link {lk.url} doesn't start with any of {prog.id}'s link prefixes")
            if self.product(lk.product) is None:
                raise ValueError(f"link {lk.url}: product '{lk.product}' isn't in your evaluated products")
        return self

    def program(self, pid: str) -> Program | None:
        return next((p for p in self.programs if p.id == pid), None)

    def product(self, name: str) -> Product | None:
        return next((p for p in self.products if p.name.lower() == name.lower()), None)

    def link(self, url: str) -> RegisteredLink | None:
        return next((lk for lk in self.links if lk.url == url), None)

    def usable_links(self) -> list[str]:
        """Exact URLs the gate allowlists: registered, active program, product not retired."""
        out = []
        for lk in self.links:
            prog, prod = self.program(lk.program_id), self.product(lk.product)
            if prog and prog.active and prod and not prod.retired:
                out.append(lk.url)
        return out


def load_registry(store: Store) -> AffiliateRegistry:
    return AffiliateRegistry.model_validate(store.get(REGISTRY_KEY) or {})


def update_registry(store: Store, fn: Callable[[AffiliateRegistry], AffiliateRegistry], actor: str, action: str,
                    details: dict[str, Any]) -> AffiliateRegistry:
    """Apply `fn`, validate the WHOLE result, write it atomically, audit it. Invalid → nothing changes."""
    result: dict[str, AffiliateRegistry] = {}

    def apply(cur: Any) -> dict[str, Any]:
        new = fn(AffiliateRegistry.model_validate(cur or {}))
        new = AffiliateRegistry.model_validate(new.model_dump(mode="json"))  # re-run cross-checks
        result["r"] = new
        return new.model_dump(mode="json")

    store.update(REGISTRY_KEY, apply)
    store.audit.append(actor, f"affiliate_{action}", "affiliate", details)
    return result["r"]


def import_registry(store: Store, data: dict[str, Any], actor: str) -> AffiliateRegistry:
    """Replace the registry with a JSON document (programs, products, links), validated as a whole."""
    new = AffiliateRegistry.model_validate(data)
    return update_registry(store, lambda _cur: new, actor, "import",
                           {"programs": len(new.programs), "products": len(new.products), "links": len(new.links)})


# ----------------------------------------------------------------------------- spoken disclosures

# Explicit phrases that say "I get paid for this". No bare "commission" or "sponsored": those
# also appear in "out of commission" and "this is not sponsored". Kept byte-identical with the
# web demo (site/js/disclosure.js; a test checks).
PLAIN_DISCLOSURE = re.compile(
    r"\b(?:(?:i|we) (?:earn|get|make|receive) (?:an? )?(?:small )?(?:commissions?|money|paid|a cut|a fee)"
    r"|(?:i|we)(?:'m|'re| am| are) (?:paid|compensated)|paid (?:link|promotion|partnership)|sponsored by|affiliate commission)\b",
    re.I)
# Denials: a negation anywhere earlier in the same sentence ("this is NOT, in any way, sponsored by
# them", "we DON'T earn a commission"), or a denial right after the phrase ("I'm paid NOTHING",
# "we're paid by NO ONE"). Benign phrases are removed first, so the honest classic
# "I earn a commission, at no extra cost to you" still counts. All three patterns are mirrored
# byte-for-byte in site/js/disclosure.js (a test checks).
NEGATION = re.compile(r"\b(?:not|no|never|neither|nor|without|none|isn't|wasn't|aren't|weren't|don't|doesn't|didn't|won't|can't|haven't|hasn't)\b|n't\b", re.I)
DENIAL_AFTER = re.compile(r"^\W*(?:\w+\W+){0,2}?(?:nothing|nobody|no one|none|zero|not a (?:cent|penny|dime|thing))\b", re.I)
BENIGN = re.compile(r"\b(?:at )?no (?:extra |additional )?(?:cost|charge|fee)(?: to you)?\b|\bno matter\b|\bnot only\b", re.I)
APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\uff07": "'"})


def _sentence_bounds(text: str, start: int, end: int) -> tuple[str, str]:
    """The part of the match's sentence before it, and after it."""
    a = max(text.rfind(ch, 0, start) for ch in ".!?;\n") + 1
    nxt = [i for i in (text.find(ch, end) for ch in ".!?;\n") if i >= 0]
    return text[a:start], text[end:min(nxt) if nxt else len(text)]


def _negated(text: str, start: int, end: int) -> bool:
    before, after = _sentence_bounds(text, start, end)
    return bool(NEGATION.search(BENIGN.sub(" ", before)) or DENIAL_AFTER.search(after))


def disclosure_hits(text: str, rx: re.Pattern[str] = PLAIN_DISCLOSURE) -> list[re.Match[str]]:
    """Disclosure phrases in `text`, minus denials. Curly apostrophes are normalised first
    (smart quotes are the default on phones and word processors)."""
    text = text.translate(APOSTROPHES)
    return [m for m in rx.finditer(text) if not _negated(text, m.start(), m.end())]


def says_paid(text: str, rx: re.Pattern[str] = PLAIN_DISCLOSURE) -> bool:
    return bool(disclosure_hits(text, rx))


def detect_spoken(words: list[tuple[float, float, str]], rx: re.Pattern[str] = PLAIN_DISCLOSURE) -> list[tuple[float, str]]:
    """Where in the clip a disclosure is actually SAID: [(clip time of its first word, the phrase)].

    Joins the clip's words (already on the clip timeline, after jump cuts) and maps each match
    back to the word it starts in, so the time is the transcript's, not the plan's claim.
    """
    text, starts = "", []
    for s, _e, w in words:
        if text:
            text += " "
        starts.append((len(text), s))
        text += w.translate(APOSTROPHES)
    out = []
    for m in disclosure_hits(text, rx):
        t = next((s for off, s in reversed(starts) if off <= m.start()), 0.0)
        out.append((round(t, 2), m.group(0)))
    return out


def registry_problems(af: Any, registry: AffiliateRegistry, today: date | None = None) -> list[str]:
    """Re-check a clip's affiliate links against the registry AS IT IS NOW. Used at approval and
    again right before upload, so pausing a program or retiring a product stops clips that were
    gated earlier. Empty list = still fine."""
    if af is None:
        return []
    today = today or datetime.now(UTC).date()
    usable, out = set(registry.usable_links()), []
    for link in af.links:
        entry, prod = registry.link(link.url), registry.product(link.product)
        if link.url not in usable or entry is None:
            out.append(f"{link.url} is no longer usable (program paused, product retired or link removed)")
            continue
        if entry.program_id != link.program_id or entry.product.lower() != link.product.lower():
            out.append(f"{link.url} is now registered for {entry.program_id} / '{entry.product}', not '{link.product}'")
            continue
        if prod is None or prod.retired:
            out.append(f"'{link.product}' is retired or no longer in your evaluated products")
            continue
        prog = registry.program(link.program_id)
        if prog and prog.required_wording and prog.required_wording.lower() not in af.disclosure_text.lower():
            out.append(f"{prog.name} now requires the wording '{prog.required_wording}'")
    return out
