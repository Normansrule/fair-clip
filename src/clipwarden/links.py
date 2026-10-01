"""Link allowlist: the only links that may ever appear in a title, description or card.

Rules (checked by the compliance gate):
* a link must match an allowlist entry *exactly* (no prefix matching, no "same domain");
* allowlist entries must be https, carry no user-info (``https://good.com@evil.com``), and must
  not be URL shorteners or open redirectors, because those hide the real destination
  (the Federal Trade Commission (FTC) calls hidden material connections deceptive; a cloaked
  affiliate link is the classic example).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

# Bare domains like "evil.example/buy" count as links too: viewers can type them.
URL_RE = re.compile(
    r"(?i)\b((?:https?://|www\.)[^\s<>\"')\]]+|[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\.(?:com|net|org|io|co|ly|gg|me|app|dev|xyz|link|shop|store|info|biz|us|uk|ca|de)(?:/[^\s<>\"')\]]*)?)"
)
SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "rebrand.ly", "cutt.ly", "is.gd", "buff.ly",
    "shorturl.at", "tiny.cc", "rb.gy", "s.id", "t.ly", "lnkd.in", "linktr.ee", "bl.ink", "short.io",
}
REDIRECT_PARAMS = {"url", "u", "redirect", "redirect_uri", "next", "dest", "destination", "target", "goto", "out"}


def find_links(text: str) -> list[str]:
    return [m.group(1).rstrip(".,;:!?") for m in URL_RE.finditer(text)]


def entry_problems(url: str) -> list[str]:
    """Why an allowlist entry itself is unsafe (empty list = fine)."""
    p = urlsplit(url)
    out = []
    if p.scheme != "https":
        out.append("not https")
    if p.username or p.password or "@" in p.netloc:
        out.append("contains user-info (@), which disguises the real host")
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        out.append("no valid host")
    if host in SHORTENERS or any(host.endswith("." + s) for s in SHORTENERS):
        out.append("URL shortener hides the destination (cloaking)")
    if host.startswith("xn--") or ".xn--" in host:
        out.append("punycode host can imitate another domain")
    qs = parse_qs(p.query)
    for k, vals in qs.items():
        if k.lower() in REDIRECT_PARAMS and any(v.startswith(("http", "//")) for v in vals):
            out.append(f"redirect parameter '{k}' points somewhere else (cloaking)")
    return out


def check_links(text: str, allowlist: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (not_allowlisted, cloaked) for every link found in text."""
    allowed = set(allowlist)
    bad, cloaked = [], []
    for link in find_links(text):
        probs = entry_problems(link if link.startswith("http") else "https://" + link)
        if link not in allowed:
            bad.append(link)
        elif probs:
            cloaked.append((link, "; ".join(probs)))
    return bad, cloaked
