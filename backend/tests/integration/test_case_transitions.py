from __future__ import annotations

import pytest
from sqlalchemy import select

from kalkan.db.models import Case, CaseEvent, ReviewTask
from kalkan.db.session import Database
from kalkan.domain.case_state import TransitionError, transition
from kalkan.domain.enums import BlockedReason, CaseState, EventType, TaskKind, TaskStatus
from tests import factories


def _new_case(database: Database, state: CaseState = CaseState.DETECTED) -> Case:
    with database.worker_session() as session:
        owner = factories.customer(session)
        return factories.case(session, owner, factories.brand(session, owner), state)


def test_transition_writes_one_event(database: Database) -> None:
    case = _new_case(database)
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        transition(session, stored, CaseState.VERIFIED, actor="rule", reason="R1", data={"x": 1})
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        events = session.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id)).all()
    assert stored.state is CaseState.VERIFIED
    assert stored.verified_at is not None
    assert [(e.type, e.actor, e.data) for e in events] == [
        (EventType.VERIFIED, "rule", {"from": "detected", "x": 1, "reason": "R1"})
    ]


def test_entering_review_opens_one_task(database: Database) -> None:
    case = _new_case(database)
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        transition(session, stored, CaseState.IN_REVIEW, actor="system", reason="challenge")
    with database.worker_session() as session:
        tasks = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).all()
    assert [(t.kind, t.status, t.reason) for t in tasks] == [
        (TaskKind.REVIEW, TaskStatus.OPEN, "challenge")
    ]


def test_review_task_is_not_duplicated_when_reentering(database: Database) -> None:
    case = _new_case(database)
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        transition(session, stored, CaseState.IN_REVIEW, actor="system", reason="first")
        transition(session, stored, CaseState.WATCHING, actor="staff@example.com")
        transition(session, stored, CaseState.DETECTED, actor="system")
        transition(session, stored, CaseState.IN_REVIEW, actor="system", reason="second")
    with database.worker_session() as session:
        tasks = session.scalars(
            select(ReviewTask).where(ReviewTask.case_id == case.id).order_by(ReviewTask.created_at)
        ).all()
    # Leaving in_review closes the review; coming back opens a new one: never two open.
    assert [(task.reason, task.status) for task in tasks] == [
        ("first", TaskStatus.DONE),
        ("second", TaskStatus.OPEN),
    ]
    assert tasks[0].resolved_by == "staff@example.com"


def test_forbidden_transition_changes_nothing(database: Database) -> None:
    case = _new_case(database)
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        with pytest.raises(TransitionError):
            transition(session, stored, CaseState.NOTIFIED, actor="system")
    with database.worker_session() as session:
        assert session.get_one(Case, case.id).state is CaseState.DETECTED
        assert session.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id)).all() == []


def test_reopen_counts_and_clears_the_takedown(database: Database) -> None:
    case = _new_case(database, CaseState.NOTIFIED)
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        transition(session, stored, CaseState.TAKEN_DOWN, actor="system")
        assert stored.taken_down_at is not None
        event = transition(session, stored, CaseState.NOTIFIED, actor="system")
        assert event.type is EventType.REOPENED
        assert stored.reopened_count == 1
        assert stored.taken_down_at is None


def test_transition_clears_the_send_block_and_sets_last_action(database: Database) -> None:
    case = _new_case(database, CaseState.APPROVED)
    with database.worker_session() as session:
        stored = session.get_one(Case, case.id)
        stored.blocked_reason = BlockedReason.DRY_RUN
        transition(session, stored, CaseState.NOTIFIED, actor="system", last_action="Host notified")
        assert stored.blocked_reason is None
        assert stored.last_action == "Host notified"
        assert stored.last_action_at is not None


def test_case_numbers_come_from_the_sequence(database: Database) -> None:
    from kalkan.db.case_numbers import format_case_number, next_case_number

    with database.worker_session() as session:
        first = next_case_number(session, "KLK")
        second = next_case_number(session, "KLK")
    assert first.startswith("KLK-")
    assert len(first) == len("KLK-000123")
    assert int(second[4:]) == int(first[4:]) + 1
    assert format_case_number("KLK", 123) == "KLK-000123"
