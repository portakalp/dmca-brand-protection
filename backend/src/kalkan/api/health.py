"""Health endpoints. ``/healthz`` checks only the process; ``/readyz`` also the database.

Cloud Run's front end reserves some paths ending in ``z``, so ``*.run.app`` callers (uptime
check, deploy smoke test, the edge Worker) use ``/api/health`` (K80).
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from kalkan.api.problems import problem_response
from kalkan.logging import get_logger

router = APIRouter()
log = get_logger(__name__)


@router.get("/healthz", include_in_schema=False)
@router.get("/api/healthz", include_in_schema=False)
@router.get("/api/health", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False, response_model=None)
def readyz(request: Request) -> dict[str, object] | JSONResponse:
    engine: Engine | None = request.app.state.db_engine
    if engine is None:
        database = "not_configured"
    else:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            database = "ok"
        except SQLAlchemyError as exc:
            log.warning("readyz.database_unavailable", error=type(exc).__name__)
            database = "unavailable"

    checks = {"database": database}
    if database != "ok":
        return problem_response(
            request.scope, HTTPStatus.SERVICE_UNAVAILABLE, extensions={"checks": checks}
        )
    return {"status": "ok", "checks": checks}
