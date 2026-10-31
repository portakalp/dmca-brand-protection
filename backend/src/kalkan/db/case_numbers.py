"""Case numbers: ``CASE_PREFIX`` + six digits from a global sequence (architecture.md §5)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def format_case_number(prefix: str, value: int) -> str:
    return f"{prefix}-{value:06d}"


def next_case_number(session: Session, prefix: str) -> str:
    value = session.execute(text("SELECT nextval('case_number_seq')")).scalar_one()
    return format_case_number(prefix, int(value))
