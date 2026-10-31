"""A case that would go to the AI when AI is off: straight to the review queue (§13, §14).

Self-hosted setups may run without Anthropic (``AI_ENABLED=false``). The rules need a triage
verdict before anything is automatic (R2: "no AI triage verdict" → review), so without AI the
only safe outcome is the operator's review: the case goes to ``in_review`` with the reason
``ai_disabled``, the same way an unavailable AI sends it there (``ai_unavailable``,
``kalkan.ai.assess``). Nothing is approved, dismissed or sent here.

A redelivered or stale message does nothing: the case must still be ``detected`` and the
stage still due since it entered the state (triage: browser scan signals; confirmation: a
``confirm`` decision), so a case the operator sent back for a new capture is not taken to
review before its new capture is in.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.db.models import Case, Decision, Signal
from kalkan.db.session import Database
from kalkan.decision.apply import Stage, entered_detected
from kalkan.domain import case_state
from kalkan.domain.enums import CaseState, DecisionOutcome
from kalkan.logging import get_logger

AI_DISABLED = "ai_disabled"
SYSTEM = "system"
# compute_signals writes it for every browser scan it reads (as in ai/assess.py).
SCAN_SIGNAL = "form_password"

log = get_logger(__name__)


def _due(session: Session, case: Case, stage: Stage) -> bool:
    since = entered_detected(session, case.id)
    if stage is Stage.TRIAGE:
        query = select(func.max(Signal.created_at)).where(
            Signal.case_id == case.id, Signal.name == SCAN_SIGNAL
        )
        signals_at = session.scalar(query)
        return signals_at is not None and (since is None or signals_at >= since)
    query_decision = select(Decision.outcome).where(Decision.case_id == case.id)
    if since is not None:
        query_decision = query_decision.where(Decision.created_at >= since)
    newest = session.scalar(
        query_decision.order_by(Decision.created_at.desc(), Decision.id.desc()).limit(1)
    )
    return newest is DecisionOutcome.CONFIRM


def review_without_ai(
    database: Database, case_id: uuid.UUID, stage: Stage, *, now: datetime
) -> bool:
    """Move a ``detected`` case whose ``stage`` is due to ``in_review``; True when it moved."""
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED or not _due(session, case, stage):
            log.info("decision.without_ai_skipped", case_id=str(case_id), stage=stage.value)
            return False
        case_state.transition(
            session,
            case,
            CaseState.IN_REVIEW,
            actor=SYSTEM,
            reason=AI_DISABLED,
            data={"stage": stage.value},
            at=now,
        )
    log.info("decision.without_ai", case_id=str(case_id), stage=stage.value)
    return True
