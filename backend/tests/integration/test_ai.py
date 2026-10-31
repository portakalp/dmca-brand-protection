"""Triage and confirmation against the database and the storage emulator.

The Claude API is the fake of ``tests/ai_support.py``: recorded responses, no network.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from kalkan.ai.client import AiClient, DatabaseRecorder
from kalkan.ai.evaluation import export_dataset, load_dataset
from kalkan.config import Settings
from kalkan.db.models import (
    AiCall,
    BrandAsset,
    Case,
    CaseEvent,
    Decision,
    Label,
    ReviewTask,
    Scan,
    Signal,
    Verdict,
)
from kalkan.db.session import Database
from kalkan.domain.enums import (
    AiPurpose,
    BrandAssetKind,
    CaseState,
    Category,
    DecisionOutcome,
    EventType,
    TaskStatus,
    VerdictSource,
)
from kalkan.storage.gcs import ObjectStore
from tests.ai_support import FakeApi, recorded
from tests.integration.ops_support import captured_case


def _signalled_case(
    database: Database,
    store: ObjectStore,
    settings: Settings,
    *,
    signals_at: datetime | None = None,
) -> Case:
    """A captured case whose browser scan has its signals."""
    case = captured_case(database, store, settings)
    with database.worker_session() as session:
        scan = session.scalars(select(Scan).where(Scan.case_id == case.id)).one()
        session.add(
            Signal(
                customer_id=case.customer_id,
                case_id=case.id,
                scan_id=scan.id,
                name="form_password",
                value=True,
                created_at=signals_at or datetime.now(UTC),
            )
        )
    return case


@pytest.fixture
def actor_env(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
) -> tuple[FakeApi, list[str]]:
    """worker-ai's actors on the test database, the emulator and a fake API."""
    from kalkan.workers import assess as worker

    api = FakeApi()
    client = api.client(recorder=DatabaseRecorder(database))
    decided: list[str] = []
    monkeypatch.setattr(worker, "get_database", lambda: database)
    monkeypatch.setattr(worker, "object_store", lambda: object_store)
    monkeypatch.setattr(worker, "ai_client", lambda: client)
    monkeypatch.setattr(worker, "get_settings", lambda: test_settings)
    monkeypatch.setattr(worker, "send_decide", decided.append)
    return api, decided


def _calls(database: Database, case_id: uuid.UUID) -> list[AiCall]:
    with database.worker_session() as session:
        return list(
            session.scalars(
                select(AiCall).where(AiCall.case_id == case_id).order_by(AiCall.created_at)
            )
        )


def _review(database: Database, case_id: uuid.UUID) -> tuple[CaseState, str | None]:
    with database.worker_session() as session:
        state = session.get_one(Case, case_id).state
        task = session.scalar(
            select(ReviewTask).where(
                ReviewTask.case_id == case_id, ReviewTask.status == TaskStatus.OPEN
            )
        )
        return state, task.reason if task is not None else None


def test_triage_writes_a_verdict_uploads_the_brand_images_and_queues_decide(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    actor_env: tuple[FakeApi, list[str]],
) -> None:
    from kalkan.workers.assess import ai_triage

    api, decided = actor_env
    api.queue("message_verdict.json")
    case = _signalled_case(database, object_store, test_settings)
    ai_triage.fn(str(case.id))
    assert decided == [str(case.id)]
    with database.worker_session() as session:
        verdict = session.scalars(select(Verdict).where(Verdict.case_id == case.id)).one()
        asset = session.scalars(
            select(BrandAsset).where(BrandAsset.kind == BrandAssetKind.REFERENCE_DESKTOP)
        ).one()
        state = session.get_one(Case, case.id).state
    assert (verdict.source, verdict.category, verdict.confidence) == (
        VerdictSource.AI_TRIAGE,
        Category.PHISHING_CLONE,
        Decimal("0.940"),
    )
    assert verdict.model == test_settings.ai_triage_model
    assert state is CaseState.DETECTED  # what the verdict means is decide's business
    [call] = _calls(database, case.id)
    assert verdict.ai_call_id == call.id
    assert (call.purpose, call.cache_write_tokens, call.error) == (AiPurpose.TRIAGE, 2600, None)
    assert call.cost_usd > 0
    assert call.prompt_version == verdict.prompt_version
    assert asset.anthropic_file_id == recorded("file_upload.json")["id"]
    [body] = api.bodies()
    images = [b for b in body["messages"][0]["content"] if b["type"] == "image"]
    assert [image["source"]["type"] for image in images] == ["file", "base64"]
    assert body["output_config"]["effort"] == "low"

    # A redelivered message finds the verdict and does nothing; no second upload either.
    ai_triage.fn(str(case.id))
    assert len(api.bodies()) == 1
    assert len(api.posts("/v1/files")) == 1
    assert decided == [str(case.id)]


def test_a_used_up_budget_sends_the_case_to_review_without_a_call(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    actor_env: tuple[FakeApi, list[str]],
) -> None:
    from kalkan.workers.assess import ai_triage

    api, decided = actor_env
    case = _signalled_case(database, object_store, test_settings)
    with database.worker_session() as session:
        session.add(
            AiCall(
                purpose=AiPurpose.TRIAGE,
                model="claude-haiku-5-5",
                prompt_version="x",
                cost_usd=Decimal("50.000000"),
            )
        )
    ai_triage.fn(str(case.id))
    assert api.requests == []
    assert decided == []
    assert _review(database, case.id) == (CaseState.IN_REVIEW, "ai_budget_exhausted")
    assert _calls(database, case.id) == []


def test_an_unavailable_api_sends_the_case_to_review(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    actor_env: tuple[FakeApi, list[str]],
) -> None:
    from kalkan.workers.assess import ai_triage

    api, decided = actor_env
    api.queue(*[(529, recorded("error_overloaded.json"))] * 3)
    case = _signalled_case(database, object_store, test_settings)
    ai_triage.fn(str(case.id))
    assert decided == []
    assert _review(database, case.id) == (CaseState.IN_REVIEW, "ai_unavailable")
    calls = _calls(database, case.id)
    assert len(calls) == 3
    assert all(call.error and "529" in call.error for call in calls)
    with database.worker_session() as session:
        event = session.scalars(
            select(CaseEvent).where(
                CaseEvent.case_id == case.id, CaseEvent.type == EventType.REVIEW_REQUESTED
            )
        ).one()
    assert event.data["stage"] == "triage"


def test_confirmation_needs_a_confirm_decision(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    actor_env: tuple[FakeApi, list[str]],
) -> None:
    from kalkan.workers.assess import ai_confirm

    api, decided = actor_env
    case = _signalled_case(database, object_store, test_settings)
    ai_confirm.fn(str(case.id))
    assert api.requests == []
    with database.worker_session() as session:
        session.add(
            Decision(
                customer_id=case.customer_id,
                case_id=case.id,
                outcome=DecisionOutcome.CONFIRM,
                rule="R1",
                reasons=["test"],
                decided_by="rule",
            )
        )
    api.queue("message_verdict.json")
    ai_confirm.fn(str(case.id))
    assert decided == [str(case.id)]
    [body] = api.bodies()
    assert body["model"] == test_settings.ai_confirm_model
    assert body["output_config"]["effort"] == "medium"
    with database.worker_session() as session:
        sources = list(session.scalars(select(Verdict.source).where(Verdict.case_id == case.id)))
    assert sources == [VerdictSource.AI_CONFIRM]


def test_the_last_failed_attempt_records_job_failed(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    actor_env: tuple[FakeApi, list[str]],
) -> None:
    from kalkan.workers import assess as worker

    case = _signalled_case(database, object_store, test_settings)

    def broken(*args: object, **kwargs: object) -> None:
        raise ConnectionError("database gone")

    monkeypatch.setattr(worker, "assess", broken)
    monkeypatch.setattr(worker, "final_attempt", lambda retries: False)
    with pytest.raises(ConnectionError):
        worker.ai_triage.fn(str(case.id))
    monkeypatch.setattr(worker, "final_attempt", lambda retries: True)
    worker.ai_triage.fn(str(case.id))
    with database.worker_session() as session:
        jobs = list(
            session.scalars(
                select(CaseEvent.data).where(
                    CaseEvent.case_id == case.id, CaseEvent.type == EventType.JOB_FAILED
                )
            )
        )
    assert [job["job"] for job in jobs] == ["ai_triage"]


# --- reconciler -------------------------------------------------------------------------------


def _detected_at(database: Database, case: Case, at: datetime) -> None:
    with database.worker_session() as session:
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=at,
                type=EventType.DETECTED,
                actor="system",
            )
        )


def test_the_reconciler_requeues_triage_and_confirmation(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    from kalkan.scheduler import reconciler

    now = datetime.now(UTC)
    entered = now - timedelta(hours=5)
    hours_ago = now - timedelta(hours=2)
    cases = {
        name: _signalled_case(
            database,
            object_store,
            test_settings,
            signals_at=now - timedelta(minutes=10) if name == "fresh" else hours_ago,
        )
        for name in ("untriaged", "fresh", "triaged", "confirm", "confirmed", "watch")
    }
    for case in cases.values():
        _detected_at(database, case, entered)
    with database.worker_session() as session:
        for name in ("triaged", "confirm", "confirmed", "watch"):
            case = cases[name]
            session.add(
                Verdict(
                    customer_id=case.customer_id,
                    case_id=case.id,
                    source=VerdictSource.AI_TRIAGE,
                    category=Category.PHISHING_CLONE,
                    confidence=Decimal("0.95"),
                    copied_elements=[],
                    collects_credentials_or_payment=True,
                    evidence=[],
                    suggested_basis="phishing",
                    short_explanation="x",
                    created_at=hours_ago + timedelta(minutes=5),
                )
            )
        for name, outcome in (
            ("confirm", DecisionOutcome.CONFIRM),
            ("confirmed", DecisionOutcome.CONFIRM),
            ("watch", DecisionOutcome.WATCH),
        ):
            session.add(
                Decision(
                    customer_id=cases[name].customer_id,
                    case_id=cases[name].id,
                    outcome=outcome,
                    rule="R1",
                    reasons=[],
                    decided_by="rule",
                    created_at=hours_ago + timedelta(minutes=10),
                )
            )
        case = cases["confirmed"]
        session.add(
            Verdict(
                customer_id=case.customer_id,
                case_id=case.id,
                source=VerdictSource.AI_CONFIRM,
                category=Category.PHISHING_CLONE,
                confidence=Decimal("0.95"),
                copied_elements=[],
                collects_credentials_or_payment=True,
                evidence=[],
                suggested_basis="phishing",
                short_explanation="x",
                created_at=hours_ago + timedelta(minutes=15),
            )
        )
    sent: list[tuple[str, dict[str, str]]] = []
    result = reconciler.reconcile(database, lambda actor, **kw: sent.append((actor, kw)), now)
    by_actor: dict[str, list[str]] = {}
    for actor, kwargs in sent:
        by_actor.setdefault(actor, []).append(kwargs["case_id"])
    assert by_actor.get("ai_triage") == [str(cases["untriaged"].id)]
    assert by_actor.get("ai_confirm") == [str(cases["confirm"].id)]
    assert (result.triages, result.confirms) == (1, 1)


# --- evaluation export ------------------------------------------------------------------------


def test_the_export_writes_labelled_cases_that_read_back(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> None:
    case = _signalled_case(database, object_store, test_settings)
    _signalled_case(database, object_store, test_settings)  # not labelled
    with database.worker_session() as session:
        session.add(Label(case_id=case.id, category=Category.PHISHING_CLONE, labeled_by="op"))
    now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    exported = export_dataset(database, object_store, tmp_path, now=now)
    assert exported.path == tmp_path / "2026-11-20.jsonl"
    assert exported.cases == 1
    meta = json.loads(exported.path.read_text().splitlines()[0])
    assert meta["thresholds"] == {"triage_min_confidence": 0.9, "visual_similarity_min": 0.85}
    dataset = load_dataset(exported.path)
    [item] = dataset.cases
    assert item.label is Category.PHISHING_CLONE
    assert item.snapshot.case.id == case.id
    assert [image.label for image in item.brand_images] == ["Official site, desktop"]
    assert list(item.screenshots) == ["desktop"]
    assert item.prompt().images == 2
    assert all(path.suffix == ".png" for path in (tmp_path / "images").iterdir())


def test_a_db_recorded_eval_call_counts_towards_the_budget(database: Database) -> None:
    client: AiClient = (
        FakeApi()
        .queue("message_verdict.json")
        .client(recorder=DatabaseRecorder(database), budget=Decimal("1"))
    )
    before = client.budget_left()
    from kalkan.ai.prompting import Prompt
    from kalkan.ai.schema import Verdict as Output

    client.call(
        Prompt(system="s", content=[{"type": "text", "text": "c"}]),
        output=Output,
        model="claude-haiku-5-5",
        purpose=AiPurpose.EVAL,
    )
    assert client.budget_left() == before - Decimal("0.000770")
