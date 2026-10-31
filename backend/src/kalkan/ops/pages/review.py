"""The review queue page (architecture.md §14, §19).

Every decision is a plain form post, so the page also works without ``static/ops.js``; the
shortcuts submit the same buttons. After a decision the next case of the queue opens.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from kalkan.decision.apply import DecisionError, StaffAction, staff_decision
from kalkan.ops import auth, labeling, review
from kalkan.ops.web import (
    client_ip,
    csrf_guard,
    current_staff,
    now,
    redirect,
    render,
    services,
    transaction,
)
from kalkan.scanning.capture_jobs import issue_case_capture

router = APIRouter(prefix="/review")


def _next_url(after: str | None) -> str:
    return f"/review?after={quote(after)}" if after else "/review"


@router.get("")
def queue(
    request: Request,
    _: Annotated[auth.Staff, Depends(current_staff)],
    after: str | None = None,
) -> Response:
    with transaction(request) as session:
        case = review.next_case(session, after)
        if case is None and after:
            # The end of this pass: start again from the skipped cases.
            case = review.next_case(session)
            after = None
        context = {
            "view": review.view(session, case) if case else None,
            "after": after,
            "waiting": review.waiting(session),
            "choices": review.CHOICES,
        }
    return render(request, "review.html", context)


@router.post("/{case_id}")
def decide(
    request: Request,
    case_id: uuid.UUID,
    staff: Annotated[auth.Staff, Depends(csrf_guard)],
    choice: Annotated[str, Form()],
    save_label: Annotated[bool, Form()] = False,
    after: Annotated[str, Form()] = "",
) -> Response:
    at = now()
    ip = client_ip(request)
    try:
        parsed = review.parse_choice(choice)
    except review.ChoiceError as exc:
        return redirect(_next_url(after or None), f"Error: {exc}")
    with transaction(request) as session:
        try:
            result = staff_decision(
                session,
                case_id,
                parsed.action,
                actor=staff.email,
                ip=ip,
                now=at,
                basis=parsed.basis,
                category=parsed.category,
            )
        except DecisionError as exc:
            return redirect(_next_url(after or None), f"Error: {exc}")
        if save_label and result.label is not None:
            labeling.save_label(
                session, case_id, result.label.value, None, actor=staff.email, ip=ip, now=at
            )
        number = result.case.number
        state = result.case.state.value
    message = f"{number}: {parsed.action.value}, now {state}"
    if parsed.action is StaffAction.RECAPTURE:
        found = services(request)
        job = issue_case_capture(found.database, found.redis, case_id, now=at)
        message += "; capture queued" if job is not None else "; no capture was needed"
    return redirect(_next_url(number), message)
