"""Outbox, notice preview and manual tasks (architecture.md §15, §19).

- ``/outbox``: notices by status (draft, queued, sent, blocked and why, manual, failed);
  previews are tagged dry-run.
- ``/outbox/{id}``: the notice as it was (or will be) sent.
- ``/tasks``: open operator tasks (web forms, retractions, escalations, replies that need
  information or a reading, bounces; ``?kind=`` filters) with the text ready to copy.
  "Submitted" marks a real web-form notice sent, through the same success path as e-mail
  (``notices.sender.mark_sent``); the other kinds are closed with "Done". "[DRY RUN]" form tasks
  are shown but cannot be submitted.
Every write is audited.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from kalkan.db.models import AuditLog, Case, Domain, Notice, ReviewTask
from kalkan.domain.enums import NoticeStatus, TaskKind, TaskStatus
from kalkan.notices.planning import DRY_RUN_TASK
from kalkan.notices.sender import mark_sent
from kalkan.ops import auth
from kalkan.ops.web import client_ip, csrf_guard, current_staff, now, redirect, render, transaction

router = APIRouter()
LIMIT = 200
TASK_KINDS = (
    TaskKind.MANUAL_CHANNEL,
    TaskKind.RETRACTION,
    TaskKind.ESCALATION,
    TaskKind.NEEDS_INFO,
    TaskKind.REPLY,
    TaskKind.BOUNCE,
)
# Tasks that stand for a notice the operator sends by hand.
SUBMIT_KINDS = (TaskKind.MANUAL_CHANNEL, TaskKind.RETRACTION)
Staff = Annotated[auth.Staff, Depends(current_staff)]


@router.get("/outbox")
def outbox(request: Request, _: Staff, status: str | None = None) -> Response:
    try:
        chosen = NoticeStatus(status) if status else None
    except ValueError:
        chosen = None
    query = (
        select(Notice, Case.number, Domain.name)
        .join(Case, Case.id == Notice.case_id)
        .join(Domain, Domain.id == Case.domain_id)
        .order_by(Notice.updated_at.desc())
        .limit(LIMIT)
    )
    if chosen is not None:
        query = query.where(Notice.status == chosen)
    with transaction(request) as session:
        rows = session.execute(query).all()
    return render(
        request,
        "outbox.html",
        {"rows": rows, "statuses": list(NoticeStatus), "chosen": chosen},
    )


@router.get("/outbox/{notice_id}")
def preview(request: Request, notice_id: uuid.UUID, _: Staff) -> Response:
    with transaction(request) as session:
        notice = session.get(Notice, notice_id)
        if notice is None:
            raise HTTPException(status_code=404)
        case = session.get_one(Case, notice.case_id)
    return render(request, "notice.html", {"notice": notice, "case": case})


@router.get("/tasks")
def tasks(request: Request, _: Staff, kind: str | None = None) -> Response:
    chosen = next((item for item in TASK_KINDS if item.value == kind), None)
    kinds = (chosen,) if chosen is not None else TASK_KINDS
    with transaction(request) as session:
        rows = session.execute(
            select(ReviewTask, Notice, Case.number)
            .outerjoin(Notice, Notice.id == ReviewTask.notice_id)
            .outerjoin(Case, Case.id == ReviewTask.case_id)
            .where(ReviewTask.kind.in_(kinds), ReviewTask.status == TaskStatus.OPEN)
            .order_by(ReviewTask.created_at)
            .limit(LIMIT)
        ).all()
    items = [
        {
            "task": task,
            "notice": notice,
            "number": number,
            "dry_run": (task.note or "").startswith(DRY_RUN_TASK),
            "submit": task.kind in SUBMIT_KINDS,
        }
        for task, notice, number in rows
    ]
    return render(request, "tasks.html", {"items": items, "kinds": TASK_KINDS, "chosen": chosen})


@router.post("/tasks/{task_id}/submitted")
def submitted(
    request: Request, task_id: uuid.UUID, staff: Annotated[auth.Staff, Depends(csrf_guard)]
) -> Response:
    at = now()
    with transaction(request) as session:
        task = session.get(ReviewTask, task_id, with_for_update=True)
        if task is None or task.status is not TaskStatus.OPEN or task.kind not in TASK_KINDS:
            return redirect("/tasks", "Error: the task is not open.")
        submit = task.kind in SUBMIT_KINDS
        if submit and (task.note or "").startswith(DRY_RUN_TASK):
            return redirect("/tasks", "Error: a dry-run task is never submitted.")
        notice = session.get(Notice, task.notice_id) if task.notice_id else None
        case = session.get(Case, task.case_id) if task.case_id else None
        if submit and notice is not None and case is not None:
            if notice.dry_run or notice.status not in {
                NoticeStatus.MANUAL_PENDING,
                NoticeStatus.BLOCKED,
            }:
                return redirect("/tasks", "Error: the notice is not waiting for the operator.")
            session.get(Case, case.id, with_for_update=True)
            mark_sent(session, case, notice, now=at, actor=staff.email)
        task.status = TaskStatus.DONE
        task.resolved_by = staff.email
        task.resolved_at = at
        task.updated_at = at
        session.add(
            AuditLog(
                actor=staff.email,
                action="task.submitted",
                customer_id=task.customer_id,
                target=case.number if case else str(task.id),
                data={"kind": task.kind.value, "notice_id": str(task.notice_id or "")},
                ip=client_ip(request),
                at=at,
            )
        )
    return redirect("/tasks", "Marked as submitted." if submit else "Marked as done.")
