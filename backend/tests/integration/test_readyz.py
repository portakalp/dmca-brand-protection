from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from kalkan.api.app import create_app
from kalkan.config import Settings


def test_readyz_checks_the_database(settings: Settings, api_engine: Engine) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}
