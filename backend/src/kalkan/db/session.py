"""Database sessions per role (architecture.md §17).

- :meth:`Database.api_session` connects as kalkan_api and binds the transaction to one
  tenant; row level security hides every other tenant's rows.
- :meth:`Database.api_preauth_session` is for requests that start before the tenant is known
  (login, tokens, webhooks). It only exposes the SECURITY DEFINER resolvers and the global
  tables kalkan_api may insert into.
- :meth:`Database.worker_session` connects as kalkan_worker (workers, scheduler, ops, CLI).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Any

from sqlalchemy import Connection, Engine, insert, text
from sqlalchemy.orm import Session, sessionmaker

from kalkan.config import ConfigError, Settings, get_settings
from kalkan.db.base import new_id, utcnow
from kalkan.db.engine import create_db_engine
from kalkan.db.models import AuditLog, AuthAttempt, JobOutbox, WebhookEvent
from kalkan.domain.enums import AuthScope, WebhookSource


@dataclass(frozen=True)
class ResolvedUser:
    user_id: uuid.UUID
    customer_id: uuid.UUID


@dataclass(frozen=True)
class ResolvedSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    customer_id: uuid.UUID


@dataclass(frozen=True)
class ResolvedInvitation:
    invitation_id: uuid.UUID
    customer_id: uuid.UUID


@dataclass(frozen=True)
class ResolvedPasswordReset:
    reset_id: uuid.UUID
    user_id: uuid.UUID
    customer_id: uuid.UUID


@dataclass(frozen=True)
class ResolvedEvidenceLink:
    link_id: uuid.UUID
    case_id: uuid.UUID
    customer_id: uuid.UUID


@dataclass(frozen=True)
class ResolvedReport:
    report_id: uuid.UUID
    customer_id: uuid.UUID


@dataclass(frozen=True)
class RecentFailures:
    by_email: int
    by_ip: int


class PreauthSession:
    """Calls available to kalkan_api before the tenant is known.

    The resolvers return identifiers only; read everything else in an
    :meth:`Database.api_session` for the returned customer.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def _one(self, sql: str, **params: Any) -> Any:
        return self._connection.execute(text(sql), params).one_or_none()

    def lookup_user(self, email: str) -> ResolvedUser | None:
        row = self._one("SELECT * FROM auth_lookup_user(:email)", email=email)
        return ResolvedUser(row.user_id, row.customer_id) if row else None

    def resolve_session(self, token_hash: str) -> ResolvedSession | None:
        row = self._one("SELECT * FROM auth_resolve_session(:h)", h=token_hash)
        return ResolvedSession(row.session_id, row.user_id, row.customer_id) if row else None

    def resolve_invitation(self, token_hash: str) -> ResolvedInvitation | None:
        row = self._one("SELECT * FROM auth_resolve_invitation(:h)", h=token_hash)
        return ResolvedInvitation(row.invitation_id, row.customer_id) if row else None

    def resolve_password_reset(self, token_hash: str) -> ResolvedPasswordReset | None:
        row = self._one("SELECT * FROM auth_resolve_password_reset(:h)", h=token_hash)
        return ResolvedPasswordReset(row.reset_id, row.user_id, row.customer_id) if row else None

    def resolve_evidence_link(self, token_hash: str) -> ResolvedEvidenceLink | None:
        row = self._one("SELECT * FROM resolve_evidence_link(:h)", h=token_hash)
        return ResolvedEvidenceLink(row.link_id, row.case_id, row.customer_id) if row else None

    def resolve_report_token(self, token_hash: str) -> ResolvedReport | None:
        """Single use: a resolved token is cleared in the same call."""
        row = self._one("SELECT * FROM resolve_report_token(:h)", h=token_hash)
        return ResolvedReport(row.report_id, row.customer_id) if row else None

    def recent_failures(self, email: str, ip: str, since: datetime) -> RecentFailures:
        row = self._one(
            "SELECT * FROM auth_recent_failures(:email, CAST(:ip AS inet), :since)",
            email=email,
            ip=ip,
            since=since,
        )
        return RecentFailures(row.email_failures, row.ip_failures)

    # Inserts into tables kalkan_api cannot read: every value is set here and no RETURNING
    # clause is used.

    def record_auth_attempt(
        self, *, scope: AuthScope, email: str | None, ip: str | None, success: bool
    ) -> None:
        self._connection.execute(
            insert(AuthAttempt).values(
                id=new_id(), scope=scope, email=email, ip=ip, at=utcnow(), success=success
            )
        )

    def write_audit(
        self,
        *,
        actor: str,
        action: str,
        customer_id: uuid.UUID | None = None,
        target: str | None = None,
        data: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        self._connection.execute(
            insert(AuditLog).values(
                id=new_id(),
                at=utcnow(),
                actor=actor,
                customer_id=customer_id,
                action=action,
                target=target,
                data=data or {},
                ip=ip,
            )
        )

    def enqueue_job(self, kind: str, payload: dict[str, Any]) -> None:
        self._connection.execute(
            insert(JobOutbox).values(
                id=new_id(), kind=kind, payload=payload, created_at=utcnow(), attempts=0
            )
        )

    def store_webhook_event(
        self, *, source: WebhookSource, payload: dict[str, Any], signature_ok: bool
    ) -> uuid.UUID:
        event_id = new_id()
        self._connection.execute(
            insert(WebhookEvent).values(
                id=event_id,
                source=source,
                payload=payload,
                signature_ok=signature_ok,
                received_at=utcnow(),
            )
        )
        return event_id


class Database:
    """Engines and sessions for the three database roles."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _engine(self, name: str) -> Engine:
        url = getattr(self._settings, name)
        if url is None:
            raise ConfigError(f"{name.upper()} is not set")
        engine: Engine = create_db_engine(url)
        return engine

    @cached_property
    def api_engine(self) -> Engine:
        return self._engine("database_url_api")

    @cached_property
    def worker_engine(self) -> Engine:
        return self._engine("database_url_worker")

    @contextmanager
    def api_session(self, customer_id: uuid.UUID) -> Iterator[Session]:
        """One transaction as kalkan_api, limited to ``customer_id`` by row level security."""
        if not isinstance(customer_id, uuid.UUID):
            raise TypeError("customer_id must be a UUID")
        with Session(self.api_engine, expire_on_commit=False) as session, session.begin():
            # set_config(..., true) is SET LOCAL: it ends with the transaction.
            session.execute(
                text("SELECT set_config('app.customer_id', :customer_id, true)"),
                {"customer_id": str(customer_id)},
            )
            yield session

    @contextmanager
    def api_preauth_session(self) -> Iterator[PreauthSession]:
        """One transaction as kalkan_api without a tenant (§17 pre-tenant resolvers)."""
        with self.api_engine.connect() as connection, connection.begin():
            yield PreauthSession(connection)

    @contextmanager
    def worker_session(self) -> Iterator[Session]:
        """One transaction as kalkan_worker; commits on success, rolls back on error."""
        with self.worker_sessionmaker() as session, session.begin():
            yield session

    @cached_property
    def worker_sessionmaker(self) -> sessionmaker[Session]:
        return sessionmaker(self.worker_engine, expire_on_commit=False)

    def dispose(self) -> None:
        for name in ("api_engine", "worker_engine"):
            engine = self.__dict__.pop(name, None)
            if engine is not None:
                engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    """The process-wide :class:`Database` for :func:`kalkan.config.get_settings`."""
    global _database
    if _database is None:
        _database = Database(get_settings())
    return _database


@contextmanager
def api_session(customer_id: uuid.UUID) -> Iterator[Session]:
    with get_database().api_session(customer_id) as session:
        yield session


@contextmanager
def api_preauth_session() -> Iterator[PreauthSession]:
    with get_database().api_preauth_session() as session:
        yield session


@contextmanager
def worker_session() -> Iterator[Session]:
    with get_database().worker_session() as session:
        yield session
