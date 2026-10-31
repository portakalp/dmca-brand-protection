"""``send_notice``: a queued notice through the gates to its recipient (architecture.md §15).

One transaction with the case and the notice locked, so a whitelist dismissal cannot race a
send. A notice goes only while its case is ``approved`` or ``notified`` (a retraction only while
it is ``dismissed``); otherwise it becomes ``rejected``.

The text is rendered once, with a new evidence link, and kept in ``subject`` and ``body``. Then
the gates of ``notices.gates`` in their order:

- a preview row (``dry_run``) is mailed to ``DRY_RUN_REDIRECT_EMAIL`` with a "[DRY RUN]" subject
  and the real recipient, channel and method on top; the envelope is built from the setting
  alone, never from ``recipient``. The row stays ``blocked`` (``dry_run``) with the preview's
  Message-ID; the case stays ``approved``. A web-form channel gets an inert "[DRY RUN]" task;
- a real row while dry-run is on again, or any later gate, is ``blocked`` with the reason;
- e-mail goes through the provider (``email/provider.py``); a web form becomes
  ``manual_pending`` with a ``manual_channel`` task for the operator, who marks it submitted.

A real send makes the notice ``sent``, moves an ``approved`` case to ``notified``, sets
``first_notice_at`` once and ``next_check_at`` to six hours later. A retraction skips the
customer gates, the daily limit and the LOA (K141); when blocked it opens a ``retraction`` task.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import Case, CaseEvent, Customer, Notice, ReviewTask
from kalkan.db.session import Database
from kalkan.db.settings_store import SPECS, read_settings
from kalkan.domain import case_state
from kalkan.domain.enums import (
    BlockedReason,
    CaseState,
    Channel,
    EventType,
    Language,
    NoticeMethod,
    NoticeStatus,
    NoticeTemplate,
    TaskKind,
    TaskStatus,
)
from kalkan.email.provider import Email, EmailProvider, PermanentEmailError, Sent
from kalkan.logging import get_logger
from kalkan.notices.evidence import create_link
from kalkan.notices.facts import notice_facts
from kalkan.notices.gates import customer_gate, effective_dry_run, limit_reached
from kalkan.notices.planning import DRY_RUN_TASK
from kalkan.notices.render import fallback_explanation, legal_review_required, render_notice

SYSTEM = "system"
RECHECK_AFTER = timedelta(hours=6)
SENDABLE = frozenset({CaseState.APPROVED, CaseState.NOTIFIED})
LAST_ACTION = {
    Channel.REGISTRAR: "Registrar notified",
    Channel.HOSTING: "Host notified",
    Channel.REGISTRY: "Registry notified",
    Channel.GOOGLE: "Reported to Google",
}
RETRACTABLE = frozenset({NoticeStatus.SENT, NoticeStatus.ACKNOWLEDGED, NoticeStatus.MANUAL_PENDING})

log = get_logger(__name__)

ProviderFactory = Callable[[], EmailProvider]


@dataclass(frozen=True)
class SendResult:
    notice_id: uuid.UUID
    status: NoticeStatus
    blocked_reason: BlockedReason | None = None


def reply_to(settings: Settings, case: Case) -> str | None:
    if not settings.reply_domain:
        return None
    return f"case+{case.number.lower()}@{settings.reply_domain}"


def _render(
    session: Session, settings: Settings, case: Case, notice: Notice, now: datetime
) -> None:
    inputs = notice_facts(session, case)
    link, url = create_link(session, settings, case, now=now)
    retraction = notice.template is NoticeTemplate.RETRACTION
    english = notice.explanation_en or fallback_explanation(inputs.facts, Language.EN)
    sent_on = None
    if retraction:
        sent_on = session.scalar(
            select(Notice.sent_at)
            .where(
                Notice.case_id == case.id,
                Notice.channel == notice.channel,
                Notice.sent_at.is_not(None),
            )
            .order_by(Notice.sent_at)
            .limit(1)
        )
    rendered = render_notice(
        notice.template,
        notice.channel,
        notice.language,
        inputs.facts,
        explanation_en=english,
        explanation_local=notice.explanation_local,
        evidence_url=url,
        reply_to=reply_to(settings, case) or settings.mail_from or "",
        sent_on=sent_on or now,
    )
    notice.subject = rendered.subject
    notice.body = rendered.body
    notice.evidence_link_id = link.id


def _open_task(session: Session, notice: Notice, kind: TaskKind, note: str, now: datetime) -> None:
    exists = session.scalar(
        select(ReviewTask.id).where(
            ReviewTask.notice_id == notice.id,
            ReviewTask.kind == kind,
            ReviewTask.status == TaskStatus.OPEN,
        )
    )
    if exists is None:
        session.add(
            ReviewTask(
                customer_id=notice.customer_id,
                case_id=notice.case_id,
                notice_id=notice.id,
                kind=kind,
                reason=note[:300],
                note=note[:300] if note.startswith(DRY_RUN_TASK) else None,
                status=TaskStatus.OPEN,
                created_at=now,
                updated_at=now,
            )
        )


def _block(
    session: Session, case: Case, notice: Notice, reason: BlockedReason, now: datetime
) -> SendResult:
    notice.status = NoticeStatus.BLOCKED
    notice.blocked_reason = reason
    notice.updated_at = now
    if case.state is CaseState.APPROVED:
        case.blocked_reason = reason
    if notice.template is NoticeTemplate.RETRACTION and reason is not BlockedReason.DRY_RUN:
        _open_task(
            session,
            notice,
            TaskKind.RETRACTION,
            f"Retract the {notice.channel.value} report by hand (blocked: {reason.value}).",
            now,
        )
    log.info("notices.blocked", notice_id=str(notice.id), reason=reason.value)
    return SendResult(notice.id, notice.status, reason)


def mark_sent(
    session: Session,
    case: Case,
    notice: Notice,
    *,
    now: datetime,
    actor: str = SYSTEM,
    sent: Sent | None = None,
) -> None:
    """The success path of a real notice: by e-mail, or a web form the operator submitted."""
    notice.status = NoticeStatus.SENT
    notice.sent_at = now
    notice.blocked_reason = None
    notice.failure_reason = None
    notice.updated_at = now
    if sent is not None:
        notice.provider_message_id = sent.message_id
        notice.external_ref = sent.provider_id
    if notice.template is NoticeTemplate.RETRACTION:
        session.execute(
            update(Notice)
            .where(
                Notice.case_id == case.id,
                Notice.channel == notice.channel,
                Notice.template != NoticeTemplate.RETRACTION,
                Notice.dry_run.is_(False),
                Notice.status.in_(RETRACTABLE),
            )
            .values(status=NoticeStatus.RETRACTED, updated_at=now)
        )
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=now,
                type=EventType.NOTE,
                actor=actor,
                data={"action": "retraction_sent", "channel": notice.channel.value},
            )
        )
        return
    data = {"channel": notice.channel.value, "method": notice.method.value}
    label = LAST_ACTION.get(notice.channel, "Notice sent")
    if case.state is CaseState.APPROVED:
        case_state.transition(
            session, case, CaseState.NOTIFIED, actor=actor, data=data, at=now, last_action=label
        )
    else:
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=now,
                type=EventType.NOTICE_SENT,
                actor=actor,
                data=data,
            )
        )
        case.last_action = label
        case.last_action_at = now
    if case.first_notice_at is None:
        case.first_notice_at = now
    case.next_check_at = now + RECHECK_AFTER


def _preview(
    session: Session,
    settings: Settings,
    case: Case,
    notice: Notice,
    provider: ProviderFactory,
    now: datetime,
) -> SendResult:
    """Gate 4: the whole notice goes to the operator's mailbox, nowhere else."""
    if notice.method is NoticeMethod.MANUAL_FORM:
        _open_task(
            session,
            notice,
            TaskKind.MANUAL_CHANNEL,
            f"{DRY_RUN_TASK} {notice.channel.value} form: {notice.recipient or 'unknown'}",
            now,
        )
    redirect = settings.dry_run_redirect_email
    if not redirect:
        log.warning("notices.preview_without_mailbox", notice_id=str(notice.id))
    elif notice.provider_message_id is None:
        header = (
            "[DRY RUN] Nothing was sent to the recipient below.\n"
            f"Real recipient: {notice.recipient or '(none: the operator finds one)'}\n"
            f"Channel: {notice.channel.value}\nMethod: {notice.method.value}\n"
            f"Language: {notice.language.value}\n\n"
        )
        try:
            sent = provider().send(
                Email(
                    to=redirect,
                    subject=f"[DRY RUN] {notice.subject}",
                    text=header + (notice.body or ""),
                    reply_to=reply_to(settings, case),
                    tag="dry-run",
                    metadata={"notice_id": str(notice.id), "case": case.number},
                )
            )
        except PermanentEmailError as exc:
            log.warning("notices.preview_failed", notice_id=str(notice.id), error=str(exc))
        else:
            notice.provider_message_id = sent.message_id
            notice.external_ref = sent.provider_id
    return _block(session, case, notice, BlockedReason.DRY_RUN, now)


def _deliver(
    session: Session,
    settings: Settings,
    case: Case,
    notice: Notice,
    provider: ProviderFactory,
    now: datetime,
) -> SendResult:
    if notice.method is NoticeMethod.MANUAL_FORM or not notice.recipient:
        notice.status = NoticeStatus.MANUAL_PENDING
        notice.updated_at = now
        verb = "Retract" if notice.template is NoticeTemplate.RETRACTION else "Submit"
        _open_task(
            session,
            notice,
            TaskKind.RETRACTION
            if notice.template is NoticeTemplate.RETRACTION
            else TaskKind.MANUAL_CHANNEL,
            f"{verb} the {notice.channel.value} report: {notice.recipient or 'find the form'}",
            now,
        )
        return SendResult(notice.id, notice.status)
    try:
        sent = provider().send(
            Email(
                to=notice.recipient,
                subject=notice.subject or "",
                text=notice.body or "",
                reply_to=reply_to(settings, case),
                tag=notice.template.value,
                metadata={"notice_id": str(notice.id), "case": case.number},
            )
        )
    except PermanentEmailError as exc:
        notice.status = NoticeStatus.FAILED
        notice.failure_reason = str(exc)
        notice.updated_at = now
        log.warning("notices.failed", notice_id=str(notice.id), error=str(exc))
        return SendResult(notice.id, notice.status)
    mark_sent(session, case, notice, now=now, sent=sent)
    log.info("notices.sent", notice_id=str(notice.id), channel=notice.channel.value)
    return SendResult(notice.id, notice.status)


def send_notice(
    database: Database,
    settings: Settings,
    provider: ProviderFactory,
    notice_id: uuid.UUID,
    *,
    now: datetime,
) -> SendResult | None:
    """None when the notice is not ``queued`` (a redelivered or early message)."""
    with database.worker_session() as session:
        probe = session.get(Notice, notice_id)
        if probe is None:
            return None
        case = session.get_one(Case, probe.case_id, with_for_update=True)
        notice = session.get_one(Notice, notice_id, with_for_update=True)
        if notice.status is not NoticeStatus.QUEUED:
            return None
        customer = session.get_one(Customer, notice.customer_id)
        if customer.is_demo:
            return None
        retraction = notice.template is NoticeTemplate.RETRACTION
        allowed = case.state is CaseState.DISMISSED if retraction else case.state in SENDABLE
        if not allowed:
            notice.status = NoticeStatus.REJECTED
            notice.failure_reason = f"case is {case.state.value}"
            notice.updated_at = now
            return SendResult(notice.id, notice.status)
        if not retraction:
            gate = customer_gate(customer)
            if gate is not None:
                return _block(session, case, notice, gate, now)
        if notice.body is None:
            _render(session, settings, case, notice, now)
        if notice.dry_run:
            return _preview(session, settings, case, notice, provider, now)
        if effective_dry_run(settings, customer):
            return _block(session, case, notice, BlockedReason.DRY_RUN, now)
        values = read_settings(session)
        if values["sending_enabled"] is not True:
            return _block(session, case, notice, BlockedReason.KILL_SWITCH, now)
        if not retraction:
            limits = dict(values["channel_daily_limits"] or {})
            defaults = dict(SPECS["channel_daily_limits"].default)
            if limit_reached(session, notice.channel, limits, defaults, now):
                return _block(session, case, notice, BlockedReason.DAILY_LIMIT, now)
            if customer.loa_signed_at is None:
                return _block(session, case, notice, BlockedReason.NO_LOA, now)
        domain_tld = notice_facts(session, case).facts.tld
        if legal_review_required(notice.template, notice.channel, notice.language, domain_tld):
            return _block(session, case, notice, BlockedReason.LEGAL_REVIEW, now)
        return _deliver(session, settings, case, notice, provider, now)
