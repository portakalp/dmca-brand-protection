"""Writes the API shares: whitelist entries and the audit log (§17)."""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import select

from kalkan.api.v1.deps import Context, client_ip
from kalkan.db.models import AuditLog, WhitelistEntry
from kalkan.domain.enums import WhitelistKind, WhitelistSource


def add_whitelist_entry(
    context: Context,
    *,
    pattern: str,
    kind: WhitelistKind,
    note: str | None,
    brand_id: uuid.UUID | None,
    source: WhitelistSource,
) -> tuple[WhitelistEntry, bool]:
    """The entry for ``pattern`` (created when missing) and whether it is new."""
    session = context.session
    existing = session.scalar(
        select(WhitelistEntry).where(
            WhitelistEntry.customer_id == context.customer_id,
            WhitelistEntry.pattern == pattern,
            WhitelistEntry.brand_id.is_(None)
            if brand_id is None
            else WhitelistEntry.brand_id == brand_id,
        )
    )
    if existing is not None:
        return existing, False
    entry = WhitelistEntry(
        id=uuid.uuid4(),
        customer_id=context.customer_id,
        brand_id=brand_id,
        pattern=pattern,
        kind=kind,
        source=source,
        note=note,
        created_by=context.user.email,
        created_at=context.at,
    )
    session.add(entry)
    session.flush()
    return entry, True


def audit(context: Context, request: Request, action: str, target: str, **data: object) -> None:
    context.session.add(
        AuditLog(
            actor=context.user.email,
            action=action,
            customer_id=context.customer_id,
            target=target,
            data={key: str(value) for key, value in data.items()},
            ip=client_ip(request),
            at=context.at,
        )
    )
