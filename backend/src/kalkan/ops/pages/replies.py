"""Replies to our notices (architecture.md §16, §19).

- ``/replies``: inbound e-mail that matched no case (``webhook_events`` with ``error =
  'unmatched'``), and the latest replies linked to cases with their classification.
- ``POST /replies/{event_id}/link``: links an unmatched reply to a case by its number; the reply
  then goes to ``ai_classify_reply`` through the outbox.
- ``POST /messages/{message_id}/classification``: the operator corrects the class; its action
  runs again (``followup.replies.apply_reply``) with the operator as actor.
Every write is audited. The e-mail text is shown escaped; it is untrusted.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select

from kalkan.db.models import AuditLog, Case, Message, Notice, WebhookEvent
from kalkan.db.outbox import CLASSIFY_REPLY, queue_job
from kalkan.domain.enums import ReplyClass, WebhookSource
from kalkan.followup.replies import apply_reply, parse_inbound
from kalkan.followup.webhooks import UNMATCHED, link_message
from kalkan.ops import auth
from kalkan.ops.web import (
    client_ip,
    csrf_guard,
    current_staff,
    evidence_bucket,
    now,
    redirect,
    render,
    transaction,
)
from kalkan.storage import paths
from kalkan.storage.gcs import make_key

router = APIRouter()
LIMIT = 100
Staff = Annotated[auth.Staff, Depends(current_staff)]
Guarded = Annotated[auth.Staff, Depends(csrf_guard)]


@router.get("/replies")
def replies(request: Request, _: Staff) -> Response:
    with transaction(request) as session:
        events = session.scalars(
            select(WebhookEvent)
            .where(
                WebhookEvent.source == WebhookSource.POSTMARK_INBOUND,
                WebhookEvent.error == UNMATCHED,
            )
            .order_by(WebhookEvent.received_at.desc())
            .limit(LIMIT)
        ).all()
        unmatched = [(event, parse_inbound(event.payload)) for event in events]
        linked = session.execute(
            select(Message, Case.number, Notice)
            .join(Case, Case.id == Message.case_id)
            .outerjoin(Notice, Notice.id == Message.notice_id)
            .order_by(Message.received_at.desc())
            .limit(LIMIT)
        ).all()
    return render(
        request,
        "replies.html",
        {"unmatched": unmatched, "linked": linked, "classes": list(ReplyClass)},
    )


@router.post("/replies/{event_id}/link")
def link(
    request: Request,
    event_id: uuid.UUID,
    staff: Guarded,
    number: Annotated[str, Form(max_length=40)],
) -> Response:
    at = now()
    with transaction(request) as session:
        event = session.get(WebhookEvent, event_id, with_for_update=True)
        if event is None or event.source is not WebhookSource.POSTMARK_INBOUND:
            return redirect("/replies", "Error: no such reply.")
        if event.error != UNMATCHED:
            return redirect("/replies", "Error: the reply is already linked.")
        typed = number.strip()
        case = session.scalar(select(Case).where(Case.number.in_({typed, typed.upper()})).limit(1))
        if case is None:
            return redirect("/replies", "Error: no case with that number.")
        key = make_key(evidence_bucket(request), paths.message_raw(event.id))
        message = link_message(session, event, case, raw_key=key, now=at)
        queue_job(session, CLASSIFY_REPLY, message_id=message.id, customer_id=case.customer_id)
        session.add(
            AuditLog(
                actor=staff.email,
                action="reply.linked",
                customer_id=case.customer_id,
                target=case.number,
                data={"event_id": str(event.id)},
                ip=client_ip(request),
                at=at,
            )
        )
    return redirect("/replies", f"Linked to {case.number}; classification queued.")


@router.post("/messages/{message_id}/classification")
def classify(
    request: Request,
    message_id: uuid.UUID,
    staff: Guarded,
    classification: Annotated[str, Form(max_length=40)],
) -> Response:
    try:
        chosen = ReplyClass(classification)
    except ValueError:
        return redirect("/replies", "Error: unknown class.")
    at = now()
    with transaction(request) as session:
        message = session.get(Message, message_id, with_for_update=True)
        if message is None:
            return redirect("/replies", "Error: no such reply.")
        before = message.classification
        message.classification = chosen
        message.classification_confidence = None
        result = apply_reply(session, message, chosen, None, actor=staff.email, now=at)
        session.add(
            AuditLog(
                actor=staff.email,
                action="reply.classified",
                customer_id=message.customer_id,
                target=str(message.id),
                data={
                    "from": before.value if before else None,
                    "to": chosen.value,
                    "action": result.action,
                },
                ip=client_ip(request),
                at=at,
            )
        )
    return redirect("/replies", f"Classified as {chosen.value.replace('_', ' ')}.")
