"""The review queue (architecture.md §14, §19).

The queue holds the ``in_review`` cases with an open review task, demo cases excepted, in
case-number order; N moves the cursor past a case for this pass. The view shows the desktop
and mobile screenshots next to the official references, the signals, the triage and
confirmation verdicts with their evidence, the newest rule decision (the suggestion) and the
review reason. Decisions are applied by ``decision.apply.staff_decision``.

Choices (``static/ops.js`` reads ``data-keys``): ``A P`` approve on the phishing basis, ``A T``
on the trademark basis, ``V`` verify only, ``R 1``-``R 6`` reject with a category, ``W`` watch,
``C`` recapture; at most two keys.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from kalkan.db.models import Brand, Case, Customer, Decision, Domain, ReviewTask, Verdict
from kalkan.decision.apply import StaffAction
from kalkan.domain.enums import (
    CaseState,
    Category,
    NoticeBasis,
    Profile,
    TaskKind,
    TaskStatus,
    VerdictSource,
)
from kalkan.ops import labeling

SHOWN_SIGNALS = (*labeling.SHOWN_SIGNALS, "official_infrastructure", "webrisk_listed")


def _name(category: Category) -> str:
    return category.value.replace("_", " ")


@dataclass(frozen=True)
class Choice:
    keys: str  # the shortcut sequence, lower case
    value: str  # the form value
    text: str


CHOICES: tuple[Choice, ...] = (
    Choice("ap", "approve:phishing", "Approve · phishing"),
    Choice("at", "approve:trademark", "Approve · trademark"),
    Choice("v", "verify", "Verify only"),
    *(
        Choice(f"r{key}", f"reject:{category.value}", f"Reject · {_name(category)}")
        for key, category in labeling.KEYS.items()
    ),
    Choice("w", "watch", "Watch"),
    Choice("c", "recapture", "Recapture"),
)


class ChoiceError(ValueError):
    pass


@dataclass(frozen=True)
class Parsed:
    action: StaffAction
    basis: NoticeBasis | None = None
    category: Category | None = None


def parse_choice(value: str) -> Parsed:
    name, _, argument = value.partition(":")
    try:
        action = StaffAction(name)
        if action is StaffAction.APPROVE:
            return Parsed(action, basis=NoticeBasis(argument))
        if action is StaffAction.REJECT:
            return Parsed(action, category=Category(argument))
    except ValueError:
        raise ChoiceError(f"unknown choice {value!r}") from None
    if argument:
        raise ChoiceError(f"unknown choice {value!r}")
    return Parsed(action)


def _queue() -> list[ColumnElement[bool]]:
    open_task = (
        select(ReviewTask.id)
        .where(
            ReviewTask.case_id == Case.id,
            ReviewTask.kind == TaskKind.REVIEW,
            ReviewTask.status == TaskStatus.OPEN,
        )
        .exists()
    )
    return [Customer.is_demo.is_(False), Case.state == CaseState.IN_REVIEW, open_task]


def next_case(session: Session, after: str | None = None) -> Case | None:
    query = select(Case).join(Customer, Customer.id == Case.customer_id).where(*_queue())
    if after:
        query = query.where(Case.number > after)
    return session.scalar(query.order_by(Case.number).limit(1))


def waiting(session: Session) -> int:
    query = (
        select(func.count(Case.id)).join(Customer, Customer.id == Case.customer_id).where(*_queue())
    )
    return int(session.scalar(query) or 0)


@dataclass
class ReviewView:
    case: Case
    domain: str
    brand: str
    customer: str
    reason: str | None
    shots: dict[Profile, labeling.Shot] = field(default_factory=dict)
    references: dict[Profile, uuid.UUID] = field(default_factory=dict)
    signals: list[tuple[str, str | None, object]] = field(default_factory=list)
    triage: Verdict | None = None
    confirm: Verdict | None = None
    suggestion: Decision | None = None


def _newest(session: Session, case_id: uuid.UUID, source: VerdictSource) -> Verdict | None:
    return session.scalar(
        select(Verdict)
        .where(Verdict.case_id == case_id, Verdict.source == source)
        .order_by(Verdict.created_at.desc())
        .limit(1)
    )


def view(session: Session, case: Case) -> ReviewView:
    domain = session.get(Domain, case.domain_id)
    brand = session.get(Brand, case.brand_id)
    customer = session.get(Customer, case.customer_id)
    task = session.scalar(
        select(ReviewTask).where(
            ReviewTask.case_id == case.id,
            ReviewTask.kind == TaskKind.REVIEW,
            ReviewTask.status == TaskStatus.OPEN,
        )
    )
    return ReviewView(
        case=case,
        domain=domain.name if domain else "?",
        brand=brand.name if brand else "?",
        customer=customer.name if customer else "?",
        reason=task.reason if task else None,
        shots=labeling.screenshots(session, case.id),
        references=labeling.references(session, case.brand_id),
        signals=labeling.case_signals(session, case.id, SHOWN_SIGNALS),
        triage=_newest(session, case.id, VerdictSource.AI_TRIAGE),
        confirm=_newest(session, case.id, VerdictSource.AI_CONFIRM),
        suggestion=session.scalar(
            select(Decision)
            .where(Decision.case_id == case.id, Decision.decided_by == "rule")
            .order_by(Decision.created_at.desc())
            .limit(1)
        ),
    )
