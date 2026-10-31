"""Response shapes shared by several endpoints of the customer API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from kalkan.db.models import Case, Domain, WhitelistEntry
from kalkan.domain.enums import Category, WhitelistKind, WhitelistSource
from kalkan.domain.metrics import PanelStatus, panel_status


class ThreatItem(BaseModel):
    id: uuid.UUID
    number: str
    domain: str
    domain_unicode: str | None
    brand_id: uuid.UUID
    category: Category | None
    status: PanelStatus
    first_seen_at: datetime
    hosting: str | None
    last_action: str | None
    last_action_at: datetime | None


def hosting_of(domain: Domain) -> str | None:
    if domain.is_cloudflare:
        return "Cloudflare"
    return domain.hosting_org


def threat_item(case: Case, domain: Domain) -> ThreatItem:
    return ThreatItem(
        id=case.id,
        number=case.number,
        domain=domain.name,
        domain_unicode=domain.unicode_name,
        brand_id=case.brand_id,
        category=case.category,
        status=panel_status(case.state),
        first_seen_at=case.first_seen_at,
        hosting=hosting_of(domain),
        last_action=case.last_action,
        last_action_at=case.last_action_at,
    )


class WhitelistItem(BaseModel):
    id: uuid.UUID
    pattern: str
    kind: WhitelistKind
    note: str | None
    brand_id: uuid.UUID | None
    source: WhitelistSource
    created_by: str | None
    created_at: datetime


def whitelist_item(entry: WhitelistEntry) -> WhitelistItem:
    return WhitelistItem(
        id=entry.id,
        pattern=entry.pattern,
        kind=entry.kind,
        note=entry.note,
        brand_id=entry.brand_id,
        source=entry.source,
        created_by=entry.created_by,
        created_at=entry.created_at,
    )


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


# A cell a spreadsheet would read as a formula is prefixed with an apostrophe.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text
