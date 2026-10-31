"""The decision engine and the review queue against the database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import (
    AuditLog,
    Case,
    CaseEvent,
    Decision,
    JobOutbox,
    Label,
    ReviewTask,
    Scan,
    Signal,
    Verdict,
)
from kalkan.db.session import Database
from kalkan.db.settings_store import write_setting
from kalkan.domain import case_state
from kalkan.domain.enums import (
    CaseState,
    Category,
    DecisionOutcome,
    EventType,
    NoticeBasis,
    TaskStatus,
    VerdictSource,
)
from kalkan.ops.web import Services
from kalkan.storage.gcs import ObjectStore
from tests.integration.ops_support import captured_case

NOW = datetime.now(UTC)


def clone_case(
    database: Database,
    store: ObjectStore,
    settings: Settings,
    *,
    forms: bool = True,
    entered: datetime | None = None,
) -> Case:
    """A captured, ``detected`` case with the code signals of a clone (or of an empty page)."""
    case = captured_case(database, store, settings)
    with database.worker_session() as session:
        scan = session.scalars(select(Scan).where(Scan.case_id == case.id)).one()
        rows: list[tuple[uuid.UUID | None, str, Any]] = [
            (scan.id, "form_password", forms),
            (scan.id, "form_card", False),
            (scan.id, "redirects_to_official", False),
            (None, "visual_similarity", 0.95 if forms else 0.1),
            (None, "official_infrastructure", False),
        ]
        for scan_id, name, value in rows:
            session.add(
                Signal(
                    customer_id=case.customer_id,
                    case_id=case.id,
                    scan_id=scan_id,
                    name=name,
                    value=value,
                )
            )
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=entered or NOW - timedelta(hours=3),
                type=EventType.DETECTED,
                actor="system",
            )
        )
    return case


def add_verdict(
    database: Database,
    case: Case,
    source: VerdictSource,
    category: Category = Category.PHISHING_CLONE,
    *,
    at: datetime | None = None,
    confidence: str = "0.950",
) -> None:
    with database.worker_session() as session:
        session.add(
            Verdict(
                customer_id=case.customer_id,
                case_id=case.id,
                source=source,
                model="claude-haiku-5-5",
                prompt_version="test",
                category=category,
                confidence=Decimal(confidence),
                copied_elements=["logo"],
                collects_credentials_or_payment=category is Category.PHISHING_CLONE,
                evidence=["Image 1 shows the brand logo."],
                suggested_basis=NoticeBasis.PHISHING,
                short_explanation="A copy of the login page.",
                created_at=at or datetime.now(UTC),
            )
        )


@pytest.fixture
def decide_actor(monkeypatch: pytest.MonkeyPatch, database: Database) -> dict[str, list[str]]:
    from kalkan.workers import decision as worker

    sent: dict[str, list[str]] = {"ai_confirm": []}
    monkeypatch.setattr(worker, "get_database", lambda: database)
    monkeypatch.setattr(worker, "send_ai_confirm", sent["ai_confirm"].append)
    return sent


def _jobs(database: Database, kind: str) -> list[str]:
    with database.worker_session() as session:
        rows = session.scalars(select(JobOutbox.payload).where(JobOutbox.kind == kind))
        return [payload["case_id"] for payload in rows]


def _state(database: Database, case_id: uuid.UUID) -> Case:
    with database.worker_session() as session:
        found = session.get_one(Case, case_id)
        session.expunge(found)
        return found


def _decisions(database: Database, case_id: uuid.UUID) -> list[tuple[str, str, str]]:
    with database.worker_session() as session:
        return [
            (row.outcome.value, row.rule, row.decided_by)
            for row in session.scalars(
                select(Decision).where(Decision.case_id == case_id).order_by(Decision.created_at)
            )
        ]


def _events(database: Database, case_id: uuid.UUID) -> list[str]:
    with database.worker_session() as session:
        return [
            event.type.value
            for event in session.scalars(
                select(CaseEvent)
                .where(CaseEvent.case_id == case_id, CaseEvent.type != EventType.DETECTED)
                .order_by(CaseEvent.at, CaseEvent.id)
            )
        ]


def test_a_clone_goes_through_confirmation_to_review_while_auto_approve_is_off(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    decide_actor: dict[str, list[str]],
) -> None:
    from kalkan.workers.decision import decide

    case = clone_case(database, object_store, test_settings)
    add_verdict(database, case, VerdictSource.AI_TRIAGE)
    decide.fn(str(case.id))
    assert decide_actor["ai_confirm"] == [str(case.id)]
    found = _state(database, case.id)
    assert found.state is CaseState.DETECTED
    assert (found.category, found.confidence) == (Category.PHISHING_CLONE, Decimal("0.950"))
    assert _decisions(database, case.id) == [("confirm", "R1", "rule")]

    decide.fn(str(case.id))  # redelivered: the confirmation is not here yet
    assert len(_decisions(database, case.id)) == 1

    add_verdict(database, case, VerdictSource.AI_CONFIRM)
    decide.fn(str(case.id))
    assert _state(database, case.id).state is CaseState.IN_REVIEW
    assert _jobs(database, "plan_notices") == []
    assert _decisions(database, case.id)[-1] == ("review", "C2", "rule")
    assert _events(database, case.id) == ["decision", "decision", "review_requested"]
    with database.worker_session() as session:
        task = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).one()
    assert task.status is TaskStatus.OPEN
    assert (task.reason or "").startswith("Auto-approve disabled")


def test_auto_approve_on_approves_and_calls_the_notice_hook(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    decide_actor: dict[str, list[str]],
) -> None:
    from kalkan.workers.decision import decide

    with database.worker_session() as session:
        write_setting(session, "auto_approve_enabled", True, actor="test")
    case = clone_case(database, object_store, test_settings)
    add_verdict(database, case, VerdictSource.AI_TRIAGE)
    decide.fn(str(case.id))
    add_verdict(database, case, VerdictSource.AI_CONFIRM)
    decide.fn(str(case.id))
    found = _state(database, case.id)
    assert found.state is CaseState.APPROVED
    assert (found.approved_by, found.auto_approved) == ("rule", True)
    assert found.verified_at is not None
    assert _jobs(database, "plan_notices") == [str(case.id)]
    assert _jobs(database, "customer_alert") == [str(case.id)]
    assert _events(database, case.id) == ["decision", "decision", "verified", "approved"]


def test_a_parked_page_is_watched_with_its_next_check(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    decide_actor: dict[str, list[str]],
) -> None:
    from kalkan.workers.decision import decide

    case = clone_case(database, object_store, test_settings, forms=False)
    add_verdict(database, case, VerdictSource.AI_TRIAGE, Category.PARKED_OR_EMPTY)
    before = datetime.now(UTC)
    decide.fn(str(case.id))
    found = _state(database, case.id)
    assert found.state is CaseState.WATCHING
    assert found.next_check_at is not None
    assert timedelta(days=7) <= found.next_check_at - before < timedelta(days=7, minutes=1)
    assert _decisions(database, case.id) == [("watch", "R3", "rule")]
    with database.worker_session() as session:
        event = session.scalars(
            select(CaseEvent).where(
                CaseEvent.case_id == case.id, CaseEvent.type == EventType.DECISION
            )
        ).one()
    assert event.data["reasons"] == ["Triage says the page is parked or empty."]


def test_an_affiliate_is_dismissed_for_30_days(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    decide_actor: dict[str, list[str]],
) -> None:
    from kalkan.workers.decision import decide

    case = clone_case(database, object_store, test_settings, forms=False)
    add_verdict(database, case, VerdictSource.AI_TRIAGE, Category.AFFILIATE_OR_REVIEW)
    decide.fn(str(case.id))
    found = _state(database, case.id)
    assert found.state is CaseState.DISMISSED
    assert found.next_check_at is not None
    assert found.next_check_at - datetime.now(UTC) > timedelta(days=29)


def test_the_last_failed_attempt_is_job_failed(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    decide_actor: dict[str, list[str]],
) -> None:
    from kalkan.workers import decision as worker

    case = clone_case(database, object_store, test_settings)

    def broken(*args: object, **kwargs: object) -> None:
        raise ConnectionError("database gone")

    monkeypatch.setattr(worker, "apply_decision", broken)
    monkeypatch.setattr(worker, "final_attempt", lambda retries: True)
    worker.decide.fn(str(case.id))
    assert _events(database, case.id) == ["job_failed"]


def test_the_reconciler_requeues_undecided_cases(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    from kalkan.scheduler import reconciler

    now = datetime.now(UTC)
    stuck = clone_case(database, object_store, test_settings)
    add_verdict(database, stuck, VerdictSource.AI_TRIAGE, at=now - timedelta(hours=1))
    recent = clone_case(database, object_store, test_settings)
    add_verdict(database, recent, VerdictSource.AI_TRIAGE, at=now - timedelta(minutes=5))
    decided = clone_case(database, object_store, test_settings)
    add_verdict(database, decided, VerdictSource.AI_TRIAGE, at=now - timedelta(hours=1))
    with database.worker_session() as session:
        session.add(
            Decision(
                customer_id=decided.customer_id,
                case_id=decided.id,
                outcome=DecisionOutcome.CONFIRM,
                rule="R1",
                reasons=[],
                decided_by="rule",
                created_at=now - timedelta(minutes=50),
            )
        )
    sent: list[tuple[str, dict[str, str]]] = []
    result = reconciler.reconcile(database, lambda actor, **kw: sent.append((actor, kw)), now)
    assert [kw["case_id"] for actor, kw in sent if actor == "decide"] == [str(stuck.id)]
    assert result.decisions == 1


# --- the review queue -------------------------------------------------------------------------


def review_case(services: Services, category: Category = Category.PHISHING_CLONE) -> Case:
    """A case in review with a triage verdict and a rule's suggestion."""
    case = clone_case(services.database, services.store, services.settings)
    add_verdict(services.database, case, VerdictSource.AI_TRIAGE, category)
    with services.database.worker_session() as session:
        row = session.get_one(Case, case.id)
        row.category = category
        session.add(
            Decision(
                customer_id=case.customer_id,
                case_id=case.id,
                outcome=DecisionOutcome.REVIEW,
                rule="C2",
                reasons=["Auto-approve disabled: both models say phishing clone."],
                decided_by="rule",
            )
        )
        case_state.transition(
            session, row, CaseState.IN_REVIEW, actor="rule", reason="Auto-approve disabled."
        )
    return _state(services.database, case.id)


def _post(client: TestClient, csrf: str, case: Case, choice: str, label: bool = True) -> Any:
    data = {"csrf": csrf, "choice": choice}
    if label:
        data["save_label"] = "true"
    return client.post(f"/review/{case.id}", data=data, follow_redirects=False)


def test_the_review_page_shows_the_evidence_and_the_suggestion(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, _ = signed_in
    case = review_case(ops_services)
    page = client.get("/review")
    assert page.status_code == 200
    for text in (
        case.number,
        "<strong>1</strong> waiting",
        "Auto-approve disabled.",
        "Rule C2 suggested",
        "A copy of the login page.",
        "Image 1 shows the brand logo.",
        'data-keys="ap"',
        'data-keys="r3"',
        'src="/files/artifacts/',
        "Save as a label too",
    ):
        assert text in page.text, text


def test_approving_writes_the_basis_the_label_and_the_audit_log(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    case = review_case(ops_services)
    other = review_case(ops_services)
    response = _post(client, csrf, case, "approve:trademark")
    assert response.status_code == 303
    assert response.headers["location"] == f"/review?after={case.number}"
    found = _state(ops_services.database, case.id)
    assert found.state is CaseState.APPROVED
    assert found.approved_by == "operator@kalkan.test"
    assert found.auto_approved is False
    with ops_services.database.worker_session() as session:
        staff = session.scalars(
            select(Verdict).where(Verdict.case_id == case.id, Verdict.source == VerdictSource.STAFF)
        ).one()
        label = session.scalars(select(Label).where(Label.case_id == case.id)).one()
        audit = list(session.scalars(select(AuditLog.action).where(AuditLog.target == case.number)))
        task = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).one()
    assert staff.suggested_basis is NoticeBasis.TRADEMARK
    assert label.category is Category.BRAND_IMPERSONATION
    assert audit == ["review.approve", "label.set"]
    assert task.status is TaskStatus.DONE
    assert _decisions(ops_services.database, case.id)[-1] == (
        "approve",
        "staff",
        "operator@kalkan.test",
    )
    assert _events(ops_services.database, case.id)[-3:] == ["decision", "verified", "approved"]
    # The next case of the queue opens.
    assert other.number in client.get(response.headers["location"]).text


@pytest.mark.parametrize(
    ("choice", "state", "label"),
    [
        ("verify", CaseState.VERIFIED, Category.PHISHING_CLONE),
        ("reject:affiliate_or_review", CaseState.DISMISSED, Category.AFFILIATE_OR_REVIEW),
        ("watch", CaseState.WATCHING, None),
    ],
)
def test_the_other_decisions(
    signed_in: tuple[TestClient, str],
    ops_services: Services,
    choice: str,
    state: CaseState,
    label: Category | None,
) -> None:
    client, csrf = signed_in
    case = review_case(ops_services)
    assert _post(client, csrf, case, choice).status_code == 303
    found = _state(ops_services.database, case.id)
    assert found.state is state
    if state in {CaseState.DISMISSED, CaseState.WATCHING}:
        assert found.next_check_at is not None
    with ops_services.database.worker_session() as session:
        labels = list(session.scalars(select(Label.category).where(Label.case_id == case.id)))
    assert labels == ([label] if label else [])


def test_a_label_is_not_saved_when_the_box_is_cleared(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    case = review_case(ops_services)
    _post(client, csrf, case, "reject:unrelated", label=False)
    with ops_services.database.worker_session() as session:
        assert session.scalars(select(Label).where(Label.case_id == case.id)).all() == []


def test_recapture_sends_the_case_back_through_the_pipeline(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    case = review_case(ops_services)
    response = _post(client, csrf, case, "recapture")
    assert response.status_code == 303
    assert _state(ops_services.database, case.id).state is CaseState.DETECTED
    assert "capture" in response.headers.get("set-cookie", "")
    with ops_services.database.worker_session() as session:
        task = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).one()
    assert task.status is TaskStatus.DONE
    assert _events(ops_services.database, case.id)[-1] == "decision"
    with ops_services.database.worker_session() as session:
        entered = session.scalars(
            select(CaseEvent).where(
                CaseEvent.case_id == case.id, CaseEvent.type == EventType.DETECTED
            )
        ).all()
    assert [event.data.get("reason") for event in entered] == [None, "recapture"]


def test_a_case_that_left_review_cannot_be_decided_again(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    case = review_case(ops_services)
    _post(client, csrf, case, "watch")
    again = _post(client, csrf, case, "approve:phishing")
    assert "not%20in%20review" in again.headers.get("set-cookie", "")
    assert _state(ops_services.database, case.id).state is CaseState.WATCHING
    assert _post(client, csrf, case, "explode").status_code == 303
