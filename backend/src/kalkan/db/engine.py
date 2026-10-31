"""SQLAlchemy engine factory (sync SQLAlchemy 2 + psycopg 3, K2)."""

from __future__ import annotations

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine

# Cloud SQL db-f1-micro allows few connections (architecture.md §22); keep pools small.
POOL_SIZE = 2
MAX_OVERFLOW = 2
CONNECT_TIMEOUT_SECONDS = 3


def create_db_engine(url: SecretStr) -> Engine:
    return create_engine(
        url.get_secret_value(),
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
