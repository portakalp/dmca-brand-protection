"""``/overview`` on the demo seed returns the design's numbers (128, 47,
39, 83 %, 31 hours; +19 %, +9 %, 22 % faster; 2/6/39/69/12; 17/14/8), and the cohort query uses
the ``cases (customer_id, first_seen_at)`` index."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from kalkan.api.v1.queries import cohort_query
from kalkan.config import Settings
from kalkan.db.session import Database
from kalkan.seed import design
from kalkan.seed.demo import DEMO_SLUG
from kalkan.storage.gcs import ObjectStore
from tests.integration.api_support import (
    api,  # noqa: F401 - a fixture
    mailbox,  # noqa: F401 - a fixture
)
from tests.integration.test_seed import DESIGN_NOW, _seed

TZ = ZoneInfo(design.TIMEZONE)
TODAY = DESIGN_NOW.astimezone(TZ).date()
PERIOD = {"from": str(TODAY - timedelta(days=29)), "to": str(TODAY)}


@pytest.fixture
def demo_owner(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> dict[str, Any]:
    credentials = _seed(database, object_store, test_settings, tmp_path)
    users = json.loads(credentials.read_text())["users"]
    owner: dict[str, Any] = next(user for user in users if user["role"] == "owner")
    return owner


def _sign_in(client: TestClient, user: dict[str, Any]) -> None:
    login = client.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert login.json() == {"mfa": "verify"}
    code = pyotp.TOTP(user["totp_secret"]).now()
    assert client.post("/api/v1/auth/mfa", json={"code": code}).status_code == 204


def test_the_overview_shows_the_designs_numbers(
    api: TestClient,  # noqa: F811
    demo_owner: dict[str, Any],
) -> None:
    _sign_in(api, demo_owner)
    body = api.get("/api/v1/overview", params=PERIOD).json()
    kpis = body["kpis"]
    assert (kpis["detected"], kpis["verified"], kpis["taken_down"]) == (128, 47, 39)
    assert kpis["takedown_rate"] == 83
    assert round(kpis["median_takedown_hours"]) == 31
    assert body["changes"]["detected"] == 19
    assert body["changes"]["verified"] == 9
    assert body["changes"]["median_takedown_hours"] == -22  # 22 % faster
    distribution = body["status_distribution"]
    assert [
        distribution[name]
        for name in ("active", "notice_sent", "taken_down", "watching", "dismissed")
    ] == [2, 6, 39, 69, 12]
    assert distribution["reviewing"] == 0
    channels = {row["channel"]: row["cases"] for row in body["takedowns_by_channel"]}
    assert channels == {"cloudflare": 17, "hosting": 14, "registrar": 8}
    assert [day["detected"] for day in body["daily"]] == list(design.DETECTED_PER_DAY)
    assert [day["taken_down"] for day in body["daily"]] == list(design.TAKEN_DOWN_PER_DAY)
    assert body["live_threats"] == 8
    recent = body["recent_threats"]
    assert recent[0]["domain"] == design.DETAIL_DOMAIN
    assert len(recent) == 8
    assert body["whitelist"]["total"] > 0
    # The demo is read-only, whatever the role.
    me = api.get("/api/v1/me").json()
    assert me["customer"]["is_demo"] is True
    assert me["permissions"]["write_whitelist"] is False


def test_the_cohort_query_uses_its_index(
    database: Database, demo_owner: dict[str, Any], test_owner_engine: Engine
) -> None:
    # Fresh statistics, as autovacuum would keep them; ANALYZE needs the tables' owner.
    with test_owner_engine.begin() as connection:
        connection.execute(text("ANALYZE cases, notices, case_events"))
    with database.worker_session() as session:
        customer_id = session.execute(
            text("SELECT id FROM customers WHERE slug = :slug"), {"slug": DEMO_SLUG}
        ).scalar_one()
        start = datetime(2026, 9, 9, tzinfo=TZ).astimezone(UTC)
        # One day of one customer: selective enough for the index on this small data set.
        query = cohort_query(customer_id, start, start + timedelta(days=1), None)
        compiled = query.compile(session.get_bind(), compile_kwargs={"literal_binds": True})
        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(row[0] for row in session.execute(text(f"EXPLAIN {compiled}")))
    assert "Index Cond: ((customer_id = " in plan
    assert "ix_cases_customer_id_first_seen_at" in plan, plan
    assert "Seq Scan on cases" not in plan
