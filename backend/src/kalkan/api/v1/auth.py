"""Sign-in, the second factor, password resets and invitations (architecture.md §17).

- ``POST /auth/login``: e-mail and password. An unknown e-mail checks a dummy hash, so the
  answer and its timing are the same. Success opens a session waiting for its second factor.
- ``POST /auth/mfa``: a TOTP code or one of the recovery codes; the session gets a new token.
- ``POST /auth/totp/setup`` and ``/auth/totp/verify``: the first sign-in sets TOTP up; the
  verification turns it on, returns 10 single-use recovery codes and passes the session.
- ``POST /auth/recovery-codes``: new codes, for a current TOTP code.
- ``POST /auth/logout``.
- ``POST /auth/password/forgot`` (always 202) and ``/auth/password/reset``: a single-use link,
  valid 24 hours; a reset ends every session of the user.
- ``POST /invitations/accept``: sets the password, then TOTP is set up like a first sign-in.

Failed passwords and codes go to ``auth_attempts`` (scope ``customer``); ``auth_recent_failures``
counts them. Five failures in 15 minutes for an e-mail or an IP lock sign-in for 15 minutes,
doubling with every further five in a day, at most 24 hours (K149).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pyotp
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kalkan.api.problems import ProblemError
from kalkan.api.v1.deps import (
    PENDING_LIFETIME,
    SESSION_COOKIE,
    SESSION_LIFETIME,
    Context,
    Member,
    SignedIn,
    client_ip,
    database,
    now,
    settings,
)
from kalkan.api.v1.mail import send_mail
from kalkan.api.v1.types import EmailAddress
from kalkan.db.models import Customer, Invitation, PasswordReset, User, UserSession
from kalkan.db.session import Database
from kalkan.domain.enums import AuthScope
from kalkan.email.messages import password_reset_email
from kalkan.logging import get_logger
from kalkan.security import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    hash_token,
    new_recovery_codes,
    new_token,
    new_totp_secret,
    totp_uri,
    verify_password,
)

router = APIRouter(tags=["auth"])
LINK_LIFETIME = timedelta(hours=24)
FAILURE_WINDOW = timedelta(minutes=15)
LOCK_LOOKBACK = timedelta(hours=24)
MAX_FAILURES = 5
BASE_LOCK = timedelta(minutes=15)
MAX_LOCK = timedelta(hours=24)
RESETS_PER_HOUR = 3
_DUMMY_HASH = hash_password("kalkan-customer-dummy-password")

log = get_logger(__name__)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailAddress
    password: str = Field(min_length=1, max_length=256)


class Code(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=40)


class NextStep(BaseModel):
    mfa: str = Field(description="'setup' (TOTP to set up first) or 'verify'")


class TotpSetup(BaseModel):
    secret: str
    otpauth_uri: str


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]


class Forgot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailAddress


class NewPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=12, max_length=256)


class Acceptance(NewPassword):
    display_name: str | None = Field(default=None, max_length=120)


class Accepted(BaseModel):
    status: str = "accepted"


# --- sessions --------------------------------------------------------------------------------


def set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        # Plain-HTTP local runs could not keep a Secure cookie (K149).
        secure=settings(request).env != "local",
        samesite="lax",
        path="/",
    )


def open_session(
    session: Session, request: Request, user: User, *, mfa_passed: bool, at: datetime
) -> str:
    token = new_token()
    session.add(
        UserSession(
            customer_id=user.customer_id,
            token_hash=hash_token(token),
            user_id=user.id,
            mfa_passed=mfa_passed,
            created_at=at,
            last_seen_at=at,
            expires_at=at + (SESSION_LIFETIME if mfa_passed else PENDING_LIFETIME),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        )
    )
    session.flush()
    return token


def _pass(context: Context, request: Request, response: Response) -> None:
    """The second factor passed: a new token replaces the waiting session's."""
    token = open_session(context.session, request, context.user, mfa_passed=True, at=context.at)
    context.session.delete(context.login)
    context.user.last_login_at = context.at
    set_cookie(request, response, token)


# --- failures and locks ------------------------------------------------------------------------


def _lock(db: Database, email: str | None, ip: str | None, at: datetime) -> timedelta | None:
    with db.api_preauth_session() as preauth:
        recent = preauth.recent_failures(email or "", ip or "0.0.0.0", at - FAILURE_WINDOW)  # noqa: S104
        if max(recent.by_email, recent.by_ip) < MAX_FAILURES:
            return None
        day = preauth.recent_failures(email or "", ip or "0.0.0.0", at - LOCK_LOOKBACK)  # noqa: S104
    rounds = max(day.by_email, day.by_ip) // MAX_FAILURES
    lock: timedelta = BASE_LOCK * (2 ** max(rounds - 1, 0))
    return min(lock, MAX_LOCK)


def _check_lock(db: Database, email: str | None, ip: str | None, at: datetime) -> None:
    lock = _lock(db, email, ip, at)
    if lock is not None:
        raise ProblemError(
            429,
            title="Too many attempts",
            detail="Try again later.",
            code="locked",
            retry_after=int(lock.total_seconds()),
        )


def _record(db: Database, email: str | None, ip: str | None, *, success: bool) -> None:
    with db.api_preauth_session() as preauth:
        preauth.record_auth_attempt(scope=AuthScope.CUSTOMER, email=email, ip=ip, success=success)


def _totp_ok(request: Request, user: User, code: str) -> bool:
    code = code.strip().replace(" ", "")
    if user.totp_secret_enc is None or not code.isdigit() or len(code) != 6:
        return False
    secret = decrypt_totp_secret(settings(request), user.totp_secret_enc)
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def _recovery_ok(user: User, code: str) -> bool:
    digest = hash_recovery_code(code)
    if digest not in user.recovery_codes_hash:
        return False
    user.recovery_codes_hash = [item for item in user.recovery_codes_hash if item != digest]
    return True


# --- the endpoints ---------------------------------------------------------------------------


@router.post("/auth/login", response_model=NextStep)
def login(body: Credentials, request: Request, response: Response) -> NextStep:
    db, ip, at = database(request), client_ip(request), now()
    email = str(body.email).lower()
    _check_lock(db, email, ip, at)
    with db.api_preauth_session() as preauth:
        found = preauth.lookup_user(email)
    user: User | None = None
    token, setup = "", False
    if found is None:
        verify_password(_DUMMY_HASH, body.password)
    else:
        with db.api_session(found.customer_id) as session:
            row = session.get(User, found.user_id)
            if row is not None and verify_password(row.password_hash, body.password):
                user = row
                token = open_session(session, request, row, mfa_passed=False, at=at)
                setup = row.totp_enabled_at is None
    _record(db, email, ip, success=user is not None)
    if user is None:
        raise ProblemError(
            401, title="Unauthorized", detail="Wrong e-mail or password.", code="credentials"
        )
    set_cookie(request, response, token)
    return NextStep(mfa="setup" if setup else "verify")


@router.post("/auth/mfa", response_model=None, status_code=204)
def mfa(body: Code, context: SignedIn, request: Request, response: Response) -> None:
    db, ip = database(request), client_ip(request)
    if context.login.mfa_passed:
        return
    if context.user.totp_enabled_at is None:
        raise ProblemError(409, title="Set up TOTP first", code="setup_required")
    _check_lock(db, context.user.email, ip, context.at)
    if not (_totp_ok(request, context.user, body.code) or _recovery_ok(context.user, body.code)):
        _record(db, context.user.email, ip, success=False)
        raise ProblemError(401, title="Unauthorized", detail="Wrong code.", code="code")
    _record(db, context.user.email, ip, success=True)
    _pass(context, request, response)


@router.post("/auth/totp/setup", response_model=TotpSetup)
def totp_setup(context: SignedIn, request: Request) -> TotpSetup:
    if context.user.totp_enabled_at is not None:
        raise ProblemError(409, title="TOTP is already on", code="totp_enabled")
    secret = new_totp_secret()
    context.user.totp_secret_enc = encrypt_totp_secret(settings(request), secret)
    return TotpSetup(secret=secret, otpauth_uri=totp_uri(secret, context.user.email))


@router.post("/auth/totp/verify", response_model=RecoveryCodes)
def totp_verify(
    body: Code, context: SignedIn, request: Request, response: Response
) -> RecoveryCodes:
    if context.user.totp_enabled_at is not None:
        raise ProblemError(409, title="TOTP is already on", code="totp_enabled")
    if not _totp_ok(request, context.user, body.code):
        _record(database(request), context.user.email, client_ip(request), success=False)
        raise ProblemError(401, title="Unauthorized", detail="Wrong code.", code="code")
    codes = new_recovery_codes()
    context.user.totp_enabled_at = context.at
    context.user.recovery_codes_hash = [hash_recovery_code(code) for code in codes]
    if not context.login.mfa_passed:
        _pass(context, request, response)
    return RecoveryCodes(recovery_codes=codes)


@router.post("/auth/recovery-codes", response_model=RecoveryCodes)
def recovery_codes(body: Code, context: Member, request: Request) -> RecoveryCodes:
    if not _totp_ok(request, context.user, body.code):
        _record(database(request), context.user.email, client_ip(request), success=False)
        raise ProblemError(401, title="Unauthorized", detail="Wrong code.", code="code")
    codes = new_recovery_codes()
    context.user.recovery_codes_hash = [hash_recovery_code(code) for code in codes]
    return RecoveryCodes(recovery_codes=codes)


@router.post("/auth/logout", response_model=None, status_code=204)
def logout(context: SignedIn, response: Response) -> None:
    context.session.delete(context.login)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/auth/password/forgot", response_model=None, status_code=202)
def forgot(body: Forgot, request: Request) -> None:
    """Always 202, so the answer does not tell whether the address has an account."""
    db, at = database(request), now()
    email = str(body.email).lower()
    with db.api_preauth_session() as preauth:
        found = preauth.lookup_user(email)
    if found is None:
        return
    token = new_token()
    with db.api_session(found.customer_id) as session:
        recent = session.scalar(
            select(func.count())
            .select_from(PasswordReset)
            .where(
                PasswordReset.user_id == found.user_id,
                PasswordReset.created_at >= at - timedelta(hours=1),
            )
        )
        if (recent or 0) >= RESETS_PER_HOUR:
            log.info("auth.reset_throttled", user_id=str(found.user_id))
            return
        session.add(
            PasswordReset(
                customer_id=found.customer_id,
                user_id=found.user_id,
                token_hash=hash_token(token),
                expires_at=at + LINK_LIFETIME,
                created_at=at,
            )
        )
    url = f"{settings(request).app_base_url or ''}/reset-password?token={token}"
    send_mail(request, password_reset_email(email, url=url))


@router.post("/auth/password/reset", response_model=None, status_code=204)
def reset(body: NewPassword, request: Request) -> None:
    db, at = database(request), now()
    with db.api_preauth_session() as preauth:
        found = preauth.resolve_password_reset(hash_token(body.token))
    if found is None:
        raise ProblemError(400, title="The link is not valid", code="token")
    with db.api_session(found.customer_id) as session:
        row = session.get(PasswordReset, found.reset_id, with_for_update=True)
        user = session.get(User, found.user_id)
        if row is None or row.used_at is not None or user is None:
            raise ProblemError(400, title="The link is not valid", code="token")
        row.used_at = at
        user.password_hash = hash_password(body.password)
        session.execute(delete(UserSession).where(UserSession.user_id == user.id))


@router.post("/invitations/accept", response_model=NextStep)
def accept(body: Acceptance, request: Request, response: Response) -> NextStep:
    db, at = database(request), now()
    with db.api_preauth_session() as preauth:
        found = preauth.resolve_invitation(hash_token(body.token))
    if found is None:
        raise ProblemError(400, title="The invitation is not valid", code="token")
    try:
        with db.api_session(found.customer_id) as session:
            invitation = session.get(Invitation, found.invitation_id, with_for_update=True)
            customer = session.get(Customer, found.customer_id)
            if invitation is None or invitation.accepted_at is not None or customer is None:
                raise ProblemError(400, title="The invitation is not valid", code="token")
            invitation.accepted_at = at
            user = User(
                customer_id=found.customer_id,
                email=invitation.email.lower(),
                display_name=body.display_name,
                password_hash=hash_password(body.password),
                role=invitation.role,
                created_at=at,
                updated_at=at,
            )
            session.add(user)
            session.flush()
            token = open_session(session, request, user, mfa_passed=False, at=at)
    except IntegrityError:
        raise ProblemError(409, title="This e-mail already has an account", code="exists") from None
    set_cookie(request, response, token)
    return NextStep(mfa="setup")
