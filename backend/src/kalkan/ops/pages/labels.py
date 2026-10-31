"""Labelling for the evaluation set (architecture.md §19; K121).

Keyboard only: 1-6 pick the category and save, N skips, / goes to the note, Esc leaves it
(``static/ops.js``). Every control is a plain form, so the page also works without it.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from kalkan.db.models import Case
from kalkan.ops import auth, labeling
from kalkan.ops.web import client_ip, csrf_guard, current_staff, now, redirect, render, transaction

router = APIRouter(prefix="/labels")


def _next_url(after: str | None) -> str:
    return f"/labels?after={quote(after)}" if after else "/labels"


@router.get("")
def queue(
    request: Request,
    _: Annotated[auth.Staff, Depends(current_staff)],
    after: str | None = None,
) -> Response:
    with transaction(request) as session:
        case = labeling.next_case(session, after)
        if case is None and after:
            # The end of this pass: start again from the skipped cases.
            case = labeling.next_case(session)
            after = None
        context = {
            "view": labeling.view(session, case) if case else None,
            "after": after,
            "progress": labeling.progress(session),
            "remaining": labeling.remaining(session),
            "keys": labeling.KEYS,
        }
    return render(request, "labels.html", context)


@router.post("/{case_id}")
def save(
    request: Request,
    case_id: uuid.UUID,
    staff: Annotated[auth.Staff, Depends(csrf_guard)],
    category: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    after: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "",
) -> Response:
    with transaction(request) as session:
        try:
            label = labeling.save_label(
                session,
                case_id,
                category,
                note,
                actor=staff.email,
                ip=client_ip(request),
                now=now(),
            )
        except labeling.LabelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        case = session.get(Case, case_id)
        number = case.number if case else ""
    message = f"{number} labelled {label.category.value}"
    if back == "case":
        return redirect(f"/cases/{case_id}", message)
    return redirect(_next_url(after or None), message)
