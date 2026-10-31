from __future__ import annotations

import json
import logging

import pytest
import structlog

from kalkan.logging import REDACTED, configure_logging, get_logger


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def test_structlog_events_are_json_with_standard_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    get_logger("kalkan.test").info("case.created", case_id="c-1")

    (line,) = _lines(capsys)
    assert line["event"] == "case.created"
    assert line["level"] == "info"
    assert line["service"] == "api"
    assert line["case_id"] == "c-1"
    assert str(line["ts"]).endswith("Z")


def test_context_variables_are_included(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker-default")
    with structlog.contextvars.bound_contextvars(request_id="r-1", customer_id="cust-1"):
        get_logger().info("job.started")

    (line,) = _lines(capsys)
    assert line["request_id"] == "r-1"
    assert line["customer_id"] == "cust-1"


def test_secret_like_keys_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    get_logger().info(
        "auth.attempt",
        password="hunter2",
        session_token="tok",
        api_key="key",
        authorization="Bearer x",
        email_count=1,
    )

    (line,) = _lines(capsys)
    for key in ("password", "session_token", "api_key", "authorization"):
        assert line[key] == REDACTED
    assert line["email_count"] == 1
    assert "hunter2" not in json.dumps(line)


def test_standard_library_loggers_use_the_same_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker-default", "INFO")
    logging.getLogger("dramatiq.worker").warning("consumer stalled")

    (line,) = _lines(capsys)
    assert line["event"] == "consumer stalled"
    assert line["level"] == "warning"
    assert line["service"] == "worker-default"
    assert line["logger"] == "dramatiq.worker"


def test_exceptions_are_structured(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger().exception("job.failed")

    (line,) = _lines(capsys)
    assert line["event"] == "job.failed"
    assert line["exception"][0]["exc_type"] == "ValueError"  # type: ignore[index]


def test_log_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api", "WARNING")
    get_logger().info("quiet")
    get_logger().warning("loud")

    assert [line["event"] for line in _lines(capsys)] == ["loud"]


def test_uvicorn_access_log_is_silenced(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    logging.getLogger("uvicorn.access").info("GET /healthz 200")
    logging.getLogger("uvicorn.error").info("Application startup complete.")

    assert [line["event"] for line in _lines(capsys)] == ["Application startup complete."]


def test_tracebacks_do_not_include_local_variables(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")

    def handler(edge_auth: str) -> None:
        session_cookie = "SESSION-COOKIE-789"
        raise RuntimeError(f"failed after reading {len(session_cookie)} bytes")

    try:
        handler("EDGE-SECRET-123")
    except RuntimeError:
        get_logger().exception("http.unhandled_error")

    output = capsys.readouterr().out
    assert "EDGE-SECRET-123" not in output
    assert "SESSION-COOKIE-789" not in output
    assert "handler" in output  # frames are still there


def test_uvicorn_duplicate_of_a_handled_error_is_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("api")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)
    logging.getLogger("uvicorn.error").error("Exception in 'lifespan' protocol")

    assert [line["event"] for line in _lines(capsys)] == ["Exception in 'lifespan' protocol"]
