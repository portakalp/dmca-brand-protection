"""Request context of the customer API (architecture.md §17).

Every request with a session cookie is resolved the same way: the opaque token's hash goes to
``auth_resolve_session`` (a SECURITY DEFINER resolver that returns identifiers only), then one
``api_session(customer_id)`` transaction as ``kalkan_api`` serves the whole request under row
level security. The transaction ends when the endpoint returns, before the response is sent
(``scope="function"``), so a failed commit is a failed request; file streams read storage
only.

- ``SignedIn``: a session, with or without the second factor (TOTP set-up and checks).
- ``Member``: a session that passed TOTP; every data endpoint needs it.
- ``Owner``: a member with the owner role.
- ``Writer``: an owner who may write: not the demo workspace, not a suspended
  (``read_only``) account.

A session ends after 12 idle hours or 7 days; one waiting for its second factor after 30
minutes. State-changing requests need ``X-Kalkan-CSRF: 1`` (:func:`csrf_header`).
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from kalkan.api.evidence import client_key
from kalkan.api.problems import ProblemError
from kalkan.config import Settings
from kalkan.db.models import Customer, User, UserSession
from kalkan.db.session import Database
from kalkan.domain.enums import CustomerStatus, UserRole
from kalkan.security import hash_token

SESSION_COOKIE = "kalkan_session"
CSRF_HEADER = "X-Kalkan-CSRF"
IDLE_TIMEOUT = timedelta(hours=12)
SESSION_LIFETIME = timedelta(days=7)
PENDING_LIFETIME = timedelta(minutes=30)
TOUCH_EVERY = timedelta(minutes=1)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
NO_WRITES = frozenset({CustomerStatus.SUSPENDED})


def database(request: Request) -> Database:
    found: Database = request.app.state.database
    return found


def settings(request: Request) -> Settings:
    found: Settings = request.app.state.settings
    return found


def now() -> datetime:
    return datetime.now(UTC)


def client_ip(request: Request) -> str | None:
    try:
        return str(ipaddress.ip_address(client_key(request)))
    except ValueError:
        return None


def csrf_header(request: Request) -> None:
    """State-changing requests carry ``X-Kalkan-CSRF: 1``; a cross-site form cannot."""
    if request.method in UNSAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise ProblemError(403, title="CSRF header missing", code="csrf")


def unauthorized(detail: str = "Sign in again.", code: str = "unauthorized") -> ProblemError:
    return ProblemError(401, title="Unauthorized", detail=detail, code=code)


@dataclass
class Context:
    session: Session  # kalkan_api, bound to the customer by RLS
    login: UserSession
    user: User
    customer: Customer
    at: datetime

    @property
    def customer_id(self) -> uuid.UUID:
        return self.customer.id

    @property
    def is_owner(self) -> bool:
        return self.user.role is UserRole.OWNER

    @property
    def read_only(self) -> bool:
        return self.customer.is_demo or self.customer.status in NO_WRITES

    @property
    def tz(self) -> ZoneInfo:
        return customer_tz(self.customer)


def customer_tz(customer: Customer) -> ZoneInfo:
    try:
        return ZoneInfo(customer.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def ended(login: UserSession, at: datetime) -> bool:
    if login.expires_at <= at:
        return True
    return login.mfa_passed and login.last_seen_at <= at - IDLE_TIMEOUT


def _context(request: Request) -> Iterator[Context]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        raise unauthorized()
    db = database(request)
    with db.api_preauth_session() as preauth:
        resolved = preauth.resolve_session(hash_token(token))
    if resolved is None:
        raise unauthorized()
    at = now()
    gone = False
    with db.api_session(resolved.customer_id) as session:
        login = session.get(UserSession, resolved.session_id)
        user = session.get(User, resolved.user_id)
        customer = session.get(Customer, resolved.customer_id)
        if login is None or user is None or customer is None:
            gone = True
        elif user.disabled_at is not None or ended(login, at):
            session.delete(login)
            gone = True
        else:
            if login.last_seen_at <= at - TOUCH_EVERY:
                login.last_seen_at = at
            yield Context(session, login, user, customer, at)
    if gone:
        raise unauthorized()


SignedIn = Annotated[Context, Depends(_context, scope="function")]


def _member(context: SignedIn) -> Context:
    if not context.login.mfa_passed:
        raise unauthorized("Two-step verification is required.", "mfa_required")
    return context


Member = Annotated[Context, Depends(_member)]


def _owner(context: Member) -> Context:
    if not context.is_owner:
        raise ProblemError(403, title="Forbidden", detail="Only owners can do this.", code="role")
    return context


Owner = Annotated[Context, Depends(_owner)]


def _writer(context: Owner) -> Context:
    if context.customer.is_demo:
        raise ProblemError(
            403, title="Read-only workspace", detail="The demo is read-only.", code="read_only"
        )
    if context.read_only:
        raise ProblemError(
            403,
            title="Read-only account",
            detail="The account is suspended and read-only.",
            code="read_only",
        )
    return context


Writer = Annotated[Context, Depends(_writer)]
