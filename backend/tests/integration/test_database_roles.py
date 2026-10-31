"""The three application roles exist and none of them is a superuser or bypasses RLS."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

ROLES = ("kalkan_owner", "kalkan_api", "kalkan_worker")


def _role_attributes(engine: Engine) -> dict[str, dict[str, bool]]:
    query = text(
        """
        SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin
        FROM pg_roles WHERE rolname = ANY(:names)
        """
    )
    with engine.connect() as connection:
        rows = connection.execute(query, {"names": list(ROLES)}).mappings().all()
    return {row["rolname"]: {k: v for k, v in row.items() if k != "rolname"} for row in rows}


def test_all_roles_exist_and_can_log_in(api_engine: Engine) -> None:
    roles = _role_attributes(api_engine)
    assert sorted(roles) == sorted(ROLES)
    assert all(attrs["rolcanlogin"] for attrs in roles.values())


@pytest.mark.parametrize("role", ROLES)
def test_role_is_not_superuser_and_does_not_bypass_rls(api_engine: Engine, role: str) -> None:
    attrs = _role_attributes(api_engine)[role]
    assert attrs["rolsuper"] is False
    assert attrs["rolbypassrls"] is False


@pytest.mark.parametrize("role", ["kalkan_api", "kalkan_worker"])
def test_application_roles_cannot_create_roles_or_databases(api_engine: Engine, role: str) -> None:
    attrs = _role_attributes(api_engine)[role]
    assert attrs["rolcreaterole"] is False
    assert attrs["rolcreatedb"] is False


@pytest.mark.parametrize(
    ("fixture", "role"),
    [
        ("api_engine", "kalkan_api"),
        ("worker_engine", "kalkan_worker"),
        ("owner_engine", "kalkan_owner"),
    ],
)
def test_each_connection_url_logs_in_as_its_role(
    request: pytest.FixtureRequest, fixture: str, role: str
) -> None:
    engine: Engine = request.getfixturevalue(fixture)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == role


def test_owner_owns_the_database_and_extensions_exist(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        owner = connection.execute(
            text(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
            )
        ).scalar_one()
        extensions = set(connection.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert owner == "kalkan_owner"
    assert {"pg_trgm", "citext"} <= extensions
