"""Failed jobs and re-queueing them by hand (architecture.md §7, §19; K119).

The list holds the ``job_failed`` events of the last 24 hours. Re-queueing writes a ``note``
event (``action=requeue``) next to the job, so the reconciler takes the case back into its
rules, and an ``audit_log`` row:

- ``http_precheck``, ``compute_signals`` and ``enrich_domain`` go through the outbox;
- a failed capture of a case still in ``detected`` gets a new capture job (the same issuer as
  the reconciler's). A capture that failed for good moved the case to ``in_review``
  (``capture_failed``, K92); new evidence for it is taken from the review queue.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, select
from sqlalchemy.orm import Session, aliased

from kalkan.db.models import AuditLog, Case, CaseEvent, Customer, Domain
from kalkan.db.outbox import COMPUTE_SIGNALS, ENRICH_DOMAIN, HTTP_PRECHECK, queue_job
from kalkan.domain.enums import CaseState, EventType
from kalkan.scheduler.reconciler import REQUEUE_ACTION

WINDOW = timedelta(hours=24)
OUTBOX_JOBS = {
    "http_precheck": HTTP_PRECHECK,
    "compute_signals": COMPUTE_SIGNALS,
    "enrich_domain": ENRICH_DOMAIN,
}
CAPTURE_JOBS = frozenset({"browser_capture", "persist_capture"})

# Issues a capture of a detected case; truthy when a job was issued.
IssueCapture = Callable[[uuid.UUID], Any]


class RequeueError(ValueError):
    pass


@dataclass(frozen=True)
class FailedJob:
    event_id: uuid.UUID
    at: datetime
    job: str
    error: str
    case_id: uuid.UUID
    number: str
    domain: str
    customer: str
    state: CaseState
    requeued_at: datetime | None

    @property
    def requeueable(self) -> str | None:
        """Why it cannot be re-queued here, or ``None`` when it can."""
        return why_not(self.job, self.state)


def why_not(job: str, state: CaseState) -> str | None:
    if job in CAPTURE_JOBS:
        if state is CaseState.DETECTED:
            return None
        return "the case is in review: take new evidence from the review queue"
    if job not in OUTBOX_JOBS:
        return f"{job} cannot be re-queued from here"
    if job != "enrich_domain" and state is not CaseState.DETECTED:
        return f"the case left detected ({state.value})"
    return None


def failed_jobs(session: Session, now: datetime) -> list[FailedJob]:
    requeue = aliased(CaseEvent)
    requeued_at = (
        select(requeue.at)
        .where(
            requeue.case_id == CaseEvent.case_id,
            requeue.type == EventType.NOTE,
            requeue.data["action"].astext == REQUEUE_ACTION,
            requeue.data["event_id"].astext == cast(CaseEvent.id, Text),
        )
        .order_by(requeue.at.desc())
        .limit(1)
        .correlate(CaseEvent)
        .scalar_subquery()
    )
    rows = session.execute(
        select(CaseEvent, Case.number, Case.state, Domain.name, Customer.name, requeued_at)
        .join(Case, Case.id == CaseEvent.case_id)
        .join(Domain, Domain.id == Case.domain_id)
        .join(Customer, Customer.id == Case.customer_id)
        .where(CaseEvent.type == EventType.JOB_FAILED, CaseEvent.at >= now - WINDOW)
        .order_by(CaseEvent.at.desc())
    ).all()
    found = []
    for event, number, state, domain, customer, when in rows:
        data = event.data or {}
        error = data.get("error") or "; ".join(str(e) for e in data.get("errors", [])) or ""
        found.append(
            FailedJob(
                event_id=event.id,
                at=event.at,
                job=str(data.get("job", "?")),
                error=str(error),
                case_id=event.case_id,
                number=number,
                domain=domain,
                customer=customer,
                state=state,
                requeued_at=when,
            )
        )
    return found


def requeue(
    session: Session,
    event_id: uuid.UUID,
    *,
    actor: str,
    ip: str | None,
    now: datetime,
    issue_capture: IssueCapture,
) -> str:
    """Re-queue the job of one ``job_failed`` event; returns what was done."""
    event = session.get(CaseEvent, event_id)
    if event is None or event.type is not EventType.JOB_FAILED:
        raise RequeueError("no such failed job")
    case = session.get(Case, event.case_id, with_for_update=True)
    if case is None:
        raise RequeueError("no such case")
    job = str((event.data or {}).get("job", ""))
    reason = why_not(job, case.state)
    if reason is not None:
        raise RequeueError(reason)
    if job in CAPTURE_JOBS:
        done = "new capture job issued" if issue_capture(case.id) else "a capture is pending"
    elif job == "enrich_domain":
        queue_job(session, ENRICH_DOMAIN, domain_id=case.domain_id)
        done = "enrich_domain queued"
    else:
        queue_job(session, OUTBOX_JOBS[job], case_id=case.id)
        done = f"{job} queued"
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=now,
            type=EventType.NOTE,
            actor=actor,
            data={"action": REQUEUE_ACTION, "job": job, "event_id": str(event.id)},
        )
    )
    session.add(
        AuditLog(
            actor=actor,
            action="job.requeue",
            customer_id=case.customer_id,
            target=case.number,
            data={"job": job, "event_id": str(event.id), "result": done},
            ip=ip,
            at=now,
        )
    )
    session.flush()
    return done
