"""Case life cycle (architecture.md §6).

The transition table is the only source of allowed state changes; anything else raises
:class:`TransitionError`. Every transition writes one ``case_events`` row, and entering
``in_review`` always opens a review task, whichever module triggered it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.base import utcnow
from kalkan.db.models import Case, CaseEvent, ReviewTask
from kalkan.domain.enums import CaseState, EventType, TaskKind, TaskStatus
from kalkan.followup.rules import (
    DISMISSED_EVERY,
    MONITOR_FOR,
    NOTIFIED_EVERY,
    TAKEN_DOWN_EVERY,
    WATCHING_EVERY,
)

S = CaseState

TRANSITIONS: Mapping[CaseState, frozenset[CaseState]] = {
    S.DETECTED: frozenset({S.WATCHING, S.IN_REVIEW, S.VERIFIED, S.DISMISSED}),
    S.WATCHING: frozenset({S.DETECTED, S.DISMISSED}),
    # Back to detected: the operator's Recapture runs the pipeline again (K131).
    S.IN_REVIEW: frozenset({S.DETECTED, S.VERIFIED, S.WATCHING, S.DISMISSED}),
    S.VERIFIED: frozenset({S.APPROVED, S.DISMISSED}),
    S.APPROVED: frozenset({S.NOTIFIED, S.DISMISSED}),
    S.NOTIFIED: frozenset({S.TAKEN_DOWN, S.DISMISSED}),
    S.TAKEN_DOWN: frozenset({S.NOTIFIED, S.ARCHIVED, S.DISMISSED}),
    S.DISMISSED: frozenset({S.DETECTED}),
    S.ARCHIVED: frozenset(),
}

# Event written for a transition into a state. Leaving taken_down for notified is a reopen.
_DEFAULT_EVENT: Mapping[CaseState, EventType] = {
    S.DETECTED: EventType.DETECTED,
    S.WATCHING: EventType.WATCHING,
    S.IN_REVIEW: EventType.REVIEW_REQUESTED,
    S.VERIFIED: EventType.VERIFIED,
    S.APPROVED: EventType.APPROVED,
    S.NOTIFIED: EventType.NOTICE_SENT,
    S.TAKEN_DOWN: EventType.TAKEN_DOWN,
    S.ARCHIVED: EventType.ARCHIVED,
    S.DISMISSED: EventType.DISMISSED,
}
# Alternative event types a caller may choose for a target state.
_ALLOWED_EVENTS: Mapping[CaseState, frozenset[EventType]] = {
    S.DISMISSED: frozenset({EventType.DISMISSED, EventType.WHITELISTED}),
}


class TransitionError(ValueError):
    """A state change that is not in the transition table."""


def can_transition(current: CaseState, target: CaseState) -> bool:
    return target in TRANSITIONS[current]


def event_type_for(
    current: CaseState, target: CaseState, requested: EventType | None = None
) -> EventType:
    if current is S.TAKEN_DOWN and target is S.NOTIFIED:
        default = EventType.REOPENED
    else:
        default = _DEFAULT_EVENT[target]
    if requested is None or requested is default:
        return default
    if requested in _ALLOWED_EVENTS.get(target, frozenset()):
        return requested
    raise TransitionError(f"event {requested} cannot record a transition to {target}")


def transition(
    session: Session,
    case: Case,
    target: CaseState,
    *,
    actor: str,
    reason: str | None = None,
    data: Mapping[str, Any] | None = None,
    event: EventType | None = None,
    at: datetime | None = None,
    last_action: str | None = None,
) -> CaseEvent:
    """Move ``case`` to ``target`` and record it.

    ``reason`` goes into the event data and, for ``in_review``, into the review task.
    ``last_action`` is the short customer-facing text for the case list (K39).
    """
    current = case.state
    if not can_transition(current, target):
        raise TransitionError(f"{case.number}: {current} -> {target} is not allowed")
    event_type = event_type_for(current, target, event)
    now = at or utcnow()

    case.state = target
    # A send block only describes an approved case waiting for its notices (§6).
    case.blocked_reason = None
    if target is S.VERIFIED and case.verified_at is None:
        case.verified_at = now
    elif target is S.APPROVED:
        case.approved_at = now
    elif target is S.TAKEN_DOWN:
        case.taken_down_at = now
    elif target is S.NOTIFIED and current is S.TAKEN_DOWN:
        case.reopened_count += 1
        case.taken_down_at = None
        case.offline_since = None
        case.monitor_until = None
    _set_follow_up(case, target, event_type, now)
    if last_action is not None:
        case.last_action = last_action
        case.last_action_at = now

    event_data: dict[str, Any] = {"from": current.value, **(data or {})}
    if reason is not None:
        event_data["reason"] = reason
    case_event = CaseEvent(
        customer_id=case.customer_id,
        case_id=case.id,
        at=now,
        type=event_type,
        actor=actor,
        data=event_data,
    )
    session.add(case_event)

    if target is S.IN_REVIEW:
        _open_review_task(session, case, reason, now)
    elif current is S.IN_REVIEW:
        _close_review_task(session, case, actor, target, now)
    return case_event


def _set_follow_up(case: Case, target: CaseState, event: EventType, at: datetime) -> None:
    """The follow-up clock of the new state (§16, K143); callers may set an earlier one.

    A whitelisted case is never checked again; the other states without follow-up stop it.
    """
    if target is S.NOTIFIED:
        case.next_check_at = at + NOTIFIED_EVERY
    elif target is S.TAKEN_DOWN:
        case.monitor_until = at + MONITOR_FOR
        case.next_check_at = at + TAKEN_DOWN_EVERY
    elif target is S.WATCHING:
        case.next_check_at = at + WATCHING_EVERY
    elif target is S.DISMISSED and event is not EventType.WHITELISTED:
        case.next_check_at = at + DISMISSED_EVERY
    else:
        case.next_check_at = None


def _close_review_task(
    session: Session, case: Case, actor: str, target: CaseState, at: datetime
) -> None:
    """The review is over once the case leaves ``in_review``, whoever moved it."""
    task = session.scalar(
        select(ReviewTask).where(
            ReviewTask.case_id == case.id,
            ReviewTask.kind == TaskKind.REVIEW,
            ReviewTask.status == TaskStatus.OPEN,
        )
    )
    if task is not None:
        task.status = TaskStatus.DONE
        task.resolved_by = actor
        task.resolved_at = at
        task.note = f"case moved to {target.value}"


def _open_review_task(session: Session, case: Case, reason: str | None, at: datetime) -> None:
    existing = session.scalar(
        select(ReviewTask).where(
            ReviewTask.case_id == case.id,
            ReviewTask.kind == TaskKind.REVIEW,
            ReviewTask.status == TaskStatus.OPEN,
        )
    )
    if existing is not None:
        existing.reason = reason
        return
    session.add(
        ReviewTask(
            customer_id=case.customer_id,
            case_id=case.id,
            kind=TaskKind.REVIEW,
            reason=reason,
            status=TaskStatus.OPEN,
            created_at=at,
            updated_at=at,
        )
    )


class PanelStatus(StrEnum):
    """Status labels the customer sees (product.md §12)."""

    REVIEWING = "Reviewing"
    ACTIVE = "Active"
    NOTICE_SENT = "Notice sent"
    TAKEN_DOWN = "Taken down"
    WATCHING = "Watching"
    DISMISSED = "Dismissed"


_PANEL_STATUS: Mapping[CaseState, PanelStatus] = {
    S.DETECTED: PanelStatus.REVIEWING,
    S.IN_REVIEW: PanelStatus.REVIEWING,
    S.VERIFIED: PanelStatus.ACTIVE,
    S.APPROVED: PanelStatus.ACTIVE,
    S.NOTIFIED: PanelStatus.NOTICE_SENT,
    S.TAKEN_DOWN: PanelStatus.TAKEN_DOWN,
    S.ARCHIVED: PanelStatus.TAKEN_DOWN,
    S.WATCHING: PanelStatus.WATCHING,
    S.DISMISSED: PanelStatus.DISMISSED,
}


def panel_status(state: CaseState) -> PanelStatus:
    """The one mapping from system state to the status shown in the panel."""
    return _PANEL_STATUS[state]


def states_for(status: PanelStatus) -> frozenset[CaseState]:
    """System states behind a panel status (for filters and metrics)."""
    return frozenset(state for state, shown in _PANEL_STATUS.items() if shown is status)
