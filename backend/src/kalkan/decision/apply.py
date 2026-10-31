"""Applying decisions (architecture.md §6, §14): the rule engine's and the operator's.

:func:`decide` runs in the ``decide`` actor after a verdict. It reads the case snapshot, the
newest triage and confirmation verdicts since the case entered ``detected`` and the
``settings`` thresholds, asks :mod:`kalkan.decision.rules`, and applies the answer in one
transaction: a ``decisions`` row, a ``decision`` event, the state change with its own event,
``next_check_at`` for ``watching`` (+7 days, R5 +1 day) and ``dismissed`` (+30 days), and the
verdict's category and confidence on the case. ``in_review`` opens its review task by itself
(``case_state.transition``). ``confirm`` keeps the case ``detected``; the caller then queues
``ai_confirm``. An approved case goes through ``verified`` to ``approved`` and leaves a
``plan_notices`` outbox job in the same transaction (``notices/planning.py``); a case
verified as a phishing clone leaves a ``customer_alert`` job.

Which stage is due: when the newest decision is ``confirm``, a confirmation verdict newer than
it; otherwise a triage verdict newer than the newest decision. Anything else is a redelivered
message and does nothing.

The operator's decisions from the review queue (:func:`staff_decision`) write the same rows and
an ``audit_log`` row; approving or rejecting also writes a ``staff`` verdict, whose
``suggested_basis`` is the basis the notices use (K132).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.db.models import AuditLog, CaseEvent, Decision, Verdict
from kalkan.db.models import Case as CaseRow
from kalkan.db.outbox import CUSTOMER_ALERT, PLAN_NOTICES, queue_job
from kalkan.db.session import Database
from kalkan.db.settings_store import read_settings
from kalkan.decision import rules
from kalkan.decision.rules import Thresholds, VerdictView
from kalkan.domain import case_state
from kalkan.domain.enums import (
    CaseState,
    Category,
    DecisionOutcome,
    EventType,
    NoticeBasis,
    VerdictSource,
)
from kalkan.logging import get_logger
from kalkan.signals.snapshot import build_snapshot

RULE_ACTOR = "rule"
STAFF_RULE = "staff"
MAX_REASON = 500

log = get_logger(__name__)


class Stage(StrEnum):
    TRIAGE = "triage"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class Applied:
    case_id: uuid.UUID
    stage: Stage
    decision: rules.Decision
    state: CaseState

    @property
    def queue_confirm(self) -> bool:
        return self.decision.outcome is DecisionOutcome.CONFIRM

    @property
    def approved(self) -> bool:
        return self.state is CaseState.APPROVED


def entered_detected(session: Session, case_id: uuid.UUID) -> datetime | None:
    return session.scalar(
        select(func.max(CaseEvent.at)).where(
            CaseEvent.case_id == case_id, CaseEvent.type == EventType.DETECTED
        )
    )


def _newest_verdict(
    session: Session, case_id: uuid.UUID, source: VerdictSource, since: datetime | None
) -> Verdict | None:
    query = select(Verdict).where(Verdict.case_id == case_id, Verdict.source == source)
    if since is not None:
        query = query.where(Verdict.created_at >= since)
    return session.scalar(query.order_by(Verdict.created_at.desc(), Verdict.id.desc()).limit(1))


def _newest_decision(
    session: Session, case_id: uuid.UUID, since: datetime | None
) -> Decision | None:
    query = select(Decision).where(Decision.case_id == case_id)
    if since is not None:
        query = query.where(Decision.created_at >= since)
    return session.scalar(query.order_by(Decision.created_at.desc(), Decision.id.desc()).limit(1))


def view_of(verdict: Verdict | None) -> VerdictView | None:
    if verdict is None:
        return None
    return VerdictView(
        category=verdict.category,
        confidence=float(verdict.confidence),
        collects_credentials_or_payment=verdict.collects_credentials_or_payment,
        copied_elements=tuple(verdict.copied_elements),
    )


def _record(
    session: Session,
    case: CaseRow,
    decision: rules.Decision,
    *,
    actor: str,
    stage: str,
    now: datetime,
) -> None:
    session.add(
        Decision(
            customer_id=case.customer_id,
            case_id=case.id,
            outcome=decision.outcome,
            rule=decision.rule,
            reasons=list(decision.reasons),
            decided_by=actor,
            created_at=now,
        )
    )
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=now,
            type=EventType.DECISION,
            actor=actor,
            data={
                "outcome": decision.outcome.value,
                "rule": decision.rule,
                "reasons": list(decision.reasons),
                "stage": stage,
            },
        )
    )


def _reason(decision: rules.Decision) -> str:
    return " ".join(decision.reasons)[:MAX_REASON]


def _move(
    session: Session,
    case: CaseRow,
    decision: rules.Decision,
    *,
    actor: str,
    now: datetime,
) -> None:
    """The state change of ``decision`` (``confirm`` changes nothing)."""
    outcome = decision.outcome
    if outcome is DecisionOutcome.CONFIRM:
        return
    if outcome is DecisionOutcome.REVIEW:
        case_state.transition(
            session, case, CaseState.IN_REVIEW, actor=actor, reason=_reason(decision), at=now
        )
        return
    if outcome in {DecisionOutcome.WATCH, DecisionOutcome.DISMISS}:
        target = CaseState.WATCHING if outcome is DecisionOutcome.WATCH else CaseState.DISMISSED
        case_state.transition(
            session,
            case,
            target,
            actor=actor,
            reason=_reason(decision),
            data={"rule": decision.rule},
            at=now,
        )
        case.next_check_at = now + (decision.next_check or rules.WATCH_RECHECK)
        return
    _verify(session, case, actor=actor, now=now)
    if outcome is DecisionOutcome.APPROVE:
        case_state.transition(session, case, CaseState.APPROVED, actor=actor, at=now)
        case.approved_by = actor
        case.auto_approved = actor == RULE_ACTOR
        # The first wave of notices, in this transaction (notices/planning.py, K137).
        queue_job(session, PLAN_NOTICES, case_id=case.id, customer_id=case.customer_id)


def _verify(session: Session, case: CaseRow, *, actor: str, now: datetime) -> None:
    """To ``verified``; a phishing clone also alerts the customer, once (K141)."""
    case_state.transition(session, case, CaseState.VERIFIED, actor=actor, at=now)
    if case.category is Category.PHISHING_CLONE and case.customer_alert_sent_at is None:
        queue_job(session, CUSTOMER_ALERT, case_id=case.id, customer_id=case.customer_id)


def decide(database: Database, case_id: uuid.UUID, *, now: datetime) -> Applied | None:
    """Apply the due decision of a ``detected`` case; ``None`` when nothing is due."""
    with database.worker_session() as session:
        case = session.get(CaseRow, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            return None
        since = entered_detected(session, case.id)
        last = _newest_decision(session, case.id, since)
        triage = _newest_verdict(session, case.id, VerdictSource.AI_TRIAGE, since)
        confirm = _newest_verdict(session, case.id, VerdictSource.AI_CONFIRM, since)
        if last is not None and last.outcome is DecisionOutcome.CONFIRM:
            if confirm is None or confirm.created_at < last.created_at:
                return None
            stage, used = Stage.CONFIRM, confirm
        elif triage is not None and (last is None or triage.created_at >= last.created_at):
            stage, used = Stage.TRIAGE, triage
        else:
            return None
        snapshot = build_snapshot(session, case.id, now=now)
        if snapshot is None:
            return None
        thresholds = Thresholds.from_settings(read_settings(session))
        if stage is Stage.CONFIRM:
            decision = rules.decide_after_confirm(
                snapshot, view_of(triage), view_of(confirm), thresholds
            )
        else:
            decision = rules.decide_after_triage(snapshot, view_of(triage), thresholds)
        case.category = used.category
        case.confidence = used.confidence
        _record(session, case, decision, actor=RULE_ACTOR, stage=stage.value, now=now)
        _move(session, case, decision, actor=RULE_ACTOR, now=now)
        state = case.state
    log.info(
        "decision.applied",
        case_id=str(case_id),
        stage=stage.value,
        outcome=decision.outcome.value,
        rule=decision.rule,
        state=state.value,
    )
    return Applied(case_id, stage, decision, state)


# --- the operator -----------------------------------------------------------------------------


class StaffAction(StrEnum):
    APPROVE = "approve"
    VERIFY = "verify"
    REJECT = "reject"
    WATCH = "watch"
    RECAPTURE = "recapture"


class DecisionError(ValueError):
    """The operator's decision cannot be applied; the message is shown on the page."""


BASIS_CATEGORY = {
    NoticeBasis.PHISHING: Category.PHISHING_CLONE,
    NoticeBasis.TRADEMARK: Category.BRAND_IMPERSONATION,
}
THREATS = {Category.PHISHING_CLONE, Category.BRAND_IMPERSONATION}


@dataclass(frozen=True)
class StaffResult:
    case: CaseRow
    action: StaffAction
    label: Category | None  # what "save as a label too" stores; None: no label


def _staff_verdict(
    session: Session, case: CaseRow, category: Category, basis: NoticeBasis, now: datetime
) -> None:
    session.add(
        Verdict(
            customer_id=case.customer_id,
            case_id=case.id,
            source=VerdictSource.STAFF,
            category=category,
            confidence=Decimal("1.000"),
            copied_elements=[],
            collects_credentials_or_payment=category is Category.PHISHING_CLONE,
            evidence=[],
            suggested_basis=basis,
            short_explanation="Operator decision in the review queue.",
            created_at=now,
        )
    )


def staff_decision(
    session: Session,
    case_id: uuid.UUID,
    action: StaffAction,
    *,
    actor: str,
    ip: str | None,
    now: datetime,
    basis: NoticeBasis | None = None,
    category: Category | None = None,
) -> StaffResult:
    """Apply the operator's decision on an ``in_review`` case (§19)."""
    case = session.get(CaseRow, case_id, with_for_update=True)
    if case is None:
        raise DecisionError("no such case")
    if case.state is not CaseState.IN_REVIEW:
        raise DecisionError(f"{case.number} is {case.state.value}, not in review")
    label: Category | None = None
    details: dict[str, Any] = {"action": action.value}
    if action is StaffAction.APPROVE:
        chosen = BASIS_CATEGORY.get(basis) if basis is not None else None
        if basis is None or chosen is None:
            raise DecisionError("approving needs a basis: phishing or trademark")
        label = chosen
        decision = rules.Decision(
            DecisionOutcome.APPROVE, STAFF_RULE, (f"Approved by the operator ({basis.value}).",)
        )
        _staff_verdict(session, case, label, basis, now)
        case.category = label
        details["basis"] = basis.value
    elif action is StaffAction.VERIFY:
        decision = rules.Decision(
            DecisionOutcome.VERIFY, STAFF_RULE, ("Verified by the operator; not approved.",)
        )
        label = case.category if case.category in THREATS else None
    elif action is StaffAction.REJECT:
        if category is None:
            raise DecisionError("rejecting needs a category")
        label = category
        decision = rules.Decision(
            DecisionOutcome.DISMISS,
            STAFF_RULE,
            (f"Rejected by the operator as {category.value.replace('_', ' ')}.",),
            next_check=rules.DISMISS_RECHECK,
        )
        _staff_verdict(session, case, category, NoticeBasis.NONE, now)
        case.category = category
        details["category"] = category.value
    elif action is StaffAction.WATCH:
        decision = rules.Decision(
            DecisionOutcome.WATCH,
            STAFF_RULE,
            ("Put on watch by the operator.",),
            next_check=rules.WATCH_RECHECK,
        )
    else:
        decision = rules.Decision(
            DecisionOutcome.REVIEW, STAFF_RULE, ("The operator asked for a new capture.",)
        )
    _record(session, case, decision, actor=actor, stage="review", now=now)
    if action is StaffAction.RECAPTURE:
        case_state.transition(
            session, case, CaseState.DETECTED, actor=actor, reason="recapture", at=now
        )
    elif action is StaffAction.VERIFY:
        _verify(session, case, actor=actor, now=now)
    else:
        _move(session, case, decision, actor=actor, now=now)
    session.add(
        AuditLog(
            actor=actor,
            action=f"review.{action.value}",
            customer_id=case.customer_id,
            target=case.number,
            data=details,
            ip=ip,
            at=now,
        )
    )
    session.flush()
    return StaffResult(case, action, label)
