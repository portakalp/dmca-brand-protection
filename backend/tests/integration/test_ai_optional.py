"""Without AI the pipeline still completes: a case that would go to the AI goes to the review
queue (``ai_disabled``), and the reconciler never queues AI work for it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import dramatiq
import pytest
from sqlalchemy import select

from kalkan.ai import queue
from kalkan.db.models import Case, CaseEvent, Decision, ReviewTask, Signal
from kalkan.db.session import Database
from kalkan.decision.apply import Stage
from kalkan.decision.without_ai import AI_DISABLED, review_without_ai
from kalkan.domain.enums import CaseState, DecisionOutcome, EventType, TaskKind, TaskStatus
from tests import factories

# The test broker (tests/conftest.py); the stand-ins are declared on it.
BROKER = dramatiq.get_broker()


class Recording:
    """Records messages sent by name and through actors of this process (the scheduler's)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def enqueue(self, message: dramatiq.Message[Any]) -> None:
        self.sent.append((message.queue_name, message.actor_name, message.kwargs))

    def get_actor(self, name: str) -> Any:
        # Other rows in the shared database may get other jobs; only names matter here.
        recording = self

        class Sender:
            def send(self, **kwargs: Any) -> None:
                recording.sent.append(("(local)", name, kwargs))

        return Sender()


@pytest.fixture
def ai_off(monkeypatch: pytest.MonkeyPatch) -> Recording:
    """AI off in this process; messages sent by name are recorded, not enqueued."""
    monkeypatch.setattr(queue, "_ai_enabled", True)
    queue.set_ai_enabled(False)
    recording = Recording()
    monkeypatch.setattr(queue.dramatiq, "get_broker", lambda: recording)
    return recording


def _case(
    database: Database,
    *,
    entered: datetime,
    signals_at: datetime | None = None,
    decision: DecisionOutcome | None = None,
) -> uuid.UUID:
    """A ``detected`` case that entered the state at ``entered``."""
    with database.worker_session() as session:
        owner = factories.customer(session, f"no-ai-{uuid.uuid4().hex[:6]}")
        case = factories.case(session, owner, factories.brand(session, owner))
        session.add(
            CaseEvent(
                customer_id=owner.id,
                case_id=case.id,
                at=entered,
                type=EventType.DETECTED,
                actor="system",
            )
        )
        if signals_at is not None:
            session.add(
                Signal(
                    customer_id=owner.id,
                    case_id=case.id,
                    name="form_password",
                    value=True,
                    created_at=signals_at,
                )
            )
        if decision is not None:
            session.add(
                Decision(
                    customer_id=owner.id,
                    case_id=case.id,
                    outcome=decision,
                    rule="R1",
                    reasons=["test"],
                    decided_by="rule",
                    created_at=entered + timedelta(minutes=30),
                )
            )
        return case.id


def _review(database: Database, case_id: uuid.UUID) -> tuple[CaseState, list[Any], list[Any]]:
    with database.worker_session() as session:
        state = session.get_one(Case, case_id).state
        events = session.scalars(
            select(CaseEvent.data).where(
                CaseEvent.case_id == case_id, CaseEvent.type == EventType.REVIEW_REQUESTED
            )
        ).all()
        tasks = session.execute(
            select(ReviewTask.kind, ReviewTask.status).where(ReviewTask.case_id == case_id)
        ).all()
        return state, list(events), [tuple(task) for task in tasks]


def test_a_signalled_case_goes_to_review_with_the_reason_ai_disabled(database: Database) -> None:
    now = datetime.now(UTC)
    case_id = _case(database, entered=now - timedelta(hours=1), signals_at=now)
    assert review_without_ai(database, case_id, Stage.TRIAGE, now=now) is True
    state, events, tasks = _review(database, case_id)
    assert state is CaseState.IN_REVIEW
    [data] = events
    assert data["reason"] == AI_DISABLED
    assert data["stage"] == "triage"
    assert tasks == [(TaskKind.REVIEW, TaskStatus.OPEN)]
    # A redelivered message does nothing.
    assert review_without_ai(database, case_id, Stage.TRIAGE, now=now) is False
    assert len(_review(database, case_id)[1]) == 1


def test_a_case_whose_stage_is_not_due_stays_detected(database: Database) -> None:
    now = datetime.now(UTC)
    # No browser scan signals since it entered detected (e.g. the operator asked for a new
    # capture): triage is not due yet.
    stale = _case(database, entered=now - timedelta(hours=1), signals_at=now - timedelta(hours=2))
    assert review_without_ai(database, stale, Stage.TRIAGE, now=now) is False
    # Confirmation needs a confirm decision.
    watched = _case(database, entered=now - timedelta(hours=2), decision=DecisionOutcome.WATCH)
    assert review_without_ai(database, watched, Stage.CONFIRM, now=now) is False
    assert _review(database, stale)[0] is CaseState.DETECTED
    assert _review(database, watched)[0] is CaseState.DETECTED


def test_a_confirm_decision_goes_to_review_without_ai(database: Database) -> None:
    now = datetime.now(UTC)
    case_id = _case(database, entered=now - timedelta(hours=2), decision=DecisionOutcome.CONFIRM)
    assert review_without_ai(database, case_id, Stage.CONFIRM, now=now) is True
    state, [data], _ = _review(database, case_id)
    assert state is CaseState.IN_REVIEW
    assert (data["reason"], data["stage"]) == (AI_DISABLED, "confirm")


def test_signals_go_straight_to_review_through_the_stand_in(
    monkeypatch: pytest.MonkeyPatch, database: Database, ai_off: Recording
) -> None:
    from kalkan.workers import without_ai

    monkeypatch.setattr(without_ai, "get_database", lambda: database)
    now = datetime.now(UTC)
    case_id = _case(database, entered=now - timedelta(hours=1), signals_at=now)
    # What compute_signals sends after the signals.
    queue.send_ai_triage(str(case_id))
    [(queue_name, actor, kwargs)] = ai_off.sent
    assert (queue_name, actor) == ("decision", "triage_without_ai")
    BROKER.get_actor(actor).fn(**kwargs)
    assert _review(database, case_id)[0] is CaseState.IN_REVIEW


def test_the_reconciler_queues_no_ai_work_without_ai(
    monkeypatch: pytest.MonkeyPatch, database: Database, ai_off: Recording
) -> None:
    from kalkan.scheduler import reconciler
    from kalkan.scheduler.__main__ import send_message
    from kalkan.workers import without_ai

    monkeypatch.setattr(without_ai, "get_database", lambda: database)
    now = datetime.now(UTC)
    untriaged = _case(
        database, entered=now - timedelta(hours=5), signals_at=now - timedelta(hours=2)
    )
    unconfirmed = _case(
        database, entered=now - timedelta(hours=5), decision=DecisionOutcome.CONFIRM
    )
    mine = {str(untriaged), str(unconfirmed)}

    reconciler.reconcile(database, send_message, now)
    # Sent by name (the pre-check these bare cases also get is worker-default's own).
    sent = [
        (q, actor, kw)
        for q, actor, kw in ai_off.sent
        if kw.get("case_id") in mine and q != "(local)"
    ]
    assert sorted((q, actor) for q, actor, _ in sent) == [
        ("decision", "confirm_without_ai"),
        ("decision", "triage_without_ai"),
    ]
    assert all(q != queue.AI_QUEUE for q, _, _ in ai_off.sent)
    for _, actor, kwargs in sent:
        BROKER.get_actor(actor).fn(**kwargs)
    assert _review(database, untriaged)[0] is CaseState.IN_REVIEW
    assert _review(database, unconfirmed)[0] is CaseState.IN_REVIEW

    # In review now: the next run leaves them to the operator.
    ai_off.sent.clear()
    reconciler.reconcile(database, send_message, now + timedelta(hours=2))
    assert not [kw for _, _, kw in ai_off.sent if kw.get("case_id") in mine]
    assert not [actor for _, actor, _ in ai_off.sent if actor in queue.WITHOUT_AI]
