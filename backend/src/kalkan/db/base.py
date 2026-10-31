"""Declarative base, column helpers and conventions (architecture.md §5).

- Ids are UUID v7, generated in the application.
- Times are ``timestamptz`` in UTC; defaults are generated in Python so inserts never need
  ``RETURNING`` (kalkan_api may only INSERT into some tables, §17).
- Enumerations are ``text`` columns with a named ``CHECK`` constraint, not native enums.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import CheckConstraint, DateTime, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column
from sqlalchemy.types import TypeDecorator
from uuid_utils import compat as uuid_compat

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_id() -> uuid.UUID:
    """A new UUID v7 (time ordered)."""
    return uuid_compat.uuid7()


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class StrEnumText[E: StrEnum](TypeDecorator[E]):
    """A ``text`` column holding a :class:`StrEnum`; values are validated on write."""

    impl = Text
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self.enum_cls = enum_cls

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else self.enum_cls(value).value

    def process_result_value(self, value: Any, dialect: Any) -> E | None:
        return None if value is None else self.enum_cls(value)


def enum_column[E: StrEnum](
    enum_cls: type[E],
    name: str,
    *,
    nullable: bool = False,
    default: E | None = None,
) -> MappedColumn[Any]:
    """A text column limited to ``enum_cls`` values by ``CHECK``; ``name`` is the column name."""
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    condition = f"{name} IN ({values})"
    if nullable:
        condition = f"{name} IS NULL OR {condition}"
    return mapped_column(
        StrEnumText(enum_cls),
        CheckConstraint(condition, name=f"{name}_valid"),
        nullable=nullable,
        default=default,
        server_default=default.value if default is not None else None,
    )


def id_column() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=new_id)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(default=utcnow, server_default=func.now())


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())


def lift_column_checks(metadata: MetaData) -> None:
    """Turn column-level CHECK constraints into table-level ones.

    Alembic's autogenerate renders only table-level CHECK constraints; the constraint names
    and SQL stay the same.
    """
    for table in metadata.tables.values():
        for column in table.columns:
            for constraint in [c for c in column.constraints if isinstance(c, CheckConstraint)]:
                column.constraints.discard(constraint)
                table.append_constraint(CheckConstraint(constraint.sqltext, name=constraint.name))
