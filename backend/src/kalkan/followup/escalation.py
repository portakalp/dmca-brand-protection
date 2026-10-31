"""Escalation of a site that stays up after its first wave (architecture.md §15, §16, K56).

The hourly check looks at ``notified`` cases. The clock starts at the last notice of the
current round's first wave that went out (``sent_at`` of a real level 0 notice); each later
level of the channel plan (``notices.planner.plan_levels``) is due its hours after that:

- phishing: level 1 at 48 hours is the registry notice, level 2 at 96 hours an operator task;
- trademark: level 1 at 48 hours is an operator task (URS or UDRP with the customer).

A level is skipped while the site is seen down (``offline_since``) and once a "removed" reply
came in this round. Each level happens once per round: its ``registrar_escalated`` event (the
name is historical; the data carries the level, the channel and the round) is the record.
Notices are written as drafts through ``notices.planning.write_targets``; ``send_notice`` and
its gates do the rest, so this module never sends anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import Case, CaseEvent, Customer, Message, Notice, ReviewTask
from kalkan.db.session import Database
from kalkan.domain.enums import (
    CaseState,
    EventType,
    NoticeTemplate,
    ReplyClass,
    TaskKind,
    TaskStatus,
)
from kalkan.logging import get_logger
from kalkan.notices.facts import channel_facts, notice_basis
from kalkan.notices.planner import ESCALATIONS, Level, Step, plan_levels
from kalkan.notices.planning import write_targets

SYSTEM = "system"
OPERATOR = "operator"
BATCH = 500

log = get_logger(__name__)


@dataclass(frozen=True)
class Escalated:
    case_id: uuid.UUID
    level: int
    channel: str
    round: int


def wave_sent_at(session: Session, case: Case) -> tuple[datetime | None, datetime | None]:
    """First and last ``sent_at`` of the current round's first wave."""
    first, last = session.execute(
        select(func.min(Notice.sent_at), func.max(Notice.sent_at)).where(
            Notice.case_id == case.id,
            Notice.round == case.reopened_count + 1,
            Notice.escalation_level == 0,
            Notice.dry_run.is_(False),
            Notice.template != NoticeTemplate.RETRACTION,
            Notice.sent_at.is_not(None),
        )
    ).one()
    return first, last


def _done_levels(session: Session, case: Case) -> set[int]:
    rows = session.scalars(
        select(CaseEvent.data).where(
            CaseEvent.case_id == case.id, CaseEvent.type == EventType.REGISTRAR_ESCALATED
        )
    ).all()
    round_ = case.reopened_count + 1
    return {int(data["level"]) for data in rows if int(data.get("round", 1)) == round_}


def _removed(session: Session, case: Case, since: datetime) -> bool:
    return (
        session.scalar(
            select(Message.id)
            .where(
                Message.case_id == case.id,
                Message.classification == ReplyClass.REMOVED,
                Message.received_at >= since,
            )
            .limit(1)
        )
        is not None
    )


def next_escalation(session: Session, case: Case) -> tuple[Step, datetime] | None:
    """The next level of a ``notified`` case and when it is due; read-only, for the panel.

    The plan's shape follows the first wave's template, so no contact data is needed.
    """
    if case.state is not CaseState.NOTIFIED or case.offline_since is not None:
        return None
    first, last = wave_sent_at(session, case)
    if first is None or last is None or _removed(session, case, first):
        return None
    template = session.scalar(
        select(Notice.template)
        .where(
            Notice.case_id == case.id,
            Notice.round == case.reopened_count + 1,
            Notice.escalation_level == 0,
            Notice.dry_run.is_(False),
            Notice.sent_at.is_not(None),
        )
        .order_by(Notice.sent_at)
        .limit(1)
    )
    done = _done_levels(session, case)
    for step in ESCALATIONS.get(template, ()) if template is not None else ():
        if step.level not in done:
            return step, last + timedelta(hours=step.after_hours)
    return None


def _task(session: Session, case: Case, level: Level, now: datetime) -> None:
    hours = level.after_hours
    reason = (
        f"Level {level.level}: the site is still up {hours} hours after the first wave. "
        "Decide the next step with the customer (registrar again, URS or UDRP)."
    )
    session.add(
        ReviewTask(
            customer_id=case.customer_id,
            case_id=case.id,
            kind=TaskKind.ESCALATION,
            reason=reason,
            status=TaskStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
    )


def escalate_case(
    session: Session, settings: Settings, case: Case, *, now: datetime
) -> list[Escalated]:
    """Every level that is due, inside the caller's transaction; ``case`` is locked."""
    if case.state is not CaseState.NOTIFIED or case.offline_since is not None:
        return []
    customer = session.get_one(Customer, case.customer_id)
    if customer.is_demo or customer.monitor_only:
        return []
    first, last = wave_sent_at(session, case)
    if first is None or last is None or _removed(session, case, first):
        return []
    done = _done_levels(session, case)
    round_ = case.reopened_count + 1
    escalated: list[Escalated] = []
    for level in plan_levels(channel_facts(session, case, notice_basis(session, case.id)))[1:]:
        if level.level in done or now < last + timedelta(hours=level.after_hours):
            continue
        if level.operator_task:
            _task(session, case, level, now)
            channel = OPERATOR
        else:
            write_targets(
                session, settings, case, customer, level.targets, level=level.level, now=now
            )
            channel = ",".join(target.channel.value for target in level.targets)
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=now,
                type=EventType.REGISTRAR_ESCALATED,
                actor=SYSTEM,
                data={
                    "level": level.level,
                    "channel": channel,
                    "round": round_,
                    "hours": level.after_hours,
                },
            )
        )
        escalated.append(Escalated(case.id, level.level, channel, round_))
        log.info("escalation.done", case_id=str(case.id), level=level.level, channel=channel)
    return escalated


def check_escalations(
    database: Database, settings: Settings, *, now: datetime, limit: int = BATCH
) -> list[Escalated]:
    """The hourly check (§7): ``notified`` cases whose wave is at least 48 hours old."""
    oldest = now - timedelta(hours=48)
    with database.worker_session() as session:
        ids = session.scalars(
            select(Case.id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Case.state == CaseState.NOTIFIED,
                Case.offline_since.is_(None),
                Case.first_notice_at <= oldest,
                Customer.is_demo.is_(False),
            )
            .order_by(Case.first_notice_at)
            .limit(limit)
        ).all()
    escalated: list[Escalated] = []
    for case_id in ids:
        with database.worker_session() as session:
            case = session.get(Case, case_id, with_for_update=True)
            if case is not None:
                escalated += escalate_case(session, settings, case, now=now)
    return escalated
