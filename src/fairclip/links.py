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
import unicodedata
from urllib.parse import parse_qs, urlsplit

# Anything shaped like host.tld counts as a link: viewers can type "evil.ai/free" as easily as a
# clickable URL. Any 2-24 letter TLD matches (new gTLDs appear constantly), except a short list of
# file extensions that would otherwise flag ordinary words like "Node.js" or "clip.mp4".
URL_RE = re.compile(
    r"(?i)(?<![\w@.-])((?:https?://|www\.)[^\s<>\"')\]]+|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,24})(?::\d{2,5})?(?:/[^\s<>\"')\]]*)?)(?![\w-])"
)
NOT_TLDS = {"js", "py", "md", "txt", "json", "mp4", "mp3", "png", "jpg", "jpeg", "gif", "pdf", "csv", "html", "css",
            "ts", "tsx", "jsx", "exe", "zip", "toml", "yaml", "yml", "sh", "wav", "mov", "mkv", "svg", "lock", "e", "g", "i"}
# Spoken or obfuscated links: "evil dot com slash free", "evil[.]com", "evil (dot) com".
_OBFUSCATED = [
    (re.compile(r"(?i)\s*(?:\[\s*\.\s*\]|\(\s*\.\s*\)|\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*"), "."),
    (re.compile(r"(?i)(?<=[a-z0-9])\s+dot\s+(?=[a-z0-9])"), "."),
    (re.compile(r"(?i)(?<=[a-z0-9])\s+slash\s+(?=[a-z0-9])"), "/"),
]


def normalise_for_links(text: str) -> str:
    """Undo common obfuscations and drop invisible characters before looking for links."""
    text = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    for rx, rep in _OBFUSCATED:
        text = rx.sub(rep, text)
    return text
SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "rebrand.ly", "cutt.ly", "is.gd", "buff.ly",
    "shorturl.at", "tiny.cc", "rb.gy", "s.id", "t.ly", "lnkd.in", "linktr.ee", "bl.ink", "short.io",
}
REDIRECT_PARAMS = {"url", "u", "redirect", "redirect_uri", "next", "dest", "destination", "target", "goto", "out"}


def find_links(text: str) -> list[str]:
    out = []
    for m in URL_RE.finditer(normalise_for_links(text)):
        link = m.group(1).rstrip(".,;:!?")
        tld = (m.group(2) or "").lower()
        if not link.lower().startswith(("http", "www.")) and tld in NOT_TLDS:
            continue
        out.append(link)
    return out


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
    if not host.isascii():
        out.append("non-ASCII host can imitate another domain (e.g. a Cyrillic 'о')")
    qs = parse_qs(p.query)  # values come back percent-decoded
    for k, vals in qs.items():
        for v in vals:
            low = v.strip().lower()
            # Any parameter that carries another URL is a redirect, whatever its name or case.
            if low.startswith(("http:", "https:", "//", "\\\\")) or (k.lower() in REDIRECT_PARAMS and "." in low):
                out.append(f"redirect parameter '{k}' points somewhere else (cloaking)")
                break
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
