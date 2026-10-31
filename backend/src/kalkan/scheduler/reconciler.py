"""The reconciler: re-queues work that got stuck (architecture.md §7). Every 10 minutes.

Rules so far:
- A ``detected`` case that entered the state at least 2 hours ago and has had no HTTP scan
  since then gets ``http_precheck``; a case the pre-check sent on to the browser stage is
  that stage's business (K74). Cases with an open review task, or with a ``job_failed``
  event since they entered the state, are left for the ops panel, until the operator
  re-queues the failed job there (K119).
- A brand match that admission never checked (``checked_at`` empty) 10 minutes after it was
  found gets ``admit_match`` again (K62).
- A ``detected`` case the pre-check sent to the browser stage at least 2 hours ago, with no
  ``ok`` browser scan since, gets a new capture job, unless one is still pending (its Redis
  marker, ``capture_contract.PENDING_KEY``); open reviews and ``job_failed`` skip it as above.
- A ``detected`` case whose newest ``ok`` browser scan of a profile, at least 30 minutes old,
  has no signals of its own (``compute_signals`` writes ``form_password`` for every scan it
  reads) gets ``compute_signals`` again; open reviews and ``job_failed`` skip it as above.
- A ``detected`` case whose newest browser scan signals (``form_password`` again; the
  enrichment's case signals do not count) came since it entered the state, at least 1 hour
  ago, and that has no triage verdict since gets ``ai_triage`` (sent by name to worker-ai).
- A ``detected`` case whose newest decision since it entered the state is ``confirm``, at
  least 1 hour old, with no confirmation verdict since gets ``ai_confirm``. Both skip open
  reviews and ``job_failed`` as above.
- A ``detected`` case with a triage or confirmation verdict since it entered the state, at
  least 30 minutes old, and no decision since that verdict gets ``decide``; open reviews and
  ``job_failed`` skip it as above.
- An ``approved`` case without ``blocked_reason``, approved at least 1 hour ago, that has no
  real notice of its current round that went out (sent, handed to the operator, failed,
  answered) gets ``plan_notices``: it plans what is missing and re-queues stuck notices. So
  does a ``notified`` case reopened at least 1 hour ago whose new round has no such notice.
- A Postmark event the API stored at least 10 minutes ago and nothing processed gets
  ``process_webhook_event`` again (the API also writes its outbox row, §16).

The demo workspace is never touched (K46). Later prompts add the rules of their stages.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from kalkan.db.models import (
    BrandMatch,
    Case,
    CaseEvent,
    Customer,
    Decision,
    Notice,
    ReviewTask,
    Scan,
    Signal,
    Verdict,
    WebhookEvent,
)
from kalkan.db.session import Database
from kalkan.domain.enums import (
    CaseState,
    DecisionOutcome,
    EventType,
    NoticeStatus,
    ScanStage,
    TaskKind,
    TaskStatus,
    VerdictSource,
    WebhookSource,
)
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import Outcome

PRECHECK_AFTER = timedelta(hours=2)
CAPTURE_AFTER = timedelta(hours=2)
ADMISSION_AFTER = timedelta(minutes=10)
SIGNALS_AFTER = timedelta(minutes=30)
TRIAGE_AFTER = timedelta(hours=1)
CONFIRM_AFTER = timedelta(hours=1)
DECIDE_AFTER = timedelta(minutes=30)
PLAN_AFTER = timedelta(hours=1)
WEBHOOK_AFTER = timedelta(minutes=10)
# A real notice in one of these states means the round's first wave went its way.
WAVE_DONE = (
    NoticeStatus.SENT,
    NoticeStatus.MANUAL_PENDING,
    NoticeStatus.FAILED,
    NoticeStatus.ACKNOWLEDGED,
    NoticeStatus.RESOLVED,
)
# A signal compute_signals writes for every browser scan it reads.
SCAN_SIGNAL = "form_password"
BATCH = 500
# ``case_events.data.action`` of the note the ops panel writes when it re-queues a failed job.
REQUEUE_ACTION = "requeue"

Send = Callable[..., None]
# Issues the capture of one case; returns something truthy when a job was issued
# (kalkan.scanning.capture_jobs.issue_case_capture, bound by the scheduler).
IssueCapture = Callable[[uuid.UUID], Any]

log = get_logger(__name__)


@dataclass(frozen=True)
class Reconciled:
    prechecks: int
    admissions: int
    captures: int = 0
    signals: int = 0
    triages: int = 0
    confirms: int = 0
    decisions: int = 0
    plans: int = 0
    webhooks: int = 0


def _entered() -> Any:
    """When the case last entered ``detected``."""
    return (
        select(func.max(CaseEvent.at))
        .where(CaseEvent.case_id == Case.id, CaseEvent.type == EventType.DETECTED)
        .correlate(Case)
        .scalar_subquery()
    )


def _left_alone(entered: Any) -> list[ColumnElement[bool]]:
    """Open reviews and failed jobs since ``entered`` are the ops panel's (§7)."""
    open_review = exists().where(
        ReviewTask.case_id == Case.id,
        ReviewTask.kind == TaskKind.REVIEW,
        ReviewTask.status == TaskStatus.OPEN,
    )
    # enrich_domain gives up without stopping the pipeline (K67), so its failures do not
    # take a case out of these rules; the failures of the stages that do stop it do. A failure
    # the operator re-queued from the ops panel since (a ``requeue`` note) no longer counts.
    failed = aliased(CaseEvent)
    requeued = aliased(CaseEvent)
    failed_since = exists().where(
        failed.case_id == Case.id,
        failed.type == EventType.JOB_FAILED,
        failed.at >= entered,
        failed.data["job"].astext.is_distinct_from("enrich_domain"),
        ~select(requeued.id)
        .where(
            requeued.case_id == Case.id,
            requeued.type == EventType.NOTE,
            requeued.data["action"].astext == REQUEUE_ACTION,
            requeued.at >= failed.at,
        )
        # Two levels down: correlate explicitly, or the case would be selected anew.
        .correlate(Case, failed)
        .exists(),
    )
    return [~open_review, ~failed_since]


def stuck_detected_cases(database: Database, now: datetime) -> list[str]:
    entered = _entered()
    scanned = exists().where(
        Scan.case_id == Case.id,
        Scan.stage == ScanStage.HTTP,
        Scan.started_at >= entered,
    )
    with database.worker_session() as session:
        rows = session.scalars(
            select(Case.id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Customer.is_demo.is_(False),
                Case.state == CaseState.DETECTED,
                entered <= now - PRECHECK_AFTER,
                ~scanned,
                *_left_alone(entered),
            )
            .order_by(Case.number)
            .limit(BATCH)
        ).all()
    return [str(case_id) for case_id in rows]


def unchecked_matches(database: Database, now: datetime) -> list[str]:
    with database.worker_session() as session:
        rows = session.scalars(
            select(BrandMatch.id)
            .join(Customer, Customer.id == BrandMatch.customer_id)
            .where(
                and_(
                    BrandMatch.checked_at.is_(None),
                    BrandMatch.first_matched_at <= now - ADMISSION_AFTER,
                    Customer.is_demo.is_(False),
                )
            )
            .order_by(BrandMatch.first_matched_at)
            .limit(BATCH)
        ).all()
    return [str(match_id) for match_id in rows]


def uncaptured_cases(database: Database, now: datetime) -> list[uuid.UUID]:
    """Cases the pre-check routed to the browser stage that have no ``ok`` browser scan."""
    entered = _entered()
    routed = aliased(CaseEvent)
    later = aliased(CaseEvent)
    # Nested two levels deep: correlated explicitly (auto-correlation is one level only).
    later_precheck = (
        exists()
        .where(later.case_id == Case.id, later.type == EventType.PRECHECKED, later.at > routed.at)
        .correlate(Case, routed)
    )
    captured = (
        exists()
        .where(
            Scan.case_id == Case.id,
            Scan.stage == ScanStage.BROWSER,
            Scan.outcome == Outcome.OK.value,
            Scan.started_at >= routed.at,
        )
        .correlate(Case, routed)
    )
    # The latest pre-check since the case entered detected routed it to the browser stage.
    waiting = exists().where(
        routed.case_id == Case.id,
        routed.type == EventType.PRECHECKED,
        routed.at >= entered,
        routed.at <= now - CAPTURE_AFTER,
        routed.data["route"].astext == "capture",
        ~later_precheck,
        ~captured,
    )
    with database.worker_session() as session:
        rows = session.scalars(
            select(Case.id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Customer.is_demo.is_(False),
                Case.state == CaseState.DETECTED,
                waiting,
                *_left_alone(entered),
            )
            .order_by(Case.number)
            .limit(BATCH)
        ).all()
    return list(rows)


def unsignalled_cases(database: Database, now: datetime) -> list[str]:
    """Cases whose newest ``ok`` browser scan of a profile has no signals yet."""
    entered = _entered()
    newer = aliased(Scan)
    newer_scan = (
        exists()
        .where(
            newer.case_id == Case.id,
            newer.stage == ScanStage.BROWSER,
            newer.outcome == Outcome.OK.value,
            newer.profile == Scan.profile,
            newer.started_at > Scan.started_at,
        )
        .correlate(Case, Scan)
    )
    computed = exists().where(Signal.scan_id == Scan.id, Signal.name == SCAN_SIGNAL).correlate(Scan)
    waiting = exists().where(
        Scan.case_id == Case.id,
        Scan.stage == ScanStage.BROWSER,
        Scan.outcome == Outcome.OK.value,
        Scan.started_at >= entered,
        Scan.started_at <= now - SIGNALS_AFTER,
        ~newer_scan,
        ~computed,
    )
    with database.worker_session() as session:
        rows = session.scalars(
            select(Case.id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Customer.is_demo.is_(False),
                Case.state == CaseState.DETECTED,
                waiting,
                *_left_alone(entered),
            )
            .order_by(Case.number)
            .limit(BATCH)
        ).all()
    return [str(case_id) for case_id in rows]


def _detected_cases(database: Database, *conditions: ColumnElement[bool]) -> list[str]:
    entered = _entered()
    with database.worker_session() as session:
        rows = session.scalars(
            select(Case.id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Customer.is_demo.is_(False),
                Case.state == CaseState.DETECTED,
                *conditions,
                *_left_alone(entered),
            )
            .order_by(Case.number)
            .limit(BATCH)
        ).all()
    return [str(case_id) for case_id in rows]


def untriaged_cases(database: Database, now: datetime) -> list[str]:
    """Cases with signals since they entered ``detected`` and no triage verdict after them."""
    entered = _entered()
    newest_signal = (
        select(func.max(Signal.created_at))
        .where(Signal.case_id == Case.id, Signal.name == SCAN_SIGNAL)
        .correlate(Case)
        .scalar_subquery()
    )
    triaged = exists().where(
        Verdict.case_id == Case.id,
        Verdict.source == VerdictSource.AI_TRIAGE,
        Verdict.created_at >= newest_signal,
    )
    return _detected_cases(
        database,
        newest_signal >= entered,
        newest_signal <= now - TRIAGE_AFTER,
        ~triaged,
    )


def unconfirmed_cases(database: Database, now: datetime) -> list[str]:
    """Cases whose newest decision is ``confirm`` and that have no confirmation verdict since."""
    entered = _entered()
    newest = aliased(Decision)
    newer = aliased(Decision)
    newer_decision = (
        exists()
        .where(newer.case_id == Case.id, newer.created_at > newest.created_at)
        .correlate(Case, newest)
    )
    confirmed = (
        exists()
        .where(
            Verdict.case_id == Case.id,
            Verdict.source == VerdictSource.AI_CONFIRM,
            Verdict.created_at >= newest.created_at,
        )
        .correlate(Case, newest)
    )
    waiting = exists().where(
        newest.case_id == Case.id,
        newest.outcome == DecisionOutcome.CONFIRM,
        newest.created_at >= entered,
        newest.created_at <= now - CONFIRM_AFTER,
        ~newer_decision,
        ~confirmed,
    )
    return _detected_cases(database, waiting)


def undecided_cases(database: Database, now: datetime) -> list[str]:
    """Cases with an AI verdict that no decision followed."""
    entered = _entered()
    verdict = aliased(Verdict)
    decided = (
        exists()
        .where(Decision.case_id == Case.id, Decision.created_at >= verdict.created_at)
        .correlate(Case, verdict)
    )
    waiting = exists().where(
        verdict.case_id == Case.id,
        verdict.source.in_([VerdictSource.AI_TRIAGE, VerdictSource.AI_CONFIRM]),
        verdict.created_at >= entered,
        verdict.created_at <= now - DECIDE_AFTER,
        ~decided,
    )
    return _detected_cases(database, waiting)


def unplanned_cases(database: Database, now: datetime) -> list[str]:
    """Approved cases whose first wave has not gone out, and reopened ones whose new round has
    not."""
    done = exists().where(
        Notice.case_id == Case.id,
        Notice.round == Case.reopened_count + 1,
        Notice.dry_run.is_(False),
        Notice.status.in_(WAVE_DONE),
    )
    reopened = exists().where(
        CaseEvent.case_id == Case.id,
        CaseEvent.type == EventType.REOPENED,
        CaseEvent.at <= now - PLAN_AFTER,
    )
    with database.worker_session() as session:
        rows = session.scalars(
            select(Case.id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Customer.is_demo.is_(False),
                or_(
                    and_(
                        Case.state == CaseState.APPROVED,
                        Case.blocked_reason.is_(None),
                        Case.approved_at <= now - PLAN_AFTER,
                    ),
                    and_(Case.state == CaseState.NOTIFIED, Case.reopened_count > 0, reopened),
                ),
                ~done,
            )
            .order_by(Case.number)
            .limit(BATCH)
        ).all()
    return [str(case_id) for case_id in rows]


def unprocessed_webhooks(database: Database, now: datetime) -> list[str]:
    with database.worker_session() as session:
        rows = session.scalars(
            select(WebhookEvent.id)
            .where(
                WebhookEvent.processed_at.is_(None),
                WebhookEvent.source.in_(
                    [WebhookSource.POSTMARK_INBOUND, WebhookSource.POSTMARK_BOUNCE]
                ),
                WebhookEvent.received_at <= now - WEBHOOK_AFTER,
            )
            .order_by(WebhookEvent.received_at)
            .limit(BATCH)
        ).all()
    return [str(event_id) for event_id in rows]


def reconcile(
    database: Database, send: Send, now: datetime, *, capture: IssueCapture | None = None
) -> Reconciled:
    """Re-queue stuck work; ``capture`` issues browser jobs (the rule is off without it)."""
    cases = stuck_detected_cases(database, now)
    for case_id in cases:
        send("http_precheck", case_id=case_id)
    matches = unchecked_matches(database, now)
    for match_id in matches:
        send("admit_match", match_id=match_id)
    captures = 0
    if capture is not None:
        # Cases with a pending job are skipped by issue_case_capture itself.
        captures = sum(1 for case_id in uncaptured_cases(database, now) if capture(case_id))
    signalled = unsignalled_cases(database, now)
    for case_id in signalled:
        send("compute_signals", case_id=case_id)
    triages = untriaged_cases(database, now)
    for case_id in triages:
        send("ai_triage", case_id=case_id)
    confirms = unconfirmed_cases(database, now)
    for case_id in confirms:
        send("ai_confirm", case_id=case_id)
    undecided = undecided_cases(database, now)
    for case_id in undecided:
        send("decide", case_id=case_id)
    unplanned = unplanned_cases(database, now)
    for case_id in unplanned:
        send("plan_notices", case_id=case_id)
    events = unprocessed_webhooks(database, now)
    for event_id in events:
        send("process_webhook_event", event_id=event_id)
    if (
        cases
        or matches
        or captures
        or signalled
        or triages
        or confirms
        or undecided
        or unplanned
        or events
    ):
        log.info(
            "reconciler.requeued",
            prechecks=len(cases),
            admissions=len(matches),
            captures=captures,
            signals=len(signalled),
            triages=len(triages),
            confirms=len(confirms),
            decisions=len(undecided),
            plans=len(unplanned),
            webhooks=len(events),
        )
    return Reconciled(
        len(cases),
        len(matches),
        captures,
        len(signalled),
        len(triages),
        len(confirms),
        len(undecided),
        len(unplanned),
        len(events),
    )
