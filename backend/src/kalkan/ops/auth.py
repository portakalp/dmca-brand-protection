"""Staff sign-in for the ops panel (architecture.md §19, §24; K117).

Two steps: e-mail and password open a *pending* session, the TOTP code turns it into a full
one with a new token. Nothing but the sign-in pages accepts a pending session.

- Sessions: 32-byte opaque token in the ``kalkan_ops_session`` cookie (HttpOnly, Secure,
  SameSite=Strict); only its SHA-256 is stored in ``staff_sessions``. A full session ends
  after 12 hours without a request and 7 days after sign-in; a pending one after 10 minutes.
- Attempt limit: every failed password or TOTP check is an ``auth_attempts`` row
  (scope=staff). Five failures within 15 minutes for the same e-mail or IP lock that e-mail or
  IP; the lock lasts 15 minutes and doubles with every further five failures of the day.
- CSRF: every POST carries a token, in the ``csrf`` form field or the ``X-CSRF-Token`` header.
  Signed in, it is derived from the session token; on the sign-in form it is the value of a
  random cookie (double submit).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pyotp
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import AuditLog, AuthAttempt, StaffSession, StaffUser
from kalkan.domain.enums import AuthScope
from kalkan.security import (
    decrypt_totp_secret,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)

SESSION_COOKIE = "kalkan_ops_session"
LOGIN_CSRF_COOKIE = "kalkan_ops_login"
IDLE_TIMEOUT = timedelta(hours=12)
SESSION_LIFETIME = timedelta(days=7)
PENDING_LIFETIME = timedelta(minutes=10)
# last_seen_at is written at most this often.
TOUCH_EVERY = timedelta(minutes=1)

FAILURE_WINDOW = timedelta(minutes=15)
MAX_FAILURES = 5
BASE_LOCK = timedelta(minutes=15)
MAX_LOCK = timedelta(hours=24)
LOCK_LOOKBACK = timedelta(hours=24)

# Verified when the e-mail is unknown, so both cases take as long (§17).
_DUMMY_HASH = hash_password("kalkan-ops-dummy-password")


@dataclass(frozen=True)
class Staff:
    id: uuid.UUID
    email: str
    session_id: uuid.UUID
    csrf: str


def csrf_for(token: str) -> str:
    """The CSRF token of a session; only the holder of the session cookie can know it."""
    return hashlib.sha256(f"kalkan-ops-csrf:{token}".encode()).hexdigest()


def same(expected: str | None, given: str | None) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(str(expected), str(given))


# --- attempt limit -----------------------------------------------------------------------------


def _failures(session: Session, column: Any, value: str, now: datetime) -> list[datetime]:
    """Failed staff attempts of the last day for an e-mail or IP, after its last success."""
    last_success = (
        select(func.max(AuthAttempt.at))
        .where(
            AuthAttempt.scope == AuthScope.STAFF,
            column == value,
            AuthAttempt.success.is_(True),
        )
        .scalar_subquery()
    )
    return list(
        session.scalars(
            select(AuthAttempt.at)
            .where(
                AuthAttempt.scope == AuthScope.STAFF,
                column == value,
                AuthAttempt.success.is_(False),
                AuthAttempt.at >= now - LOCK_LOOKBACK,
                AuthAttempt.at > func.coalesce(last_success, now - LOCK_LOOKBACK),
            )
            .order_by(AuthAttempt.at)
        )
    )


def _locked_for(failures: list[datetime], now: datetime) -> timedelta | None:
    recent = [at for at in failures if at >= now - FAILURE_WINDOW]
    if len(recent) < MAX_FAILURES:
        return None
    rounds = len(failures) // MAX_FAILURES
    lock = min(BASE_LOCK * 2 ** (rounds - 1), MAX_LOCK)
    remaining = failures[-1] + lock - now
    return remaining if remaining > timedelta(0) else None


def locked_for(
    session: Session, email: str | None, ip: str | None, now: datetime
) -> timedelta | None:
    """How long sign-in stays locked for this e-mail or IP; ``None`` when it is not."""
    locks = []
    if email:
        locks.append(_locked_for(_failures(session, AuthAttempt.email, email, now), now))
    if ip:
        locks.append(_locked_for(_failures(session, AuthAttempt.ip, ip, now), now))
    found = [lock for lock in locks if lock is not None]
    return max(found) if found else None


def record_attempt(
    session: Session, *, email: str | None, ip: str | None, success: bool, now: datetime
) -> None:
    session.add(AuthAttempt(scope=AuthScope.STAFF, email=email, ip=ip, success=success, at=now))
    session.flush()


# --- the two steps -------------------------------------------------------------------------------


def check_password(session: Session, email: str, password: str) -> StaffUser | None:
    user = session.scalar(select(StaffUser).where(StaffUser.email == email))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or user.disabled_at is not None:
        return None
    return user


def open_session(session: Session, user: StaffUser, *, now: datetime, mfa_passed: bool) -> str:
    """A new session for ``user``; returns the cookie token."""
    token = new_token()
    session.add(
        StaffSession(
            token_hash=hash_token(token),
            staff_user_id=user.id,
            mfa_passed=mfa_passed,
            created_at=now,
            last_seen_at=now,
            expires_at=now + (SESSION_LIFETIME if mfa_passed else PENDING_LIFETIME),
        )
    )
    session.flush()
    return token


def pending_user(session: Session, token: str | None, now: datetime) -> StaffUser | None:
    """The user of a live pending session (between password and TOTP)."""
    if not token:
        return None
    row = session.execute(
        select(StaffSession, StaffUser)
        .join(StaffUser, StaffUser.id == StaffSession.staff_user_id)
        .where(
            StaffSession.token_hash == hash_token(token),
            StaffSession.mfa_passed.is_(False),
            StaffSession.expires_at > now,
            StaffUser.disabled_at.is_(None),
        )
    ).one_or_none()
    return row[1] if row else None


def check_totp(settings: Settings, user: StaffUser, code: str) -> bool:
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    secret = decrypt_totp_secret(settings, user.totp_secret_enc)
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(StaffSession).where(StaffSession.token_hash == hash_token(token)))


def resolve(session: Session, token: str | None, now: datetime) -> Staff | None:
    """The signed-in staff member of a full, live session; touches ``last_seen_at``."""
    if not token:
        return None
    row = session.execute(
        select(StaffSession, StaffUser)
        .join(StaffUser, StaffUser.id == StaffSession.staff_user_id)
        .where(
            StaffSession.token_hash == hash_token(token),
            StaffSession.mfa_passed.is_(True),
            StaffSession.expires_at > now,
            StaffSession.last_seen_at > now - IDLE_TIMEOUT,
            StaffUser.disabled_at.is_(None),
        )
    ).one_or_none()
    if row is None:
        return None
    staff_session, user = row
    if staff_session.last_seen_at < now - TOUCH_EVERY:
        staff_session.last_seen_at = now
    return Staff(user.id, user.email, staff_session.id, csrf_for(token))


def audit(
    session: Session,
    actor: str,
    action: str,
    *,
    ip: str | None,
    target: str | None = None,
    data: dict[str, object] | None = None,
) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, data=data or {}, ip=ip))
