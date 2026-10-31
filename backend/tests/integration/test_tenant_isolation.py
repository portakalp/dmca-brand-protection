"""Row level security: kalkan_api only ever sees and writes its own tenant (§17; K16)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from kalkan.db.models import TENANT_TABLES
from kalkan.db.session import Database
from tests.factories import TenantRows, tenant_rows

TABLES = ("customers", *TENANT_TABLES)
INSUFFICIENT_PRIVILEGE = "42501"  # permission denied and row level security violations


@pytest.fixture
def tenants(database: Database) -> tuple[TenantRows, TenantRows]:
    with database.worker_session() as session:
        return tenant_rows(session, "tenant-a"), tenant_rows(session, "tenant-b")


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def test_every_tenant_table_has_a_test_row(tenants: tuple[TenantRows, TenantRows]) -> None:
    a, b = tenants
    assert set(a.ids) == set(TABLES) == set(b.ids)


@pytest.mark.parametrize("table", TABLES)
def test_api_sees_only_its_own_rows(
    database: Database, tenants: tuple[TenantRows, TenantRows], table: str
) -> None:
    a, b = tenants
    with database.api_session(a.customer.id) as session:
        ids = set(session.execute(text(f"SELECT id FROM {table}")).scalars())  # noqa: S608
        found_b = session.execute(
            text(f"SELECT count(*) FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": b.ids[table]},
        ).scalar_one()
    assert a.ids[table] in ids
    assert b.ids[table] not in ids
    assert found_b == 0


@pytest.mark.parametrize("table", TABLES)
def test_api_cannot_change_another_tenants_rows(
    database: Database, tenants: tuple[TenantRows, TenantRows], table: str
) -> None:
    a, b = tenants

    def attempt(statement: str) -> int | str | None:
        """Rows changed, or the SQLSTATE of the error."""
        try:
            with database.api_session(a.customer.id) as session:
                result = session.execute(text(statement), {"id": b.ids[table]})
                return int(result.rowcount)  # type: ignore[attr-defined]
        except DBAPIError as error:
            return _sqlstate(error)

    with database.worker_session() as session:
        updatable = session.scalar(
            text(
                "SELECT a.attname FROM pg_attribute a "
                "WHERE a.attrelid = CAST(:table AS regclass) AND a.attnum > 0 "
                "AND NOT a.attisdropped "
                "AND has_column_privilege('kalkan_api', a.attrelid, a.attnum, 'UPDATE') "
                "ORDER BY a.attname LIMIT 1"
            ),
            {"table": table},
        )
        deletable = session.scalar(
            text("SELECT has_table_privilege('kalkan_api', :table, 'DELETE')"), {"table": table}
        )

    # Where kalkan_api may write, row level security must hide the other tenant's row
    # (0 rows); elsewhere the privilege check refuses the statement.
    if updatable:
        update = f"UPDATE {table} SET {updatable} = {updatable} WHERE id = :id"  # noqa: S608
        assert attempt(update) == 0
    else:
        assert attempt(f"UPDATE {table} SET id = id WHERE id = :id") == INSUFFICIENT_PRIVILEGE  # noqa: S608
    expected_delete = 0 if deletable else INSUFFICIENT_PRIVILEGE
    assert attempt(f"DELETE FROM {table} WHERE id = :id") == expected_delete  # noqa: S608

    with database.worker_session() as session:
        still_there = session.execute(
            text(f"SELECT count(*) FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": b.ids[table]},
        ).scalar_one()
    assert still_there == 1


@pytest.mark.parametrize("table", TABLES)
def test_api_cannot_insert_rows_for_another_tenant(
    database: Database, tenants: tuple[TenantRows, TenantRows], table: str
) -> None:
    """A copy of the tenant's own row, re-labelled with the other tenant, is refused."""
    a, b = tenants
    tenant_column = "id" if table == "customers" else "customer_id"
    statement = text(
        f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "  # noqa: S608
        f"to_jsonb(r) || jsonb_build_object('id', CAST(:new_id AS text), "
        f"'{tenant_column}', CAST(:other AS text)))).* "
        f"FROM {table} r WHERE r.id = :own"
    )
    with pytest.raises(DBAPIError) as caught, database.api_session(a.customer.id) as session:
        session.execute(
            statement,
            {"new_id": str(uuid.uuid4()), "other": str(b.customer.id), "own": a.ids[table]},
        )
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("table", TABLES)
def test_api_without_tenant_context_sees_nothing(
    database: Database, tenants: tuple[TenantRows, TenantRows], table: str
) -> None:
    with database.api_engine.connect() as connection:
        count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
    assert count == 0


def test_context_ends_with_the_transaction_on_a_reused_connection(
    database: Database, tenants: tuple[TenantRows, TenantRows]
) -> None:
    """After a tenant transaction the pooled connection reads no rows and does not fail."""
    a, _ = tenants
    with database.api_engine.connect() as connection:
        with connection.begin():
            connection.execute(
                text("SELECT set_config('app.customer_id', :id, true)"), {"id": str(a.customer.id)}
            )
            assert connection.execute(text("SELECT count(*) FROM cases")).scalar_one() == 1
        with connection.begin():
            assert connection.execute(text("SELECT count(*) FROM cases")).scalar_one() == 0


@pytest.mark.parametrize("table", TABLES)
def test_worker_sees_every_tenant(
    database: Database, tenants: tuple[TenantRows, TenantRows], table: str
) -> None:
    a, b = tenants
    with database.worker_session() as session:
        ids = set(session.execute(text(f"SELECT id FROM {table}")).scalars())  # noqa: S608
    assert {a.ids[table], b.ids[table]} <= ids


def test_api_cannot_redirect_another_tenants_alerts(
    database: Database, tenants: tuple[TenantRows, TenantRows]
) -> None:
    a, b = tenants
    with database.api_session(a.customer.id) as session:
        changed = session.execute(
            text("UPDATE customers SET alert_email = 'attacker@example.com' WHERE id = :id"),
            {"id": b.customer.id},
        )
        assert changed.rowcount == 0  # type: ignore[attr-defined]
        views = session.execute(
            text("UPDATE evidence_links SET views = views + 100 WHERE id = :id"),
            {"id": b.ids["evidence_links"]},
        )
        assert views.rowcount == 0  # type: ignore[attr-defined]


def test_api_session_requires_a_uuid(database: Database) -> None:
    with pytest.raises(TypeError), database.api_session("not-a-uuid"):  # type: ignore[arg-type]
        pass
