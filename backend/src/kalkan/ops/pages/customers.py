"""Customers and onboarding (architecture.md §19, product.md §8).

Every change goes through :mod:`kalkan.admin` (K53). Flag and LOA changes need the
confirmation box ticked; the server checks it, not only the browser.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan import admin
from kalkan.db.base import new_id
from kalkan.db.models import (
    AuditLog,
    Brand,
    BrandAsset,
    Customer,
    Invitation,
    OfficialDomain,
    Report,
    WhitelistEntry,
)
from kalkan.domain.enums import BrandAssetKind, OfficialDomainKind, UserRole
from kalkan.ops import auth, uploads
from kalkan.ops.web import (
    client_ip,
    csrf_guard,
    current_staff,
    evidence_bucket,
    now,
    redirect,
    render,
    services,
    transaction,
)
from kalkan.reports import build as report_build
from kalkan.storage import paths

router = APIRouter()

Staff = Annotated[auth.Staff, Depends(current_staff)]
Writer = Annotated[auth.Staff, Depends(csrf_guard)]
Text = Annotated[str, Form()]
Optional = Annotated[str, Form()]
CONFIRM = "yes"
REPORTS_SHOWN = 12


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def _customer(session: Session, customer_id: uuid.UUID) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404)
    return customer


def _brand(session: Session, brand_id: uuid.UUID) -> Brand:
    brand = session.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status_code=404)
    return brand


def _page(customer_id: uuid.UUID) -> str:
    return f"/customers/{customer_id}"


@router.get("/customers")
def customers(request: Request, _: Staff) -> Response:
    with transaction(request) as session:
        rows = session.execute(
            select(Customer, func.count(Brand.id))
            .outerjoin(Brand, Brand.customer_id == Customer.id)
            .group_by(Customer.id)
            .order_by(Customer.name)
        ).all()
    return render(request, "customers.html", {"rows": rows})


@router.post("/customers")
def create_customer(
    request: Request,
    staff: Writer,
    name: Text,
    slug: Optional = "",
    alert_email: Optional = "",
    timezone: Optional = "UTC",
    monitor_only: Optional = "",
) -> Response:
    with transaction(request) as session:
        try:
            customer = admin.create_customer(
                session,
                name=name.strip(),
                slug=slug.strip() or None,
                actor=staff.email,
                monitor_only=monitor_only == "on",
                alert_email=alert_email.strip() or None,
                timezone=timezone.strip() or "UTC",
            )
        except admin.AdminError as exc:
            return redirect("/customers", f"Error: {exc}")
        customer_id = customer.id
    return redirect(_page(customer_id), "Customer created: trial, dry-run on, sending off.")


@router.get("/customers/{customer_id}")
def customer_page(request: Request, customer_id: uuid.UUID, _: Staff) -> Response:
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        brands = list(
            session.scalars(
                select(Brand).where(Brand.customer_id == customer_id).order_by(Brand.created_at)
            )
        )
        brand_ids = [brand.id for brand in brands]
        domains: dict[uuid.UUID, list[OfficialDomain]] = {}
        for official in session.scalars(
            select(OfficialDomain)
            .where(OfficialDomain.brand_id.in_(brand_ids))
            .order_by(OfficialDomain.domain)
        ):
            domains.setdefault(official.brand_id, []).append(official)
        assets: dict[uuid.UUID, list[BrandAsset]] = {}
        for asset in session.scalars(
            select(BrandAsset)
            .where(BrandAsset.brand_id.in_(brand_ids))
            .order_by(BrandAsset.created_at.desc())
        ):
            assets.setdefault(asset.brand_id, []).append(asset)
        whitelist = session.execute(
            select(WhitelistEntry.kind, func.count())
            .where(WhitelistEntry.customer_id == customer_id)
            .group_by(WhitelistEntry.kind)
        ).all()
        invitations = list(
            session.scalars(
                select(Invitation)
                .where(Invitation.customer_id == customer_id)
                .order_by(Invitation.created_at.desc())
            )
        )
        reports = list(
            session.scalars(
                select(Report)
                .where(Report.customer_id == customer_id)
                .order_by(Report.period.desc())
                .limit(REPORTS_SHOWN)
            )
        )
        settings = services(request).settings
        context = {
            "customer": customer,
            "brands": brands,
            "domains": domains,
            "assets": assets,
            "whitelist": whitelist,
            "invitations": invitations,
            "reports": reports,
            "report_period": report_build.last_month(customer, now()),
            "flags": admin.FLAGS,
            "asset_kinds": admin.ASSET_KINDS,
            "domain_kinds": list(OfficialDomainKind),
            "roles": list(UserRole),
            "now": now(),
            "notices_dry_run": settings.notices_dry_run,
        }
    return render(request, "customer.html", context)


@router.post("/customers/{customer_id}/brands")
def add_brand(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    name: Text,
    keywords: Optional = "",
    variants: Optional = "",
    markets: Optional = "",
    languages: Optional = "",
) -> Response:
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        try:
            admin.add_brand(
                session,
                customer,
                name=name.strip(),
                actor=staff.email,
                keywords=_split(keywords),
                variants=_split(variants),
                target_markets=_split(markets),
                languages=_split(languages),
            )
        except admin.AdminError as exc:
            return redirect(_page(customer_id), f"Error: {exc}")
    return redirect(_page(customer_id), f"Brand {name.strip()} added.")


@router.post("/brands/{brand_id}/domains")
def add_domain(
    request: Request, brand_id: uuid.UUID, staff: Writer, domain: Text, kind: Text
) -> Response:
    with transaction(request) as session:
        brand = _brand(session, brand_id)
        customer_id = brand.customer_id
        try:
            chosen = OfficialDomainKind(kind)
            official = admin.add_official_domain(
                session, brand, domain, actor=staff.email, kind=chosen
            )
        except (admin.AdminError, ValueError) as exc:
            return redirect(_page(customer_id), f"Error: {exc}")
        host = official.domain
    return redirect(_page(customer_id), f"Official domain {host} added and whitelisted.")


@router.post("/brands/{brand_id}/assets")
def upload_asset(
    request: Request,
    brand_id: uuid.UUID,
    staff: Writer,
    kind: Text,
    file: Annotated[UploadFile, File()],
) -> Response:
    found = services(request)
    with transaction(request) as session:
        customer_id = _brand(session, brand_id).customer_id
    try:
        chosen = BrandAssetKind(kind)
        if chosen not in admin.ASSET_KINDS:
            raise uploads.UploadError(f"{kind} is not uploaded by hand")
        upload = uploads.read_upload(
            file, max_bytes=uploads.MAX_ASSET_BYTES, allowed=uploads.ASSET_TYPES
        )
    except (uploads.UploadError, ValueError) as exc:
        return redirect(_page(customer_id), f"Error: {exc}")
    bucket = evidence_bucket(request)
    path = paths.brand_asset(brand_id, new_id(), upload.extension)
    stored = found.store.put(bucket, path, upload.data, upload.content_type)
    with transaction(request) as session:
        admin.record_brand_asset(
            session,
            _brand(session, brand_id),
            chosen,
            stored,
            actor=staff.email,
            filename=upload.filename,
        )
    note = " SVG files keep no hash." if upload.extension == "svg" else ""
    return redirect(_page(customer_id), f"{chosen.value} uploaded; hashes follow.{note}")


@router.post("/brands/{brand_id}/references")
def refresh_references(request: Request, brand_id: uuid.UUID, staff: Writer) -> Response:
    with transaction(request) as session:
        brand = _brand(session, brand_id)
        admin.refresh_references(session, brand, actor=staff.email)
        customer_id = brand.customer_id
    return redirect(_page(customer_id), "Reference screenshots queued.")


def _csv_text(file: UploadFile | None, text: str) -> str:
    if file is not None and file.filename:
        data = file.file.read(uploads.MAX_CSV_BYTES + 1)
        if len(data) > uploads.MAX_CSV_BYTES:
            raise uploads.UploadError("the CSV file is larger than 2 MB")
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise uploads.UploadError("the CSV file is not UTF-8") from None
    return text


def _whitelist_brand(session: Session, customer_id: uuid.UUID, brand: str) -> Brand | None:
    if not brand:
        return None
    try:
        brand_id = uuid.UUID(brand)
    except ValueError:
        raise HTTPException(status_code=404) from None
    found = session.get(Brand, brand_id)
    if found is None or found.customer_id != customer_id:
        raise HTTPException(status_code=404)
    return found


@router.post("/customers/{customer_id}/whitelist/preview")
def whitelist_preview(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    brand: Optional = "",
    csv_text: Optional = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """The import run in a savepoint that is rolled back: the same checks, nothing stored."""
    try:
        text = _csv_text(file, csv_text)
    except uploads.UploadError as exc:
        return render(request, "_whitelist_report.html", {"error": str(exc)})
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        scope = _whitelist_brand(session, customer_id, brand)
        savepoint = session.begin_nested()
        report = admin.import_whitelist(session, customer, text, actor=staff.email, brand=scope)
        savepoint.rollback()
    return render(
        request,
        "_whitelist_report.html",
        {
            "report": report,
            "csv_text": text,
            "brand": brand,
            "customer_id": customer_id,
            "preview": True,
        },
    )


@router.post("/customers/{customer_id}/whitelist/import")
def whitelist_import(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    csv_text: Text,
    brand: Optional = "",
) -> Response:
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        scope = _whitelist_brand(session, customer_id, brand)
        report = admin.import_whitelist(session, customer, csv_text, actor=staff.email, brand=scope)
    if report.errors:
        return redirect(
            _page(customer_id), f"Error: {len(report.errors)} invalid rows; nothing imported."
        )
    return redirect(
        _page(customer_id),
        f"Whitelist: {report.imported} imported, {report.already_listed} already listed.",
    )


@router.post("/customers/{customer_id}/flags")
def set_flag(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    flag: Text,
    value: Text,
    confirm: Optional = "",
) -> Response:
    if confirm != CONFIRM:
        return redirect(_page(customer_id), "Error: tick the confirmation box to change a flag.")
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        try:
            change = admin.set_customer_flag(
                session, customer, flag, value == "true", actor=staff.email
            )
        except admin.AdminError as exc:
            return redirect(_page(customer_id), f"Error: {exc}")
    if change is None:
        return redirect(_page(customer_id), f"{flag} already had that value.")
    lifted = " Blocked cases are re-planned." if change.lifts_block else ""
    return redirect(_page(customer_id), f"{flag}: {change.old} → {change.new}.{lifted}")


@router.post("/customers/{customer_id}/reports")
def queue_report(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    period: Annotated[str, Form(max_length=7)],
    confirm: Optional = "",
) -> Response:
    if confirm != CONFIRM:
        return redirect(_page(customer_id), "Error: tick the confirmation box to make a report.")
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        try:
            month = report_build.queue_build(
                session, customer, period.strip(), now=now(), actor=staff.email
            )
        except report_build.ReportError as exc:
            return redirect(_page(customer_id), f"Error: {exc}.")
        session.add(
            AuditLog(
                actor=staff.email,
                action="report.queue",
                customer_id=customer.id,
                target=month,
                data={},
                ip=client_ip(request),
                at=now(),
            )
        )
    return redirect(_page(customer_id), f"The {month} report is being made; refresh in a minute.")


@router.post("/customers/{customer_id}/loa")
def upload_loa(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    signed_on: Text,
    file: Annotated[UploadFile, File()],
    confirm: Optional = "",
) -> Response:
    if confirm != CONFIRM:
        return redirect(_page(customer_id), "Error: tick the confirmation box to record the LOA.")
    try:
        signed = date.fromisoformat(signed_on)
        upload = uploads.read_upload(
            file, max_bytes=uploads.MAX_LOA_BYTES, allowed=uploads.LOA_TYPES
        )
    except (uploads.UploadError, ValueError) as exc:
        return redirect(_page(customer_id), f"Error: {exc}")
    at = now()
    if signed > at.date():
        return redirect(_page(customer_id), "Error: the signature date is in the future.")
    found = services(request)
    with transaction(request) as session:
        _customer(session, customer_id)
    bucket = evidence_bucket(request)
    stored = found.store.put(
        bucket,
        paths.customer_loa(customer_id, at, upload.filename),
        upload.data,
        upload.content_type,
    )
    with transaction(request) as session:
        admin.record_loa(
            session,
            _customer(session, customer_id),
            file_key=stored.key,
            signed_at=datetime.combine(signed, time(12), tzinfo=UTC),
            actor=staff.email,
        )
    return redirect(_page(customer_id), f"LOA signed {signed.isoformat()} recorded.")


@router.post("/customers/{customer_id}/invitations")
def invite(
    request: Request,
    customer_id: uuid.UUID,
    staff: Writer,
    email: Text,
    role: Text = "owner",
) -> Response:
    with transaction(request) as session:
        customer = _customer(session, customer_id)
        try:
            created = admin.invite_user(
                session, customer, email, actor=staff.email, role=UserRole(role)
            )
        except (admin.AdminError, ValueError) as exc:
            return redirect(_page(customer_id), f"Error: {exc}")
    base = (services(request).settings.app_base_url or "").rstrip("/")
    link = f"{base}/invite/{created.token}"
    # Shown once: only the token's hash is stored. The invitation e-mail carries the link.
    return render(
        request,
        "invitation.html",
        {"link": link, "email": created.invitation.email, "customer_id": customer_id},
    )
