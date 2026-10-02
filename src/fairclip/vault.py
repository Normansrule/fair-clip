"""Where the YouTube refresh token lives. Never in the repo, never in a log, never in plain text.

Preferred: the operating-system keychain (macOS Keychain, Windows Credential Manager,
Secret Service on Linux) via the optional ``keyring`` package.

Fallback: an encrypted file. The key is derived from a passphrase in the environment
variable FAIRCLIP_VAULT_PASSPHRASE with scrypt (n=2^15, r=8, p=1), and the token is sealed
with AES-GCM (authenticated, so tampering is detected). Without the passphrase the file is
useless. Put the passphrase in a secrets manager or your shell's keychain helper, not in .env.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

SERVICE = "fair-clip"
PASS_ENV = "FAIRCLIP_VAULT_PASSPHRASE"  # noqa: S105 - env var name


class VaultError(RuntimeError):
    pass


def _keyring():  # pragma: no cover - depends on the host OS
    try:
        import keyring
        from keyring.backends import fail

        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - any import/backend failure means "no keychain"
        return None


def passphrase_from_env() -> str | None:
    """FAIRCLIP_VAULT_PASSPHRASE, or the contents of the file named by ..._FILE (Docker secrets)."""
    f = os.environ.get(PASS_ENV + "_FILE")
    if f and Path(f).exists():
        return Path(f).read_text().strip()
    return os.environ.get(PASS_ENV)


class EncryptedFileVault:
    def __init__(self, path: Path, passphrase: str | None = None) -> None:
        self.path = path
        self.passphrase = passphrase if passphrase is not None else passphrase_from_env()

    def _key(self, salt: bytes) -> bytes:
        if not self.passphrase or len(self.passphrase) < 12:
            raise VaultError(f"set {PASS_ENV} (12+ characters) to use the encrypted-file vault")
        return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(self.passphrase.encode())

    def save(self, name: str, secret: dict[str, Any]) -> None:
        data = self._load_all()
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = AESGCM(self._key(salt)).encrypt(nonce, json.dumps(secret).encode(), name.encode())
        data[name] = {k: base64.b64encode(v).decode() for k, v in {"salt": salt, "nonce": nonce, "ct": ct}.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def load(self, name: str) -> dict[str, Any] | None:
        entry = self._load_all().get(name)
        if not entry:
            return None
        raw = {k: base64.b64decode(v) for k, v in entry.items()}
        try:
            pt = AESGCM(self._key(raw["salt"])).decrypt(raw["nonce"], raw["ct"], name.encode())
        except Exception as exc:  # noqa: BLE001
            raise VaultError("could not decrypt token (wrong passphrase or tampered file)") from exc
        return json.loads(pt)

    def delete(self, name: str) -> None:
        data = self._load_all()
        if data.pop(name, None) is not None:
            self.path.write_text(json.dumps(data))

    def _load_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())


class KeychainVault:  # pragma: no cover - needs a real OS keychain
    def __init__(self, kr: Any) -> None:
        self.kr = kr

    def save(self, name: str, secret: dict[str, Any]) -> None:
        self.kr.set_password(SERVICE, name, json.dumps(secret))

    def load(self, name: str) -> dict[str, Any] | None:
        v = self.kr.get_password(SERVICE, name)
        return json.loads(v) if v else None

    def delete(self, name: str) -> None:
        with contextlib.suppress(Exception):  # already gone is fine
            self.kr.delete_password(SERVICE, name)


def open_vault(data_dir: Path, prefer_keychain: bool = True) -> KeychainVault | EncryptedFileVault:
    kr = _keyring() if prefer_keychain else None
    if kr is not None:
        return KeychainVault(kr)
    return EncryptedFileVault(data_dir / "vault.json.enc")
