"""The initial migration: clean up and down, matches the models, secures every tenant table."""

from __future__ import annotations

from alembic import command
from sqlalchemy import Engine, text

from kalkan.db.models import TENANT_TABLES
from kalkan.db.session import Database
from tests.integration.conftest import alembic_config, run_alembic


def _public_tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def test_downgrade_to_base_and_upgrade_again(migrated: Engine) -> None:
    run_alembic(migrated, "downgrade", "base")
    assert _public_tables(migrated) == {"alembic_version"}
    with migrated.connect() as connection:
        functions = connection.execute(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.prosecdef"
            )
        ).scalar_one()
    assert functions == 0
    run_alembic(migrated, "upgrade", "head")
    assert len(_public_tables(migrated)) == 37


def test_models_and_migration_match(migrated: Engine) -> None:
    config = alembic_config(migrated)
    with migrated.connect() as connection:
        config.attributes["connection"] = connection
        command.check(config)  # raises when autogenerate would produce operations


def test_every_tenant_table_has_row_level_security(migrated: Engine) -> None:
    with migrated.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       array_agg(p.polname ORDER BY p.polname) AS policies
                FROM pg_class c LEFT JOIN pg_policy p ON p.polrelid = c.oid
                WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
                GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity
                """
            )
        ).all()
    by_table = {row.relname: row for row in rows}
    for table in ("customers", *TENANT_TABLES):
        row = by_table[table]
        assert row.relrowsecurity is True, table
        assert row.relforcerowsecurity is False, table
        assert row.policies == ["tenant_isolation", "worker_all"], table
    secured = {name for name, row in by_table.items() if row.relrowsecurity}
    assert secured == {"customers", *TENANT_TABLES}


def test_default_settings_keep_everything_switched_off(database: Database) -> None:
    with database.worker_session() as session:
        settings = {
            row.key: row.value for row in session.execute(text("SELECT key, value FROM settings"))
        }
    assert settings["sending_enabled"] is False
    assert settings["auto_approve_enabled"] is False
    assert settings["triage_min_confidence"] == 0.9
    assert settings["visual_similarity_min"] == 0.85
    assert set(settings["channel_daily_limits"]) == {
        "cloudflare",
        "hosting",
        "registrar",
        "registry",
        "google",
    }


def test_roles_have_no_elevated_attributes(migrated: Engine) -> None:
    with migrated.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname IN ('kalkan_owner', 'kalkan_api', 'kalkan_worker')"
            )
        ).all()
    assert len(rows) == 3
    assert all(not row.rolsuper and not row.rolbypassrls for row in rows)
