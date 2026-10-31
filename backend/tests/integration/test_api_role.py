"""What kalkan_api may do outside tenant tables (§5, §17)."""

from __future__ import annotations

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from kalkan.db.models import AuditLog, Customer, JobOutbox
from kalkan.db.session import Database
from kalkan.domain.enums import AuthScope, WebhookSource
from tests.factories import customer

INSUFFICIENT_PRIVILEGE = "42501"
INSERT_ONLY = ("jobs_outbox", "audit_log", "auth_attempts", "webhook_events")
NOT_READABLE = (
    *INSERT_ONLY,
    "staff_users",
    "staff_sessions",
    "settings",
    "labels",
    "ai_calls",
    "channel_contacts",
)
READABLE = ("domains", "domain_sightings")


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def test_preauth_inserts_without_returning(database: Database) -> None:
    with database.api_preauth_session() as session:
        session.record_auth_attempt(
            scope=AuthScope.CUSTOMER, email="x@example.com", ip="203.0.113.7", success=False
        )
        session.write_audit(actor="api", action="login.failed", ip="203.0.113.7")
        session.enqueue_job("whitelist_changed", {"customer_id": "c"})
        session.store_webhook_event(
            source=WebhookSource.POSTMARK_INBOUND, payload={"x": 1}, signature_ok=True
        )
    with database.worker_session() as session:
        for table in INSERT_ONLY:
            count = session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
            assert count == 1, table


def test_orm_inserts_into_insert_only_tables_work_in_a_tenant_session(
    database: Database,
) -> None:
    with database.worker_session() as session:
        owner = customer(session)
    with database.api_session(owner.id) as session:
        session.add(
            AuditLog(actor="owner@example.com", action="whitelist.add", customer_id=owner.id)
        )
        session.add(JobOutbox(kind="whitelist_changed", payload={"customer_id": str(owner.id)}))


@pytest.mark.parametrize("table", NOT_READABLE)
def test_api_cannot_read_global_tables(database: Database, table: str) -> None:
    with pytest.raises(DBAPIError) as caught, database.api_preauth_session() as session:
        session._connection.execute(text(f"SELECT * FROM {table} LIMIT 1"))  # noqa: S608
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("table", READABLE)
def test_api_reads_global_domain_tables(database: Database, table: str) -> None:
    with database.api_preauth_session() as session:
        session._connection.execute(text(f"SELECT * FROM {table} LIMIT 1"))  # noqa: S608


def test_api_updates_only_customer_settings_columns(database: Database) -> None:
    with database.worker_session() as session:
        owner = customer(session)
    with database.api_session(owner.id) as session:
        session.execute(
            update(Customer).where(Customer.id == owner.id).values(timezone="Europe/Warsaw")
        )
    with pytest.raises(DBAPIError) as caught, database.api_session(owner.id) as session:
        session.execute(
            update(Customer).where(Customer.id == owner.id).values(sending_enabled=True)
        )
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE
    with database.worker_session() as session:
        stored = session.get_one(Customer, owner.id)
        assert stored.timezone == "Europe/Warsaw"
        assert stored.sending_enabled is False


def test_api_cannot_create_cases(database: Database) -> None:
    with database.worker_session() as session:
        owner = customer(session)
    with pytest.raises(DBAPIError) as caught, database.api_session(owner.id) as session:
        session.execute(text("SELECT nextval('case_number_seq')"))
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE
