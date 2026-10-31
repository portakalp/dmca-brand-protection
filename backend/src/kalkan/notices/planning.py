"""Writing a case's first wave of notices (architecture.md §15; K137).

:func:`plan_notices` runs after a case is approved (the ``decide`` actor, the review page), when
a sending gate is lifted (``clear_blocked_cases``) and from the reconciler. It only touches the
database: in one transaction it

- writes the first wave of the channel plan (``notices.planner``) as ``draft`` rows of the
  case's current round (``reopened_count + 1``) with the effective dry-run; the unique key
  includes ``dry_run``, so previews and real notices never collide and a second plan writes
  nothing new;
- hands every row that still needs work to ``ai_notice_text`` through the outbox: new drafts,
  blocked rows (a gate may have been lifted) and drafts or queued rows stuck for 30 minutes; a
  preview that already went out is left alone;
- closes the inert "[DRY RUN]" tasks of the case once a real wave is planned.

A monitor-only customer gets no notice at all, not even a preview (gate 1); the case keeps
``blocked_reason=monitor_only``. A basis that sends nothing (``none``, ``copyright``) plans
nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import Case, Customer, Notice, ReviewTask
from kalkan.db.outbox import AI_NOTICE_TEXT, queue_job
from kalkan.db.session import Database
from kalkan.domain.enums import (
    BlockedReason,
    CaseState,
    NoticeStatus,
    NoticeTemplate,
    TaskKind,
    TaskStatus,
)
from kalkan.logging import get_logger
from kalkan.notices.facts import channel_facts, notice_basis
from kalkan.notices.gates import effective_dry_run
from kalkan.notices.planner import Target, first_wave

STUCK_AFTER = timedelta(minutes=30)
PLANNABLE = frozenset({CaseState.APPROVED, CaseState.NOTIFIED})
DRY_RUN_TASK = "[DRY RUN]"

log = get_logger(__name__)


@dataclass(frozen=True)
class Planned:
    created: int
    queued: int
    dry_run: bool


def _needs_work(notice: Notice, created: set[uuid.UUID], now: datetime) -> bool:
    if notice.id in created:
        return True
    if notice.status is NoticeStatus.BLOCKED:
        # A delivered preview stays as it is; a real row or an unsent preview is tried again.
        return not (notice.dry_run and notice.provider_message_id)
    if notice.status in {NoticeStatus.DRAFT, NoticeStatus.QUEUED}:
        return notice.updated_at <= now - STUCK_AFTER
    return False


def plan_case(session: Session, settings: Settings, case: Case, *, now: datetime) -> Planned | None:
    """Plan inside the caller's transaction; ``case`` is locked by the caller.

    An ``approved`` case gets its first wave; a ``notified`` one gets the first wave of its
    current round, which after a reopen is a new round (§16, K144).
    """
    if case.state not in PLANNABLE:
        return None
    customer = session.get_one(Customer, case.customer_id)
    if customer.is_demo:
        return None
    if customer.monitor_only:
        if case.state is CaseState.APPROVED:
            case.blocked_reason = BlockedReason.MONITOR_ONLY
        return Planned(0, 0, dry_run=True)
    basis = notice_basis(session, case.id)
    targets = first_wave(channel_facts(session, case, basis))
    if not targets:
        log.info("notices.nothing_to_plan", case_id=str(case.id), basis=basis.value)
        return Planned(0, 0, dry_run=effective_dry_run(settings, customer))
    planned = write_targets(session, settings, case, customer, targets, level=0, now=now)
    log.info(
        "notices.planned",
        case_id=str(case.id),
        basis=basis.value,
        round=case.reopened_count + 1,
        created=planned.created,
        queued=planned.queued,
        dry_run=planned.dry_run,
    )
    return planned


def write_targets(
    session: Session,
    settings: Settings,
    case: Case,
    customer: Customer,
    targets: Sequence[Target],
    *,
    level: int,
    now: datetime,
) -> Planned:
    """Draft rows for ``targets`` at ``level`` of the case's current round, and their jobs.

    The escalation engine writes its levels through here too (``followup.escalation``).
    """
    dry = effective_dry_run(settings, customer)
    round_ = case.reopened_count + 1
    created: set[uuid.UUID] = set()
    for target in targets:
        notice_id = session.scalar(
            insert(Notice)
            .values(
                id=uuid.uuid4(),
                customer_id=case.customer_id,
                case_id=case.id,
                channel=target.channel,
                method=target.method,
                recipient=target.recipient,
                template=target.template,
                language=target.language,
                escalation_level=target.level,
                round=round_,
                dry_run=dry,
                status=NoticeStatus.DRAFT,
            )
            .on_conflict_do_nothing()
            .returning(Notice.id)
        )
        if notice_id is not None:
            created.add(notice_id)
    rows = session.scalars(
        select(Notice)
        .where(
            Notice.case_id == case.id,
            Notice.round == round_,
            Notice.escalation_level == level,
            Notice.template != NoticeTemplate.RETRACTION,
            Notice.dry_run.is_(dry),
        )
        .with_for_update()
    ).all()
    queued = 0
    for notice in rows:
        if not _needs_work(notice, created, now):
            continue
        notice.status = NoticeStatus.DRAFT
        notice.blocked_reason = None
        notice.updated_at = now
        queue_job(session, AI_NOTICE_TEXT, notice_id=notice.id, customer_id=case.customer_id)
        queued += 1
    if not dry:
        _close_dry_run_tasks(session, case.id, now)
    return Planned(len(created), queued, dry)


def _close_dry_run_tasks(session: Session, case_id: uuid.UUID, now: datetime) -> None:
    session.execute(
        update(ReviewTask)
        .where(
            ReviewTask.case_id == case_id,
            ReviewTask.kind.in_([TaskKind.MANUAL_CHANNEL, TaskKind.RETRACTION]),
            ReviewTask.status == TaskStatus.OPEN,
            ReviewTask.note.startswith(DRY_RUN_TASK),
        )
        .values(
            status=TaskStatus.REJECTED,
            resolved_by="system",
            resolved_at=now,
            updated_at=now,
        )
    )


def plan_notices(
    database: Database, settings: Settings, case_id: uuid.UUID, *, now: datetime
) -> Planned | None:
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None:
            return None
        return plan_case(session, settings, case, now=now)
