"""Triage and confirmation of a case (architecture.md §7, §13, §14).

``assess(case_id, stage)`` builds the case snapshot and the three-layer prompt, asks the
stage's model for a :class:`~kalkan.ai.schema.Verdict` and writes it to ``verdicts``. What the
verdict means is decided by the decision engine (``decide``); nothing here sends or
approves anything.

- triage (``AI_TRIAGE_MODEL``, effort low): a ``detected`` case whose browser scan signals
  (``compute_signals``) are newer than its last triage verdict.
- confirm (``AI_CONFIRM_MODEL``, effort medium): a ``detected`` case whose newest decision is
  ``confirm`` and that has no confirmation verdict since.

When no verdict can be had the case goes to ``in_review``: ``ai_unavailable`` (API errors after
3 attempts, a refused or invalid answer twice) or ``ai_budget_exhausted`` (monthly budget).
Anything else that fails is raised for the actor's retries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.ai.brand_files import (
    StoredImage,
    brand_images,
    newest_assets,
    read_image,
    whitelist_summary,
)
from kalkan.ai.client import AiClient, AiError, BudgetExhaustedError, Effort
from kalkan.ai.images import PreparedImage
from kalkan.ai.prompting import Prompt, build_prompt
from kalkan.ai.schema import Verdict as VerdictOutput
from kalkan.config import Settings
from kalkan.db.models import Artifact, Case, Decision, Signal
from kalkan.db.models import Verdict as VerdictRow
from kalkan.db.session import Database
from kalkan.domain import case_state
from kalkan.domain.enums import (
    AiPurpose,
    CaseState,
    Category,
    DecisionOutcome,
    NoticeBasis,
    Profile,
    VerdictSource,
)
from kalkan.logging import get_logger
from kalkan.signals.snapshot import CaseSnapshot, build_snapshot
from kalkan.storage.gcs import ObjectStore

SYSTEM = "system"
# compute_signals writes it for every browser scan it reads (the reconciler uses it too).
SCAN_SIGNAL = "form_password"

log = get_logger(__name__)


class Stage(StrEnum):
    TRIAGE = "triage"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class StageSpec:
    source: VerdictSource
    purpose: AiPurpose
    effort: Effort


STAGES = {
    Stage.TRIAGE: StageSpec(VerdictSource.AI_TRIAGE, AiPurpose.TRIAGE, "low"),
    Stage.CONFIRM: StageSpec(VerdictSource.AI_CONFIRM, AiPurpose.CONFIRM, "medium"),
}


class AssessStatus(StrEnum):
    DONE = "done"  # a verdict was written; decide comes next
    REVIEW = "review"  # no verdict; the case went to in_review
    SKIPPED = "skipped"  # nothing to do (state, already assessed)


def model_for(settings: Settings, stage: Stage) -> str:
    return settings.ai_triage_model if stage is Stage.TRIAGE else settings.ai_confirm_model


@dataclass(frozen=True)
class Inputs:
    snapshot: CaseSnapshot
    whitelist: list[str]
    assets: list[StoredImage]
    screenshots: dict[Profile, str]  # profile -> storage key


def _due(session: Session, case: Case, stage: Stage) -> bool:
    """Whether the stage still has work on ``case`` (redelivered messages do nothing)."""
    if stage is Stage.TRIAGE:
        signals_at = session.scalar(
            select(func.max(Signal.created_at)).where(
                Signal.case_id == case.id, Signal.name == SCAN_SIGNAL
            )
        )
        if signals_at is None:
            return False
        done = session.scalar(
            select(VerdictRow.id).where(
                VerdictRow.case_id == case.id,
                VerdictRow.source == VerdictSource.AI_TRIAGE,
                VerdictRow.created_at >= signals_at,
            )
        )
        return done is None
    decision = session.scalar(
        select(Decision)
        .where(Decision.case_id == case.id)
        .order_by(Decision.created_at.desc())
        .limit(1)
    )
    if decision is None or decision.outcome is not DecisionOutcome.CONFIRM:
        return False
    done = session.scalar(
        select(VerdictRow.id).where(
            VerdictRow.case_id == case.id,
            VerdictRow.source == VerdictSource.AI_CONFIRM,
            VerdictRow.created_at >= decision.created_at,
        )
    )
    return done is None


def load_inputs(
    database: Database, case_id: uuid.UUID, stage: Stage, now: datetime
) -> Inputs | None:
    with database.worker_session() as session:
        case = session.get(Case, case_id)
        if case is None or case.state is not CaseState.DETECTED or not _due(session, case, stage):
            return None
        snapshot = build_snapshot(session, case_id, now=now)
        if snapshot is None:
            return None
        screenshots: dict[Profile, str] = {}
        for scan in snapshot.scans:
            if scan.profile is None or scan.screenshot_artifact_id is None:
                continue
            key = session.scalar(
                select(Artifact.storage_key).where(Artifact.id == scan.screenshot_artifact_id)
            )
            if key is not None:
                screenshots[scan.profile] = key
        return Inputs(
            snapshot=snapshot,
            whitelist=whitelist_summary(session, case.customer_id, case.brand_id),
            assets=newest_assets(session, case.brand_id),
            screenshots=screenshots,
        )


def prompt_for(database: Database, store: ObjectStore, client: AiClient, inputs: Inputs) -> Prompt:
    candidate: dict[Profile, PreparedImage | None] = {
        profile: read_image(store, key) for profile, key in inputs.screenshots.items()
    }
    return build_prompt(
        inputs.snapshot,
        whitelist=inputs.whitelist,
        brand_images=brand_images(database, store, client.api, inputs.assets),
        candidate_images=candidate,
    )


def _to_review(
    database: Database, case_id: uuid.UUID, stage: Stage, error: AiError, now: datetime
) -> bool:
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            return False
        case_state.transition(
            session,
            case,
            CaseState.IN_REVIEW,
            actor=SYSTEM,
            reason=error.reason,
            data={"stage": stage.value, "error": str(error)[:500]},
            at=now,
        )
    return True


def _save(
    database: Database,
    case_id: uuid.UUID,
    stage: Stage,
    output: VerdictOutput,
    *,
    model: str,
    prompt_version: str,
    ai_call_id: uuid.UUID,
    now: datetime,
) -> bool:
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            return False
        session.add(
            VerdictRow(
                customer_id=case.customer_id,
                case_id=case.id,
                source=STAGES[stage].source,
                model=model,
                prompt_version=prompt_version,
                category=Category(output.category),
                confidence=Decimal(str(round(output.confidence, 3))),
                copied_elements=list(output.copied_elements),
                collects_credentials_or_payment=output.collects_credentials_or_payment,
                evidence=list(output.evidence),
                suggested_basis=NoticeBasis(output.suggested_basis),
                short_explanation=output.short_explanation,
                ai_call_id=ai_call_id,
                created_at=now,
            )
        )
    return True


def assess(
    database: Database,
    store: ObjectStore,
    client: AiClient,
    settings: Settings,
    case_id: uuid.UUID,
    stage: Stage,
    *,
    now: datetime | None = None,
) -> AssessStatus:
    at = now or datetime.now(UTC)
    inputs = load_inputs(database, case_id, stage, at)
    if inputs is None:
        log.info("ai.skipped", case_id=str(case_id), stage=stage.value)
        return AssessStatus.SKIPPED
    spec = STAGES[stage]
    model = model_for(settings, stage)
    try:
        client.check_budget()
        prompt = prompt_for(database, store, client, inputs)
        answer = client.call(
            prompt,
            output=VerdictOutput,
            model=model,
            purpose=spec.purpose,
            case_id=case_id,
            effort=spec.effort,
            check_budget=False,  # checked above, before any upload
        )
    except AiError as exc:
        if isinstance(exc, BudgetExhaustedError):
            from kalkan.ai.metrics import metrics

            metrics().budget_stops.inc()
        moved = _to_review(database, case_id, stage, exc, datetime.now(UTC))
        log.warning(
            "ai.no_verdict", case_id=str(case_id), stage=stage.value, reason=exc.reason, moved=moved
        )
        return AssessStatus.REVIEW if moved else AssessStatus.SKIPPED
    saved = _save(
        database,
        case_id,
        stage,
        answer.output,
        model=answer.model,
        prompt_version=answer.prompt_version,
        ai_call_id=answer.ai_call_id,
        now=datetime.now(UTC),
    )
    log.info(
        "ai.verdict",
        case_id=str(case_id),
        stage=stage.value,
        model=model,
        category=answer.output.category,
        confidence=answer.output.confidence,
        cost_usd=str(answer.cost_usd),
        saved=saved,
    )
    return AssessStatus.DONE if saved else AssessStatus.SKIPPED
