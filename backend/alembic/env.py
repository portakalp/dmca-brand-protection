"""Alembic environment. Migrations run as kalkan_owner (``DATABASE_URL_MIGRATE``).

A connection can be passed in through ``config.attributes["connection"]`` (tests do this to
migrate the test database).
"""

from __future__ import annotations

from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection, create_engine, pool

from kalkan.config import get_settings
from kalkan.db.base import StrEnumText
from kalkan.db.models import Base

config = context.config
target_metadata = Base.metadata


def _render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render enum columns as plain ``sa.Text()``; their CHECK constraints carry the values."""
    if type_ == "type" and isinstance(obj, StrEnumText):
        return "sa.Text()"
    return False


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    url = get_settings().database_url_migrate
    if url is None:
        raise RuntimeError("DATABASE_URL_MIGRATE is required to run migrations")
    engine = create_engine(url.get_secret_value(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; run against a database.")
run_migrations_online()
