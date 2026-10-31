"""Failed jobs of the last 24 hours and re-queueing them (architecture.md §7; K119)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from kalkan.ops import auth, requeue
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

router = APIRouter(prefix="/jobs")


@router.get("")
def failed(request: Request, _: Annotated[auth.Staff, Depends(current_staff)]) -> Response:
    with transaction(request) as session:
        jobs = requeue.failed_jobs(session, now())
    return render(request, "jobs.html", {"jobs": jobs})


@router.post("/{event_id}/requeue")
def requeue_job(
    request: Request, event_id: uuid.UUID, staff: Annotated[auth.Staff, Depends(csrf_guard)]
) -> Response:
    found = services(request)
    at = now()

    def issue(case_id: uuid.UUID) -> object:
        return issue_case_capture(found.database, found.redis, case_id, now=at)

    with transaction(request) as session:
        try:
            done = requeue.requeue(
                session,
                event_id,
                actor=staff.email,
                ip=client_ip(request),
                now=at,
                issue_capture=issue,
            )
        except requeue.RequeueError as exc:
            return redirect("/jobs", f"Error: {exc}")
    return redirect("/jobs", f"Re-queued: {done}.")
