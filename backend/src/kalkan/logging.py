"""JSON logging with structlog (architecture.md §23).

Every line is one JSON object on stdout with at least ``ts``, ``level``, ``event`` and
``service``. Context such as ``request_id``, ``customer_id``, ``case_id`` and ``job`` is
added with ``structlog.contextvars``. Standard library loggers (uvicorn, dramatiq,
apscheduler) go through the same renderer. Values of keys that look like secrets are
replaced before rendering, and tracebacks never include local variables (they hold request
headers, cookies and credentials).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|authorization|cookie|api[_-]?key|dsn|totp|credential",
    re.IGNORECASE,
)
# Loggers that configure their own handlers; they are routed to the root handler instead.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "dramatiq", "apscheduler")
# Replaced by the one-line request log of kalkan.api.middleware.RequestContextMiddleware.
_SILENCED_LOGGERS = ("uvicorn.access",)
# HTTP clients log every request URL at INFO; URLs may carry keys or tokens (§23). Their
# warnings and errors still come through.
_QUIET_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "whoisit")
# Evidence links carry their access token in the path (/e/<token>/...).
_EVIDENCE_TOKEN_PATH = re.compile(r"^/e/[^/]+")


def redact_path(path: str) -> str:
    """Return a URL path that is safe to log or report (no evidence-link token)."""
    return _EVIDENCE_TOKEN_PATH.sub("/e/[REDACTED]", path)


class _DropUvicornDuplicateErrors(logging.Filter):
    """Uvicorn logs every unhandled ASGI exception again after the API's own error handler
    (kalkan.api.problems) has logged it with the request id; keep only the first record."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "uvicorn.error"
            and str(record.msg).startswith("Exception in ASGI application")
        )


def _redact_secrets(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
    return event_dict


def _add_service(service: str) -> Processor:
    def processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return processor


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure structlog and the standard library root logger for this process."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _add_service(service),
        _redact_secrets,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer(
                ExceptionDictTransformer(show_locals=False, use_rich=False)
            ),
            structlog.processors.JSONRenderer(),
        ],
    )
    # Bind to sys.stdout as it is now: Dramatiq worker processes replace it with a pipe to
    # the parent process before they import their modules.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_DropUvicornDuplicateErrors())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in _THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    for name in _SILENCED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = False
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLevelName(level)))


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name, **initial_values)
    return logger
