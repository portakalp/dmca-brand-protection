from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from kalkan.config import ConfigError
from kalkan.domain.hostnames import InvalidHostnameError, normalize_hostname
from kalkan.security import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    hash_token,
    new_recovery_codes,
    new_token,
    verify_password,
)
from tests.support import make_settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Mintera.com", "mintera.com"),
        ("  www.mintera.pl. ", "www.mintera.pl"),
        ("partners.mintera-referrals.com", "partners.mintera-referrals.com"),
        ("minter\u0430.com", "xn--minter-8nf.com"),  # Cyrillic a (homoglyph)
        ("xn--minter-8nf.com", "xn--minter-8nf.com"),
        ("stra\u00dfe.de", "xn--strae-oqa.de"),  # IDNA 2008 keeps the sharp s
    ],
)
def test_normalize_hostname(value: str, expected: str) -> None:
    assert normalize_hostname(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "mintera",
        "https://mintera.com",
        "mintera.com/login",
        "*.mintera.com",
        "user@mintera.com",
        "mintera.com:443",
        "-mintera.com",
        "mint_era.com",
        "203.0.113.7",
        "a" * 64 + ".com",
    ],
)
def test_invalid_hostnames(value: str) -> None:
    with pytest.raises(InvalidHostnameError):
        normalize_hostname(value)


def test_passwords_are_argon2id() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong")
    assert not verify_password("not-a-hash", "anything")


def test_tokens_are_random_and_stored_as_hashes() -> None:
    first, second = new_token(), new_token()
    assert first != second
    assert len(first) >= 43
    assert hash_token(first) == hash_token(first)
    assert len(hash_token(first)) == 64
    assert first not in hash_token(first)


def test_recovery_codes() -> None:
    codes = new_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    assert hash_recovery_code(codes[0].upper() + " ") == hash_recovery_code(codes[0])


def test_totp_secrets_are_encrypted() -> None:
    settings = make_settings(env="local", totp_encryption_key=Fernet.generate_key().decode())
    encrypted = encrypt_totp_secret(settings, "JBSWY3DPEHPK3PXP")
    assert "JBSWY3DPEHPK3PXP" not in encrypted
    assert decrypt_totp_secret(settings, encrypted) == "JBSWY3DPEHPK3PXP"


def test_totp_needs_a_key(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="TOTP_ENCRYPTION_KEY"):
        encrypt_totp_secret(make_settings(env="local"), "JBSWY3DPEHPK3PXP")
