"""Stage 9: talking to YouTube (OAuth 2.0 + Data API v3). Needs `pip install '.[youtube]'`.

Security choices, each tied to Google's own guidance (docs/POLICY.md, docs/SECURITY_MODEL.md):
* OAuth for installed apps: loopback redirect on 127.0.0.1, PKCE (S256) and a `state` check,
  so a stolen authorization code is useless.
* One scope: ``youtube.upload``. We upload only *after* human approval and set privacy and
  ``publishAt`` in the same insert call, so we never need the broad ``youtube`` scope that
  videos.update requires.
* Refresh token in the OS keychain or the encrypted vault (vault.py); never logged.
* Revocation from the dashboard calls Google's revoke endpoint, then deletes the local copy.
"""

from __future__ import annotations

import contextlib
import json
import random
import time
from pathlib import Path
from typing import Any

import httpx

from ..redact import redact
from ..vault import EncryptedFileVault, KeychainVault

TOKEN_NAME = "youtube-oauth"  # noqa: S105 - vault entry name, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
RETRYABLE_STATUS = {500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}


class QuotaError(RuntimeError):
    """YouTube said 'not today'. The clip is deferred, not failed."""


class PublishError(RuntimeError):
    pass


def authorize(client_file: Path, scopes: list[str], vault: KeychainVault | EncryptedFileVault) -> dict[str, Any]:  # pragma: no cover - interactive
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_file.exists():
        raise PublishError(f"OAuth client file {client_file} not found (see docs/SETUP_YOUTUBE.md)")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes=scopes, autogenerate_code_verifier=True)
    creds = flow.run_local_server(host="127.0.0.1", bind_addr="127.0.0.1", port=0, open_browser=True,
                                  authorization_prompt_message="Opening Google sign-in in your browser...",
                                  success_message="clipwarden is authorized. You can close this tab.")
    granted = set(creds.scopes or scopes)
    if granted - set(scopes):
        raise PublishError(f"Google granted extra scopes {granted - set(scopes)}; refusing to store them")
    record = {"refresh_token": creds.refresh_token, "scopes": sorted(granted), "obtained": int(time.time())}
    vault.save(TOKEN_NAME, record)
    return {"scopes": record["scopes"], "obtained": record["obtained"]}


def credentials(client_file: Path, vault: KeychainVault | EncryptedFileVault):  # pragma: no cover - needs Google libs
    from google.oauth2.credentials import Credentials

    rec = vault.load(TOKEN_NAME)
    if not rec:
        raise PublishError("not authorized: run `clipwarden auth login`")
    client = json.loads(client_file.read_text())
    c = client.get("installed") or client.get("web") or {}
    return Credentials(None, refresh_token=rec["refresh_token"], token_uri=c.get("token_uri", "https://oauth2.googleapis.com/token"),
                       client_id=c["client_id"], client_secret=c.get("client_secret"), scopes=rec["scopes"])


def revoke(vault: KeychainVault | EncryptedFileVault, http: httpx.Client | None = None) -> bool:
    rec = vault.load(TOKEN_NAME)
    ok = True
    if rec and rec.get("refresh_token"):
        client = http or httpx.Client(timeout=15)
        r = client.post(REVOKE_URL, params={"token": rec["refresh_token"]},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        ok = r.status_code == 200
    vault.delete(TOKEN_NAME)  # delete locally even if Google already considered it revoked
    return ok


def _error_reason(exc: Exception) -> tuple[int, str]:
    status = getattr(getattr(exc, "resp", None), "status", 0)
    reason = ""
    with contextlib.suppress(Exception):  # an unparseable error body just means "no reason given"
        content = json.loads(getattr(exc, "content", b"{}") or b"{}")
        errs = content.get("error", {}).get("errors", [])
        reason = errs[0].get("reason", "") if errs else content.get("error", {}).get("status", "")
    return int(status or 0), reason


def upload(youtube: Any, body: dict[str, Any], file: Path, *, max_retries: int = 6,
           sleep=time.sleep, media_factory=None) -> str:
    """Resumable upload with exponential backoff. Returns the new video ID.

    `youtube` is a googleapiclient Resource (injected so tests can pass a fake).
    """
    if media_factory is None:  # pragma: no cover - real library path
        from googleapiclient.http import MediaFileUpload

        def media_factory(p: Path):
            return MediaFileUpload(str(p), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)

    parts = ",".join(k for k in ("snippet", "status") if k in body)
    request = youtube.videos().insert(part=parts, body=body, media_body=media_factory(file), notifySubscribers=False)
    response, retries = None, 0
    while response is None:
        try:
            _status, response = request.next_chunk()
        except Exception as exc:  # noqa: BLE001 - classify below
            code, reason = _error_reason(exc)
            if code == 403 and reason in QUOTA_REASONS or code == 429:
                raise QuotaError(reason or "quota") from exc
            if code in RETRYABLE_STATUS or isinstance(exc, OSError):
                retries += 1
                if retries > max_retries:
                    raise PublishError(f"upload failed after {max_retries} retries: {redact(str(exc))}") from exc
                sleep(min(64.0, 2 ** retries) + random.random())  # noqa: S311 - jitter, not crypto
                continue
            raise PublishError(f"upload rejected ({code} {reason}): {redact(str(exc))[:300]}") from exc
    if "id" not in response:
        raise PublishError(f"unexpected upload response: {str(response)[:200]}")
    return str(response["id"])
