"""Dashboard security: localhost-only, password login, signed sessions, CSRF tokens, strict headers.

Defences and what they stop:
* Host-header allowlist -> DNS-rebinding (a web page making your browser talk to 127.0.0.1).
* Password (scrypt-hashed) + signed, HttpOnly, SameSite=Strict session cookie -> other local
  users/processes and drive-by requests.
* Per-session CSRF token on every POST, plus an Origin check -> cross-site request forgery.
* Content-Security-Policy with no inline script, frame-ancestors 'none' -> XSS and clickjacking.
* Login throttling -> password guessing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..store import Store

SESSION_COOKIE = "cw_session"
SESSION_MAX_AGE = 12 * 3600
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin (not no-referrer): with no-referrer, browsers send "Origin: null" on form POSTs,
    # which would defeat our Origin check. same-origin still sends nothing to other sites.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}


def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def set_password(store: Store, pw: str) -> None:
    if len(pw) < 12:
        raise ValueError("password must be at least 12 characters")
    salt = os.urandom(16)
    store.set("dashboard_pw", {"salt": salt.hex(), "hash": _scrypt(pw, salt).hex()})


def check_password(store: Store, pw: str) -> bool:
    rec = store.get("dashboard_pw")
    if not rec:
        return False
    return hmac.compare_digest(_scrypt(pw, bytes.fromhex(rec["salt"])).hex(), rec["hash"])


def ensure_password(store: Store) -> str | None:
    """First run: create a random password and return it once so the CLI can print it."""
    if store.get("dashboard_pw"):
        return None
    pw = secrets.token_urlsafe(15)
    set_password(store, pw)
    store.audit.append("system", "set_password", "dashboard", {"generated": True})
    return pw


def session_key(data_dir: Path) -> bytes:
    p = data_dir / "session.key"
    if not p.exists():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    return p.read_bytes()


class Sessions:
    def __init__(self, key: bytes) -> None:
        self.s = URLSafeTimedSerializer(key, salt="clipwarden-session")

    def issue(self, user: str) -> str:
        return self.s.dumps({"u": user, "csrf": secrets.token_urlsafe(24), "t": int(time.time())})

    def read(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            return self.s.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None


class Throttle:
    """At most `limit` failed logins per `window` seconds per client, then a lockout."""

    def __init__(self, limit: int = 5, window: int = 900) -> None:
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = defaultdict(list)

    def blocked(self, who: str) -> bool:
        now = time.monotonic()
        self.fails[who] = [t for t in self.fails[who] if now - t < self.window]
        return len(self.fails[who]) >= self.limit

    def fail(self, who: str) -> None:
        self.fails[who].append(time.monotonic())

    def reset(self, who: str) -> None:
        self.fails.pop(who, None)


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "testserver"}


def csrf_ok(session: dict | None, token: str | None) -> bool:
    return bool(session and token and hmac.compare_digest(session.get("csrf", ""), token))
