"""Users and invitations, settings and reports (architecture.md §17, product.md §9).

- ``/users``: owners invite by e-mail (a single-use link valid 24 hours); a user is disabled
  (``disabled_at``, sessions ended), never deleted, and no owner can disable themself.
- ``/settings``: the alert e-mail and the time zone; the only customer columns kalkan_api may
  update.
- ``/reports``: the monthly reports and their files; ``/reports/{id}/data`` is the print page's
  data and takes only the single-use ``X-Report-Token`` (worker-browser prints with it).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import available_timezones

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from kalkan.api.problems import ProblemError
from kalkan.api.v1.deps import (
    Context,
    Member,
    Owner,
    Writer,
    customer_tz,
    database,
    now,
    settings,
)
from kalkan.api.v1.files import stream_file
from kalkan.api.v1.mail import send_mail
from kalkan.api.v1.overview import OverviewOut, overview_for
from kalkan.api.v1.queries import ReportCase, report_cases
from kalkan.api.v1.types import EmailAddress
from kalkan.api.v1.writes import audit
from kalkan.db.models import Customer, Invitation, Report, User, UserSession
from kalkan.domain.enums import Category, Channel, UserRole
from kalkan.domain.metrics import taken_down
from kalkan.email.messages import invite_email
from kalkan.reports.content import month_period, takedown_hours
from kalkan.security import hash_token, new_token

router = APIRouter(tags=["account"])
INVITATION_LIFETIME = timedelta(hours=24)
REPORT_TOKEN_HEADER = "X-Report-Token"  # noqa: S105 - a header name


# --- users and invitations ---------------------------------------------------------------------


class UserItem(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: UserRole
    totp_enabled: bool
    last_login_at: datetime | None
    disabled: bool


class InvitationItem(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    expires_at: datetime
    invited_by: str | None


class Users(BaseModel):
    users: list[UserItem]
    invitations: list[InvitationItem]


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailAddress
    role: UserRole = UserRole.VIEWER


def _users(context: Context) -> Users:
    session = context.session
    users = session.scalars(
        select(User).where(User.customer_id == context.customer_id).order_by(User.email)
    ).all()
    invitations = session.scalars(
        select(Invitation)
        .where(
            Invitation.customer_id == context.customer_id,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > context.at,
        )
        .order_by(Invitation.created_at)
    ).all()
    return Users(
        users=[
            UserItem(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=user.role,
                totp_enabled=user.totp_enabled_at is not None,
                last_login_at=user.last_login_at,
                disabled=user.disabled_at is not None,
            )
            for user in users
        ],
        invitations=[
            InvitationItem(
                id=item.id,
                email=item.email,
                role=item.role,
                expires_at=item.expires_at,
                invited_by=item.invited_by,
            )
            for item in invitations
        ],
    )


@router.get("/users", response_model=Users)
def users(context: Owner) -> Users:
    return _users(context)


@router.post("/users", response_model=InvitationItem, status_code=201)
def invite(body: Invite, context: Writer, request: Request) -> InvitationItem:
    session = context.session
    exists = session.scalar(
        select(User.id).where(User.customer_id == context.customer_id, User.email == body.email)
    )
    if exists is not None:
        raise ProblemError(409, title="This person already has an account", code="exists")
    with database(request).api_preauth_session() as preauth:
        elsewhere = preauth.lookup_user(body.email)
    if elsewhere is not None:
        raise ProblemError(409, title="This e-mail already has an account", code="exists")
    session.execute(
        delete(Invitation).where(
            Invitation.customer_id == context.customer_id,
            Invitation.email == body.email,
            Invitation.accepted_at.is_(None),
        )
    )
    token = new_token()
    invitation = Invitation(
        id=uuid.uuid4(),
        customer_id=context.customer_id,
        email=body.email,
        role=body.role,
        token_hash=hash_token(token),
        expires_at=context.at + INVITATION_LIFETIME,
        invited_by=context.user.email,
        created_at=context.at,
    )
    session.add(invitation)
    session.flush()
    audit(context, request, "user.invited", body.email, role=body.role.value)
    url = f"{settings(request).app_base_url or ''}/invite?token={token}"
    send_mail(
        request,
        invite_email(
            body.email, customer=context.customer.name, inviter=context.user.email, url=url
        ),
    )
    return InvitationItem(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        invited_by=invitation.invited_by,
    )


@router.delete("/users/{user_id}", response_model=None, status_code=204)
def disable(user_id: uuid.UUID, context: Writer, request: Request) -> None:
    session = context.session
    user = session.get(User, user_id)
    if user is None or user.customer_id != context.customer_id:
        raise ProblemError(404, title="No such user", code="not_found")
    if user.id == context.user.id:
        raise ProblemError(409, title="You cannot remove yourself", code="self")
    if user.disabled_at is None:
        user.disabled_at = context.at
        session.execute(delete(UserSession).where(UserSession.user_id == user.id))
        audit(context, request, "user.disabled", user.email)


@router.delete("/users/invitations/{invitation_id}", response_model=None, status_code=204)
def withdraw(invitation_id: uuid.UUID, context: Writer, request: Request) -> None:
    invitation = context.session.get(Invitation, invitation_id)
    if invitation is None or invitation.customer_id != context.customer_id:
        raise ProblemError(404, title="No such invitation", code="not_found")
    context.session.delete(invitation)
    audit(context, request, "user.invitation_withdrawn", invitation.email)


# --- settings ----------------------------------------------------------------------------------


class SettingsOut(BaseModel):
    alert_email: str | None
    timezone: str


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_email: EmailAddress | None = None
    timezone: str | None = Field(default=None, max_length=64)
    clear_alert_email: bool = False


@router.get("/settings", response_model=SettingsOut)
def get_settings(context: Owner) -> SettingsOut:
    return SettingsOut(alert_email=context.customer.alert_email, timezone=context.customer.timezone)


@router.patch("/settings", response_model=SettingsOut)
def patch_settings(body: SettingsPatch, context: Writer, request: Request) -> SettingsOut:
    customer: Customer = context.customer
    if body.timezone is not None:
        if body.timezone not in available_timezones():
            raise ProblemError(422, title="Unknown time zone", code="timezone")
        customer.timezone = body.timezone
    if body.clear_alert_email:
        customer.alert_email = None
    elif body.alert_email is not None:
        customer.alert_email = body.alert_email
    customer.updated_at = context.at
    audit(
        context,
        request,
        "settings.changed",
        customer.slug,
        alert_email=customer.alert_email or "",
        timezone=customer.timezone,
    )
    return SettingsOut(alert_email=customer.alert_email, timezone=customer.timezone)


# --- reports -----------------------------------------------------------------------------------


class ReportItem(BaseModel):
    id: uuid.UUID
    period: str
    generated_at: datetime | None
    pdf: bool
    csv: bool


class ReportThreat(BaseModel):
    """A threat of the month that is taken down (the report's closed list)."""

    number: str
    domain: str
    category: Category | None
    first_seen_at: datetime
    first_notice_at: datetime | None
    taken_down_at: datetime | None
    takedown_hours: float | None
    channel: Channel | None


class ReportData(BaseModel):
    report: ReportItem
    customer: str
    timezone: str
    overview: OverviewOut
    taken_down: list[ReportThreat]


def _report_item(report: Report) -> ReportItem:
    return ReportItem(
        id=report.id,
        period=report.period,
        generated_at=report.generated_at,
        pdf=report.pdf_key is not None,
        csv=report.csv_key is not None,
    )


@router.get("/reports", response_model=list[ReportItem])
def reports(context: Member) -> list[ReportItem]:
    rows = context.session.scalars(
        select(Report)
        .where(Report.customer_id == context.customer_id)
        .order_by(Report.period.desc())
    ).all()
    return [_report_item(row) for row in rows]


@router.get("/reports/{report_id}/download", response_class=StreamingResponse)
def download(
    report_id: uuid.UUID,
    context: Member,
    request: Request,
    format: Annotated[str, Query(pattern="^(pdf|csv)$")] = "pdf",
) -> StreamingResponse:
    report = context.session.get(Report, report_id)
    if report is None or report.customer_id != context.customer_id:
        raise ProblemError(404, title="No such report", code="not_found")
    key = report.pdf_key if format == "pdf" else report.csv_key
    if key is None:
        raise ProblemError(404, title="The report has no such file yet", code="not_found")
    content_type = "application/pdf" if format == "pdf" else "text/csv"
    name = f"report-{report.period}.{format}"
    response = stream_file(request, key, content_type, name)
    response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


def _taken_down(rows: list[ReportCase]) -> list[ReportThreat]:
    return [
        ReportThreat(
            number=row.number,
            domain=row.domain,
            category=row.category,
            first_seen_at=row.facts.first_seen_at,
            first_notice_at=row.facts.first_notice_at,
            taken_down_at=row.facts.offline_since,
            takedown_hours=takedown_hours(row),
            channel=row.facts.closing_channel,
        )
        for row in rows
        if taken_down(row.facts)
    ]


@router.get("/reports/{report_id}/data", response_model=ReportData)
def report_data(
    report_id: uuid.UUID,
    request: Request,
    token: Annotated[str | None, Header(alias=REPORT_TOKEN_HEADER, max_length=200)] = None,
) -> ReportData:
    """The print page's data; the token is spent by this call (``resolve_report_token``)."""
    if not token:
        raise ProblemError(401, title="Unauthorized", code="report_token")
    db = database(request)
    with db.api_preauth_session() as preauth:
        found = preauth.resolve_report_token(hash_token(token))
    if found is None or found.report_id != report_id:
        raise ProblemError(401, title="Unauthorized", code="report_token")
    with db.api_session(found.customer_id) as session:
        report = session.get_one(Report, found.report_id)
        customer = session.get_one(Customer, found.customer_id)
        period = month_period(report.period)
        data = overview_for(session, customer, now(), period, None)
        start, after = period.bounds(customer_tz(customer))
        rows = report_cases(session, customer.id, start, after)
        return ReportData(
            report=_report_item(report),
            customer=customer.name,
            timezone=str(customer_tz(customer)),
            overview=data,
            taken_down=_taken_down(rows),
        )
