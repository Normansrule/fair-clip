"""Scrub secrets from every log line, whatever code path produced it."""

from __future__ import annotations

import logging
import re

PATTERNS = [
    re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"),  # Google OAuth access token
    re.compile(r"1//[0-9A-Za-z_\-]{10,}"),  # Google OAuth refresh token
    re.compile(r"1%2F%2F[0-9A-Za-z_\-%]{10,}", re.I),  # ...URL-encoded
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]{10,}"),  # Google OAuth client secret
    re.compile(r"(?i)(bearer\s+)[0-9A-Za-z._\-]{10,}"),
    re.compile(r"(?i)((?:refresh_token|access_token|client_secret|code_verifier|password|token)[\"'=:\s]+)[^\s\"',}&]{6,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # common LLM API key shape
]


def redact(text: str) -> str:
    for p in PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:  # tracebacks can carry tokens in exception messages too
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install() -> None:
    root = logging.getLogger()
    for h in root.handlers or [logging.StreamHandler()]:
        if h not in root.handlers:
            root.addHandler(h)
        h.addFilter(RedactingFilter())
    # Library loggers with their own handlers (uvicorn, httpx, google) get the filter directly.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "googleapiclient", "google_auth_oauthlib"):
        logging.getLogger(name).addFilter(RedactingFilter())
