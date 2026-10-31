"""Passwords, TOTP secrets and opaque tokens (architecture.md §17).

- Passwords: argon2id.
- TOTP secrets: encrypted with Fernet (``TOTP_ENCRYPTION_KEY``) before they are stored.
- Tokens (sessions, invitations, evidence links, ...): 32 random bytes; only the SHA-256 hex
  digest is stored.
"""

from __future__ import annotations

import hashlib
import secrets

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from kalkan.config import ConfigError, Settings

TOKEN_BYTES = 32
RECOVERY_CODE_COUNT = 10
TOTP_ISSUER = "Kalkan"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    """A URL-safe random token for links and cookies. Store only :func:`hash_token`."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_password() -> str:
    return secrets.token_urlsafe(18)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=TOTP_ISSUER)


def new_recovery_codes() -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(RECOVERY_CODE_COUNT)]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


def _fernet(settings: Settings) -> Fernet:
    if settings.totp_encryption_key is None:
        raise ConfigError("TOTP_ENCRYPTION_KEY is required to store TOTP secrets")
    return Fernet(settings.totp_encryption_key.get_secret_value().encode())


def encrypt_totp_secret(settings: Settings, secret: str) -> str:
    return _fernet(settings).encrypt(secret.encode()).decode()


def decrypt_totp_secret(settings: Settings, encrypted: str) -> str:
    return _fernet(settings).decrypt(encrypted.encode()).decode()
