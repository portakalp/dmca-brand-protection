"""``process_webhook_event(event_id)``: Postmark's inbound and bounce webhooks (§16).

The API only stores the payload (``webhook_events``) with an outbox row; this runs in
worker-default with the worker role.

- Inbound: the raw message goes to the evidence bucket (``messages/<event id>.eml``), the case
  is found (``followup.replies``) and a ``messages`` row is written with the event's id, so a
  retried event writes nothing twice. The reply then goes to ``ai_classify_reply`` (worker-ai).
  A message Postmark already delivered under another event (its retries) is a duplicate. A
  reply that reaches us more than 7 days after it was received (a long outage) is not
  classified; the operator gets a task instead (K146).
- Bounce: the notice whose Postmark ``MessageID`` the bounce names becomes ``failed`` with the
  bounce as ``failure_reason`` and the operator gets a ``bounce`` task. A bounce of a dry-run
  preview only marks the preview.
- Whatever matches nothing is marked processed with ``error = 'unmatched'`` and waits on the
  ops panel's Replies page.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import Case, CaseEvent, Message, Notice, ReviewTask, WebhookEvent
from kalkan.db.session import Database
from kalkan.domain.enums import (
    EventType,
    MessageDirection,
    NoticeStatus,
    TaskKind,
    TaskStatus,
    WebhookSource,
)
from kalkan.followup.replies import Inbound, find_case, find_notice, parse_inbound, raw_email
from kalkan.logging import get_logger
from kalkan.storage import paths
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, make_key, sha256_hex

UNMATCHED = "unmatched"
DUPLICATE = "duplicate"
TOO_OLD = timedelta(days=7)
MAX_BODY = 100_000

log = get_logger(__name__)


class Outcome(StrEnum):
    CLASSIFY = "classify"  # a message waits for ai_classify_reply
    TASK = "task"  # handed to the operator
    BOUNCED = "bounced"
    UNMATCHED = "unmatched"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    SKIPPED = "skipped"  # already processed or unknown


@dataclass(frozen=True)
class Processed:
    event_id: uuid.UUID
    outcome: Outcome
    message_id: uuid.UUID | None = None


def store_raw(store: ObjectStore, bucket: str, event_id: uuid.UUID, data: bytes) -> str:
    path = paths.message_raw(event_id)
    try:
        return store.put(bucket, path, data, "message/rfc822").key
    except ObjectExistsError:
        key = make_key(bucket, path)
        if store.stored_sha256(key) != sha256_hex(data):
            raise
        return key


def _duplicate(session: Session, event: WebhookEvent, inbound: Inbound) -> bool:
    if not inbound.message_id:
        return False
    found = session.scalar(
        select(WebhookEvent.id)
        .where(
            WebhookEvent.id != event.id,
            WebhookEvent.source == event.source,
            WebhookEvent.processed_at.is_not(None),
            WebhookEvent.error.is_distinct_from(DUPLICATE),
            text("webhook_events.payload->>'MessageID' = :message_id").bindparams(
                message_id=inbound.message_id
            ),
        )
        .limit(1)
    )
    return found is not None


def link_message(
    session: Session,
    event: WebhookEvent,
    case: Case,
    *,
    raw_key: str | None,
    now: datetime,
) -> Message:
    """The ``messages`` row of an inbound event; its id is the event's."""
    inbound = parse_inbound(event.payload)
    notice = find_notice(session, case, inbound)
    message = session.get(Message, event.id)
    if message is None:
        message = Message(
            id=event.id,
            customer_id=case.customer_id,
            case_id=case.id,
            notice_id=notice.id if notice is not None else None,
            direction=MessageDirection.INBOUND,
            from_addr=inbound.from_addr or None,
            to_addr=", ".join(inbound.to_addrs)[:1000] or None,
            subject=inbound.subject or None,
            body_text=(inbound.reply_text or inbound.text)[:MAX_BODY] or None,
            raw_key=raw_key,
            received_at=event.received_at,
        )
        session.add(message)
        session.flush()
    event.processed_at = now
    event.error = None
    return message


def _too_old_task(session: Session, message: Message, now: datetime) -> None:
    session.add(
        ReviewTask(
            customer_id=message.customer_id,
            case_id=message.case_id,
            notice_id=message.notice_id,
            kind=TaskKind.REPLY,
            reason=f"A reply from {message.from_addr or 'unknown sender'} arrived late; read it.",
            status=TaskStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )


def _inbound(
    session: Session,
    store: ObjectStore,
    bucket: str,
    settings: Settings,
    event: WebhookEvent,
    now: datetime,
) -> Processed:
    inbound = parse_inbound(event.payload)
    if _duplicate(session, event, inbound):
        event.processed_at, event.error = now, DUPLICATE
        return Processed(event.id, Outcome.DUPLICATE)
    raw_key = store_raw(store, bucket, event.id, raw_email(event.payload, inbound))
    case = find_case(session, settings, inbound)
    if case is None:
        event.processed_at, event.error = now, UNMATCHED
        log.info("webhooks.unmatched", event_id=str(event.id))
        return Processed(event.id, Outcome.UNMATCHED)
    message = link_message(session, event, case, raw_key=raw_key, now=now)
    if now - event.received_at > TOO_OLD:
        _too_old_task(session, message, now)
        return Processed(event.id, Outcome.TASK, message.id)
    return Processed(event.id, Outcome.CLASSIFY, message.id)


def _bounce(session: Session, event: WebhookEvent, now: datetime) -> Processed:
    payload: dict[str, Any] = event.payload
    if payload.get("RecordType", "Bounce") != "Bounce":
        event.processed_at, event.error = now, "not a bounce"
        return Processed(event.id, Outcome.IGNORED)
    provider_id = str(payload.get("MessageID") or "")[:300]
    notice = (
        session.scalar(select(Notice).where(Notice.external_ref == provider_id).limit(1))
        if provider_id
        else None
    )
    if notice is None:
        event.processed_at, event.error = now, UNMATCHED
        return Processed(event.id, Outcome.UNMATCHED)
    kind = str(payload.get("Type") or "Bounce")[:60]
    description = str(payload.get("Description") or "")[:300]
    reason = f"bounce: {kind}: {description}".strip(": ")[:500]
    event.processed_at, event.error = now, None
    if notice.dry_run:
        notice.failure_reason = reason
        return Processed(event.id, Outcome.BOUNCED)
    if notice.status is not NoticeStatus.FAILED:
        notice.status = NoticeStatus.FAILED
        notice.failure_reason = reason
        notice.updated_at = now
        session.add(
            ReviewTask(
                customer_id=notice.customer_id,
                case_id=notice.case_id,
                notice_id=notice.id,
                kind=TaskKind.BOUNCE,
                reason=(
                    f"The {notice.channel.value} notice to {notice.recipient} bounced "
                    f"({kind}). Find another address and send it again."
                )[:300],
                status=TaskStatus.OPEN,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            CaseEvent(
                customer_id=notice.customer_id,
                case_id=notice.case_id,
                at=now,
                type=EventType.NOTE,
                actor="system",
                data={"action": "bounce", "channel": notice.channel.value, "type": kind},
            )
        )
    log.info("webhooks.bounced", event_id=str(event.id), notice_id=str(notice.id))
    return Processed(event.id, Outcome.BOUNCED)


def process_webhook_event(
    database: Database,
    settings: Settings,
    store: Callable[[], tuple[ObjectStore, str]],
    event_id: uuid.UUID,
    *,
    now: datetime,
) -> Processed:
    """Process one stored event once; ``store`` is called only for inbound mail."""
    with database.worker_session() as session:
        event = session.get(WebhookEvent, event_id, with_for_update=True)
        if event is None or event.processed_at is not None:
            return Processed(event_id, Outcome.SKIPPED)
        if event.source is WebhookSource.POSTMARK_INBOUND:
            objects, bucket = store()
            return _inbound(session, objects, bucket, settings, event, now)
        if event.source is WebhookSource.POSTMARK_BOUNCE:
            return _bounce(session, event, now)
        return Processed(event_id, Outcome.SKIPPED)
