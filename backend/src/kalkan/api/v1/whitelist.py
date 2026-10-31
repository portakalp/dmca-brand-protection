"""The customer's whitelist (architecture.md §9, §17, product.md §9).

Owners add and delete entries; every addition leaves a ``whitelist_changed`` outbox job, so the
open cases it covers are dismissed (and retracted) at once by the worker. A pattern is a host;
public suffixes and shared platform roots are refused (``enrichment.whitelist``).

CSV import: a header row with ``pattern`` and optionally ``kind``, ``note`` and ``brand`` (a
brand name; empty for every brand), at most 5,000 rows and 1 MB. Valid rows are added, rows
already on the list are skipped, and every other row is reported with its line number.
``dry_run=true`` checks the file the same way and writes nothing; the panel previews with it.
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from kalkan.api.problems import ProblemError
from kalkan.api.v1.deps import Context, Member, Owner, Writer
from kalkan.api.v1.schemas import WhitelistItem, csv_cell, whitelist_item
from kalkan.api.v1.writes import add_whitelist_entry, audit
from kalkan.db.models import Brand, WhitelistEntry
from kalkan.db.outbox import WHITELIST_CHANGED, queue_job
from kalkan.domain.enums import WhitelistKind, WhitelistSource
from kalkan.domain.hostnames import InvalidHostnameError
from kalkan.enrichment.whitelist import validate_pattern

router = APIRouter(tags=["whitelist"])
MAX_IMPORT_BYTES = 1024 * 1024
MAX_IMPORT_ROWS = 5000


class NewEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=253)
    kind: WhitelistKind = WhitelistKind.OTHER
    note: str | None = Field(default=None, max_length=500)
    brand_id: uuid.UUID | None = None


class RowError(BaseModel):
    line: int
    value: str
    error: str


class ImportResult(BaseModel):
    added: int
    skipped: int
    errors: list[RowError]


def _entries(context: Context) -> list[WhitelistEntry]:
    return list(
        context.session.scalars(
            select(WhitelistEntry)
            .where(WhitelistEntry.customer_id == context.customer_id)
            .order_by(WhitelistEntry.pattern, WhitelistEntry.created_at)
        ).all()
    )


@router.get("/whitelist", response_model=list[WhitelistItem])
def whitelist(context: Member) -> list[WhitelistItem]:
    return [whitelist_item(entry) for entry in _entries(context)]


@router.post("/whitelist", response_model=WhitelistItem, status_code=201)
def add(body: NewEntry, context: Writer, request: Request) -> WhitelistItem:
    try:
        pattern = validate_pattern(body.pattern)
    except InvalidHostnameError as exc:
        raise ProblemError(
            422, title="Not a valid pattern", detail=str(exc), code="pattern"
        ) from None
    if body.brand_id is not None and context.session.get(Brand, body.brand_id) is None:
        raise ProblemError(404, title="No such brand", code="not_found")
    entry, created = add_whitelist_entry(
        context,
        pattern=pattern,
        kind=body.kind,
        note=body.note,
        brand_id=body.brand_id,
        source=WhitelistSource.MANUAL,
    )
    if not created:
        raise ProblemError(409, title="Already on the whitelist", code="exists")
    queue_job(context.session, WHITELIST_CHANGED, customer_id=context.customer_id)
    audit(context, request, "whitelist.added", pattern)
    return whitelist_item(entry)


@router.delete("/whitelist/{entry_id}", response_model=None, status_code=204)
def remove(entry_id: uuid.UUID, context: Writer, request: Request) -> None:
    entry = context.session.get(WhitelistEntry, entry_id)
    if entry is None or entry.customer_id != context.customer_id:
        raise ProblemError(404, title="No such entry", code="not_found")
    context.session.delete(entry)
    audit(context, request, "whitelist.removed", entry.pattern)


def _read_csv(data: bytes) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProblemError(422, title="The file is not UTF-8", code="csv") from None
    reader = csv.DictReader(io.StringIO(text))
    fields = {name.strip().lower() for name in reader.fieldnames or []}
    if "pattern" not in fields:
        raise ProblemError(422, title="The file needs a 'pattern' column", code="csv")
    rows = []
    for row in reader:
        rows.append(
            {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in row.items()
                if isinstance(value, str)
            }
        )
        if len(rows) > MAX_IMPORT_ROWS:
            raise ProblemError(422, title=f"At most {MAX_IMPORT_ROWS} rows", code="csv")
    return rows


@router.post("/whitelist/import", response_model=ImportResult)
def import_csv(
    context: Writer,
    request: Request,
    file: Annotated[UploadFile, File()],
    dry_run: bool = False,
) -> ImportResult:
    data = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ProblemError(413, title="The file is larger than 1 MB", code="csv")
    brands = {
        brand.name.lower(): brand.id
        for brand in context.session.scalars(
            select(Brand).where(Brand.customer_id == context.customer_id)
        )
    }
    added = skipped = 0
    errors: list[RowError] = []
    known = {(entry.pattern, entry.brand_id) for entry in _entries(context)} if dry_run else set()
    for line, row in enumerate(_read_csv(data), start=2):
        value = row.get("pattern", "")
        try:
            pattern = validate_pattern(value)
        except InvalidHostnameError as exc:
            errors.append(RowError(line=line, value=value[:253], error=str(exc)[:200]))
            continue
        try:
            kind = WhitelistKind((row.get("kind") or "other").lower())
        except ValueError:
            errors.append(RowError(line=line, value=value[:253], error="unknown kind"))
            continue
        brand_name = (row.get("brand") or "").lower()
        if brand_name and brand_name not in brands:
            errors.append(RowError(line=line, value=value[:253], error="unknown brand"))
            continue
        if dry_run:
            key = (pattern, brands.get(brand_name) if brand_name else None)
            if key in known:
                skipped += 1
            else:
                known.add(key)
                added += 1
            continue
        _, created = add_whitelist_entry(
            context,
            pattern=pattern,
            kind=kind,
            note=(row.get("note") or None) and row["note"][:500],
            brand_id=brands.get(brand_name) if brand_name else None,
            source=WhitelistSource.CSV,
        )
        if created:
            added += 1
        else:
            skipped += 1
    if added:
        queue_job(context.session, WHITELIST_CHANGED, customer_id=context.customer_id)
        audit(context, request, "whitelist.imported", "csv", added=added)
    return ImportResult(added=added, skipped=skipped, errors=errors)


@router.get("/whitelist/export", response_class=Response)
def export(context: Owner) -> Response:
    brands = {
        brand.id: brand.name
        for brand in context.session.scalars(
            select(Brand).where(Brand.customer_id == context.customer_id)
        )
    }
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["pattern", "kind", "note", "brand", "source", "created_by", "created_at"])
    for entry in _entries(context):
        writer.writerow(
            [
                csv_cell(value)
                for value in (
                    entry.pattern,
                    entry.kind.value,
                    entry.note,
                    brands.get(entry.brand_id) if entry.brand_id else "",
                    entry.source.value,
                    entry.created_by,
                    entry.created_at.isoformat(),
                )
            ]
        )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="whitelist.csv"'},
    )
