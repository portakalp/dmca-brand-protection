"""Replies to our notices (architecture.md §16 "Gelen cevaplar ve bounce'lar").

A Postmark inbound payload is untrusted. It is read into :class:`Inbound`, kept as an ``.eml``
in the evidence bucket and matched to its case:

1. the plus address we gave as Reply-To (``case+klk-000123@<REPLY_DOMAIN>``, Postmark's
   ``MailboxHash``), only on our reply domain;
2. otherwise a case number in the subject (``[KLK-000123]``).

Within the case the notice is the one whose Message-ID the reply names (``In-Reply-To``,
``References``), else the newest notice to the sender's address, else none.

The classification (``followup.classify``, Haiku) only chooses among harmless actions
(:func:`apply_reply`); no reply sends anything:

- ``removed``: the notice is acknowledged and the site is checked again at once;
- ``needs_info`` and ``other``: an operator task;
- ``not_our_customer``: the notice is ``rejected`` and the operator gets a task that names the
  channels the plan has not tried yet; the decision stays with the operator (K145);
- ``auto_reply``: nothing.

A classification below :data:`MIN_CONFIDENCE` counts as ``other``. A reply to a dry-run
preview is classified and recorded, but it changes no notice and its task says "[DRY RUN]".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import Case, CaseEvent, Message, Notice, ReviewTask
from kalkan.db.outbox import RECHECK, queue_job
from kalkan.domain.enums import (
    EventType,
    NoticeStatus,
    NoticeTemplate,
    ReplyClass,
    TaskKind,
    TaskStatus,
)
from kalkan.logging import get_logger
from kalkan.notices.facts import channel_facts, notice_basis
from kalkan.notices.planner import plan_levels
from kalkan.notices.planning import DRY_RUN_TASK

MIN_CONFIDENCE = 0.6
MAX_TEXT = 100_000
MAX_FIELD = 1000
_MESSAGE_ID = re.compile(r"<[^<>\s]{1,300}>")
ANSWERED = frozenset({NoticeStatus.SENT, NoticeStatus.MANUAL_PENDING, NoticeStatus.ACKNOWLEDGED})

log = get_logger(__name__)


@dataclass(frozen=True)
class Inbound:
    message_id: str
    from_addr: str
    from_name: str
    to_addrs: tuple[str, ...]
    mailbox_hashes: tuple[str, ...]
    subject: str
    text: str
    reply_text: str  # Postmark's StrippedTextReply: the new part of a reply, when it found one
    in_reply_to: tuple[str, ...]
    date: str
    headers: tuple[tuple[str, str], ...]


def _text(value: Any, limit: int = MAX_FIELD) -> str:
    return str(value)[:limit] if isinstance(value, (str, int)) else ""


def _people(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def parse_inbound(payload: dict[str, Any]) -> Inbound:
    headers: list[tuple[str, str]] = []
    for item in _people(payload.get("Headers")):
        headers.append((_text(item.get("Name"), 200), _text(item.get("Value"), 4000)))
    referenced: list[str] = []
    for name, value in headers:
        if name.lower() in ("in-reply-to", "references"):
            referenced += _MESSAGE_ID.findall(value)
    people = _people(payload.get("ToFull")) + _people(payload.get("CcFull"))
    recipients = [_text(person.get("Email")).lower() for person in people]
    original = _text(payload.get("OriginalRecipient")).lower()
    if original:
        recipients.append(original)
    hashes = [_text(person.get("MailboxHash")) for person in people]
    hashes.append(_text(payload.get("MailboxHash")))
    full = payload.get("FromFull")
    sender: dict[str, Any] = full if isinstance(full, dict) else {}
    return Inbound(
        message_id=_text(payload.get("MessageID"), 300),
        from_addr=(_text(sender.get("Email")) or _text(payload.get("From"))).lower(),
        from_name=_text(sender.get("Name") or payload.get("FromName"), 300),
        to_addrs=tuple(dict.fromkeys(address for address in recipients if address)),
        mailbox_hashes=tuple(dict.fromkeys(value.lower() for value in hashes if value)),
        subject=_text(payload.get("Subject"), 1000),
        text=_text(payload.get("TextBody"), MAX_TEXT),
        reply_text=_text(payload.get("StrippedTextReply"), MAX_TEXT),
        in_reply_to=tuple(dict.fromkeys(referenced)),
        date=_text(payload.get("Date"), 200),
        headers=tuple(headers),
    )


def raw_email(payload: dict[str, Any], inbound: Inbound) -> bytes:
    """The message as an ``.eml``: Postmark's raw source when the server sends it
    (``RawEmail``), otherwise rebuilt from the parsed fields without attachments (K146)."""
    raw = payload.get("RawEmail")
    if isinstance(raw, str) and raw:
        return raw.encode("utf-8", "replace")
    message = EmailMessage()
    message["From"] = formataddr((inbound.from_name, inbound.from_addr))
    message["To"] = ", ".join(inbound.to_addrs)
    message["Subject"] = inbound.subject.replace("\r", " ").replace("\n", " ")
    if inbound.date:
        message["Date"] = inbound.date.replace("\r", " ").replace("\n", " ")
    if inbound.message_id:
        message["X-Postmark-Message-ID"] = inbound.message_id
    if inbound.in_reply_to:
        message["In-Reply-To"] = inbound.in_reply_to[0]
    message.set_content(inbound.text or "")
    html = payload.get("HtmlBody")
    if isinstance(html, str) and html:
        message.add_alternative(html[:MAX_TEXT], subtype="html")
    return message.as_bytes()


def _number_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){prefix}-[A-Za-z0-9]{{4,12}}(?![A-Za-z0-9])", re.I)


def find_case(session: Session, settings: Settings, inbound: Inbound) -> Case | None:
    prefix = settings.case_prefix
    pattern = _number_pattern(prefix)
    numbers: list[str] = []
    domain = (settings.reply_domain or "").lower()
    on_reply_domain = bool(domain) and any(
        address.endswith("@" + domain) for address in inbound.to_addrs
    )
    if on_reply_domain:
        for value in inbound.mailbox_hashes:
            if value.startswith("case+"):
                value = value[len("case+") :]
            numbers += pattern.findall(value)
        for address in inbound.to_addrs:
            local, _, host = address.partition("@")
            if host == domain and local.startswith("case+"):
                numbers += pattern.findall(local[len("case+") :])
    numbers += pattern.findall(inbound.subject)
    for number in dict.fromkeys(numbers):
        spellings = {number, number.upper()}
        case = session.scalar(select(Case).where(Case.number.in_(spellings)).limit(1))
        if case is not None:
            return case
    return None


def find_notice(session: Session, case: Case, inbound: Inbound) -> Notice | None:
    if inbound.in_reply_to:
        notice = session.scalar(
            select(Notice).where(
                Notice.case_id == case.id,
                Notice.provider_message_id.in_(inbound.in_reply_to),
            )
        )
        if notice is not None:
            return notice
    if not inbound.from_addr:
        return None
    sender_domain = inbound.from_addr.rpartition("@")[2]
    candidates = session.scalars(
        select(Notice)
        .where(
            Notice.case_id == case.id,
            Notice.template != NoticeTemplate.RETRACTION,
            Notice.recipient.is_not(None),
        )
        .order_by(Notice.created_at.desc())
    ).all()
    for notice in candidates:
        if (notice.recipient or "").lower() == inbound.from_addr:
            return notice
    for notice in candidates:
        if (notice.recipient or "").lower().rpartition("@")[2] == sender_domain:
            return notice
    return None


def _task(
    session: Session,
    message: Message,
    kind: TaskKind,
    reason: str,
    *,
    dry_run: bool,
    now: datetime,
) -> None:
    note = f"{DRY_RUN_TASK} {reason}" if dry_run else None
    session.add(
        ReviewTask(
            customer_id=message.customer_id,
            case_id=message.case_id,
            notice_id=message.notice_id,
            kind=kind,
            reason=(note or reason)[:300],
            note=note[:300] if note else None,
            status=TaskStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )


def _untried_channels(session: Session, case: Case, notice: Notice) -> list[str]:
    tried = set(
        session.scalars(
            select(Notice.channel).where(
                Notice.case_id == case.id,
                Notice.round == notice.round,
                Notice.dry_run.is_(notice.dry_run),
            )
        ).all()
    )
    channels: list[str] = []
    for level in plan_levels(channel_facts(session, case, notice_basis(session, case.id))):
        for target in level.targets:
            if target.channel not in tried and target.channel.value not in channels:
                channels.append(target.channel.value)
    return channels


@dataclass(frozen=True)
class ReplyAction:
    category: ReplyClass
    action: str


def effective_class(category: ReplyClass | None, confidence: float | None) -> ReplyClass:
    if category is None or (confidence is not None and confidence < MIN_CONFIDENCE):
        return ReplyClass.OTHER
    return category


def apply_reply(
    session: Session,
    message: Message,
    category: ReplyClass | None,
    confidence: float | None,
    *,
    summary: str = "",
    actor: str = "system",
    now: datetime,
) -> ReplyAction:
    """The action of a classified reply, inside the caller's transaction."""
    case = session.get_one(Case, message.case_id, with_for_update=True)
    notice = session.get(Notice, message.notice_id) if message.notice_id else None
    chosen = effective_class(category, confidence)
    dry_run = notice is not None and notice.dry_run
    sender = message.from_addr or "unknown sender"
    about = f"{notice.channel.value} " if notice is not None else ""
    action = "none"
    if chosen is ReplyClass.REMOVED:
        if notice is not None and not dry_run and notice.status in ANSWERED:
            notice.status = NoticeStatus.ACKNOWLEDGED
            notice.updated_at = now
        if not dry_run:
            queue_job(session, RECHECK, case_id=case.id, customer_id=case.customer_id)
            action = "recheck"
    elif chosen is ReplyClass.NEEDS_INFO:
        _task(
            session,
            message,
            TaskKind.NEEDS_INFO,
            f"The {about}recipient ({sender}) asks for more information. {summary}".strip(),
            dry_run=dry_run,
            now=now,
        )
        action = "task"
    elif chosen is ReplyClass.NOT_OUR_CUSTOMER:
        if notice is not None and not dry_run and notice.status in ANSWERED:
            notice.status = NoticeStatus.REJECTED
            notice.failure_reason = "the recipient says the site is not its customer"
            notice.updated_at = now
        untried = _untried_channels(session, case, notice) if notice is not None else []
        suggestion = f" Not tried yet: {', '.join(untried)}." if untried else ""
        _task(
            session,
            message,
            TaskKind.REPLY,
            f"The {about}recipient ({sender}) says the site is not its customer.{suggestion}",
            dry_run=dry_run,
            now=now,
        )
        action = "task"
    elif chosen is ReplyClass.OTHER:
        _task(
            session,
            message,
            TaskKind.REPLY,
            f"Read the reply from {sender}. {summary}".strip(),
            dry_run=dry_run,
            now=now,
        )
        action = "task"
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=now,
            type=EventType.REPLY_RECEIVED,
            actor=actor,
            data={
                "message_id": str(message.id),
                "classification": chosen.value,
                "channel": notice.channel.value if notice is not None else None,
                "dry_run": dry_run,
                "action": action,
            },
        )
    )
    log.info(
        "replies.applied",
        message_id=str(message.id),
        case_id=str(case.id),
        classification=chosen.value,
        action=action,
    )
    return ReplyAction(chosen, action)
