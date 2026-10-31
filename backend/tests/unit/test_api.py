from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from kalkan.api.app import create_app
from kalkan.api.problems import PROBLEM_CONTENT_TYPE, ProblemError
from kalkan.logging import redact_path
from tests.support import make_settings

EDGE_SECRET = "edge-secret-for-tests" + "-" * 16


class Payload(BaseModel):
    count: int


def _add_test_routes(app: FastAPI) -> None:
    @app.get("/api/v1/_test/ok")
    def ok() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/v1/_test/validate")
    def validate(payload: Payload) -> dict[str, int]:
        return {"count": payload.count}

    @app.get("/api/v1/_test/problem")
    def problem() -> None:
        raise ProblemError(409, title="Already whitelisted", detail="mintera.pl", code="dup")

    @app.get("/api/v1/_test/crash")
    def crash() -> None:
        raise RuntimeError("database password is hunter2")


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(make_settings(env="local"))
    _add_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def edge_client() -> Iterator[TestClient]:
    app = create_app(make_settings(env="prod", edge_shared_secret=EDGE_SECRET))
    _add_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/healthz", "/api/healthz", "/api/health"])
def test_health_endpoints(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_without_database_is_unavailable(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["checks"] == {"database": "not_configured"}


def test_readyz_reports_unreachable_database() -> None:
    settings = make_settings(
        env="local",
        database_url_api="postgresql+psycopg://kalkan_api:wrong@127.0.0.1:1/kalkan",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"] == {"database": "unavailable"}
    assert "wrong" not in response.text


def test_request_id_is_generated_and_returned(client: TestClient) -> None:
    response = client.get("/api/v1/_test/ok")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert client.get("/api/v1/_test/ok").headers["X-Request-ID"] != request_id


def test_valid_incoming_request_id_is_kept(client: TestClient) -> None:
    response = client.get("/api/v1/_test/ok", headers={"X-Request-ID": "edge-abc.123"})
    assert response.headers["X-Request-ID"] == "edge-abc.123"


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "semi;colon", "slash/inside", ""])
def test_invalid_incoming_request_id_is_replaced(client: TestClient, bad: str) -> None:
    response = client.get("/api/v1/_test/ok", headers={"X-Request-ID": bad})
    assert response.headers["X-Request-ID"] != bad
    assert len(response.headers["X-Request-ID"]) == 32


def test_not_found_is_problem_json(client: TestClient) -> None:
    response = client.get("/api/v1/nope", headers={"X-Request-ID": "rid-1"})
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "instance": "/api/v1/nope",
        "request_id": "rid-1",
    }


def test_method_not_allowed_keeps_allow_header(client: TestClient) -> None:
    response = client.delete("/healthz")
    assert response.status_code == 405
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert "GET" in response.headers["allow"]


def test_validation_errors_are_problem_json_without_input(client: TestClient) -> None:
    response = client.post("/api/v1/_test/validate", json={"count": "secret-value"})
    assert response.status_code == 422
    body = response.json()
    assert body["title"] == "Validation failed"
    assert body["errors"][0]["loc"] == ["body", "count"]
    assert "secret-value" not in response.text


def test_problem_error_fields(client: TestClient) -> None:
    response = client.get("/api/v1/_test/problem")
    assert response.status_code == 409
    body = response.json()
    assert body["title"] == "Already whitelisted"
    assert body["detail"] == "mintera.pl"
    assert body["code"] == "dup"


def test_unhandled_error_is_generic_problem(client: TestClient) -> None:
    response = client.get("/api/v1/_test/crash", headers={"X-Request-ID": "rid-500"})
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.headers["X-Request-ID"] == "rid-500"
    body = response.json()
    assert body["title"] == "Internal Server Error"
    assert body["request_id"] == "rid-500"
    assert "hunter2" not in response.text


def test_edge_header_is_required_when_configured(edge_client: TestClient) -> None:
    missing = edge_client.get("/api/v1/_test/ok")
    wrong = edge_client.get("/api/v1/_test/ok", headers={"X-Edge-Auth": "nope"})
    for response in (missing, wrong):
        assert response.status_code == 403
        assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
        assert response.headers["X-Request-ID"]

    allowed = edge_client.get("/api/v1/_test/ok", headers={"X-Edge-Auth": EDGE_SECRET})
    assert allowed.status_code == 200


@pytest.mark.parametrize("path", ["/healthz", "/api/healthz", "/api/health", "/readyz"])
def test_health_endpoints_skip_the_edge_check(edge_client: TestClient, path: str) -> None:
    assert edge_client.get(path).status_code != 403


def test_run_app_health_alias_works_without_the_edge(edge_client: TestClient) -> None:
    # The uptime check and the deploy smoke test call run.app directly, without X-Edge-Auth.
    response = edge_client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_alias_is_exact(edge_client: TestClient) -> None:
    # Only the exact path skips the edge check; nothing else under /api/health does.
    assert edge_client.get("/api/health/").status_code == 403
    assert edge_client.get("/api/healthcheck").status_code == 403


def test_edge_check_is_off_without_a_secret(client: TestClient) -> None:
    assert client.get("/api/v1/_test/ok").status_code == 200


def test_docs_are_only_served_locally(client: TestClient, edge_client: TestClient) -> None:
    assert client.get("/api/openapi.json").status_code == 200
    headers = {"X-Edge-Auth": EDGE_SECRET}
    assert edge_client.get("/api/openapi.json", headers=headers).status_code == 404
    assert edge_client.get("/api/docs", headers=headers).status_code == 404


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/e/abcDEF123/files/42", "/e/[REDACTED]/files/42"),
        ("/e/abcDEF123", "/e/[REDACTED]"),
        ("/api/v1/threats", "/api/v1/threats"),
        ("/api/e/x", "/api/e/x"),
    ],
)
def test_evidence_tokens_are_not_logged(path: str, expected: str) -> None:
    assert redact_path(path) == expected
