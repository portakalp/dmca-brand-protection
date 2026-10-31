"""Decision rules (architecture.md §14). Pure functions: snapshot, verdicts and settings in,
:class:`Decision` out. The ``decide`` actor (``decision/apply.py``) applies the result.

After triage (:func:`decide_after_triage`), in this order:

- review first: no triage verdict (the AI failed), a browser capture that met a challenge or
  no ``ok`` browser capture, official infrastructure (true or unknown: null counts as true,
  K64), a conflict between the model and the code (``decision/conflicts.py``);
- R1 automatic candidate → ``confirm`` (the confirmation model is asked);
- R5 redirect to the official site with a match score of at least 80 → ``watch`` (+1 day,
  K57); before R4;
- R3 parked or empty → ``watch`` (+7 days);
- R4 affiliate or unrelated with enough confidence and no credential or payment form →
  ``dismiss`` (+30 days);
- R2 everything else → ``review``.

After confirmation (:func:`decide_after_confirm`): both models say phishing clone with enough
confidence → ``approve`` (C1) when ``auto_approve_enabled`` is on, otherwise ``review`` (C2,
"auto-approve disabled"); anything else → ``review`` (C3, "models disagree").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kalkan.decision.conflicts import collecting_form, conflicts
from kalkan.domain.enums import Category, DecisionOutcome, ScanStage
from kalkan.scanning.capture_contract import Outcome
from kalkan.signals.snapshot import CaseSnapshot

# Code signals of a form that collects credentials or money (§14 R1).
COLLECTING_SIGNALS = (
    "form_password",
    "form_card",
    "form_iban",
    "deposit_cta",
    "external_form_action",
)
REDIRECT_MIN_SCORE = 80
WATCH_RECHECK = timedelta(days=7)
REDIRECT_RECHECK = timedelta(days=1)
DISMISS_RECHECK = timedelta(days=30)


@dataclass(frozen=True)
class VerdictView:
    """What the rules read of a verdict (a ``verdicts`` row or a model answer)."""

    category: Category
    confidence: float
    collects_credentials_or_payment: bool
    copied_elements: tuple[str, ...] = ()


@dataclass(frozen=True)
class Thresholds:
    auto_approve_enabled: bool = False
    triage_min_confidence: float = 0.90
    confirm_min_confidence: float = 0.90
    visual_similarity_min: float = 0.85
    dismiss_min_confidence: float = 0.80

    @classmethod
    def from_settings(cls, values: Mapping[str, Any]) -> Thresholds:
        return cls(
            auto_approve_enabled=values["auto_approve_enabled"] is True,
            triage_min_confidence=float(values["triage_min_confidence"]),
            confirm_min_confidence=float(values["confirm_min_confidence"]),
            visual_similarity_min=float(values["visual_similarity_min"]),
            dismiss_min_confidence=float(values["dismiss_min_confidence"]),
        )


@dataclass(frozen=True)
class Decision:
    outcome: DecisionOutcome
    rule: str
    reasons: tuple[str, ...]
    next_check: timedelta | None = None  # watch and dismiss


def _review(rule: str, *reasons: str) -> Decision:
    return Decision(DecisionOutcome.REVIEW, rule, tuple(reasons))


def _similar(snapshot: CaseSnapshot, minimum: float) -> bool:
    value = snapshot.value("visual_similarity")
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= minimum


def r1_auto_candidate(
    snapshot: CaseSnapshot,
    category: Category,
    confidence: float,
    *,
    triage_min_confidence: float,
    visual_similarity_min: float,
) -> bool:
    """R1: triage says ``phishing_clone`` with enough confidence, the code found a collecting
    form and a visual or favicon match, nothing points at the official site, and the official
    infrastructure check ran and found none (false, not missing; K64)."""
    if category is not Category.PHISHING_CLONE or confidence < triage_min_confidence:
        return False
    if not any(snapshot.flag(name) for name in COLLECTING_SIGNALS):
        return False
    if not (_similar(snapshot, visual_similarity_min) or snapshot.flag("favicon_match")):
        return False
    if snapshot.flag("redirects_to_official"):
        return False
    return snapshot.value("official_infrastructure") is False


def _capture_problem(snapshot: CaseSnapshot) -> str | None:
    browser = [scan for scan in snapshot.scans if scan.stage is ScanStage.BROWSER]
    if any(scan.outcome == Outcome.CHALLENGE.value for scan in browser):
        return "A browser capture met a challenge page; challenges are never solved."
    if not any(scan.outcome == Outcome.OK.value for scan in browser):
        return "There is no successful browser capture."
    return None


def _name(category: Category) -> str:
    return category.value.replace("_", " ")


def decide_after_triage(
    snapshot: CaseSnapshot, triage: VerdictView | None, thresholds: Thresholds
) -> Decision:
    if triage is None:
        return _review("R2", "There is no AI triage verdict.")
    problem = _capture_problem(snapshot)
    if problem is not None:
        return _review("R2", problem)
    official = snapshot.value("official_infrastructure")
    if official is not False:
        shares = "shares" if official is True else "may share (the check did not finish)"
        return _review(
            "R2",
            f"The domain {shares} IP addresses or name servers with an official domain; "
            "it may be the customer's own new domain.",
        )
    found = conflicts(snapshot, triage, visual_similarity_min=thresholds.visual_similarity_min)
    if found:
        return _review("R2", *found)
    if r1_auto_candidate(
        snapshot,
        triage.category,
        triage.confidence,
        triage_min_confidence=thresholds.triage_min_confidence,
        visual_similarity_min=thresholds.visual_similarity_min,
    ):
        return Decision(
            DecisionOutcome.CONFIRM,
            "R1",
            (
                f"Triage says phishing clone with confidence {triage.confidence:.2f}.",
                "The code found a credential or payment form and a visual or favicon match.",
            ),
        )
    score = snapshot.match.score if snapshot.match is not None else 0
    if snapshot.flag("redirects_to_official") and score >= REDIRECT_MIN_SCORE:
        return Decision(
            DecisionOutcome.WATCH,
            "R5",
            (
                f"The domain redirects to the official site (match score {score}).",
                "It is watched and captured again daily for 7 days.",
            ),
            next_check=REDIRECT_RECHECK,
        )
    if triage.category is Category.PARKED_OR_EMPTY:
        return Decision(
            DecisionOutcome.WATCH,
            "R3",
            ("Triage says the page is parked or empty.",),
            next_check=WATCH_RECHECK,
        )
    if triage.category in {Category.AFFILIATE_OR_REVIEW, Category.UNRELATED}:
        name = _name(triage.category)
        if triage.confidence < thresholds.dismiss_min_confidence:
            return _review(
                "R2",
                f"Triage says {name}, but its confidence {triage.confidence:.2f} is below "
                f"{thresholds.dismiss_min_confidence:.2f}.",
            )
        if collecting_form(snapshot) or triage.collects_credentials_or_payment:
            return _review("R2", f"Triage says {name}, but the page has a login or payment form.")
        return Decision(
            DecisionOutcome.DISMISS,
            "R4",
            (f"Triage says {name} with confidence {triage.confidence:.2f}.",),
            next_check=DISMISS_RECHECK,
        )
    if triage.category is Category.PHISHING_CLONE:
        return _review(
            "R2",
            f"Triage says phishing clone with confidence {triage.confidence:.2f}, "
            "but not every automatic confirmation condition is met.",
        )
    if triage.category is Category.BRAND_IMPERSONATION:
        return _review("R2", "Triage says brand impersonation; an operator decides.")
    return _review("R2", "Triage could not decide (unclear).")


def decide_after_confirm(
    snapshot: CaseSnapshot,
    triage: VerdictView | None,
    confirm: VerdictView | None,
    thresholds: Thresholds,
) -> Decision:
    if confirm is None:
        return _review("C3", "There is no AI confirmation verdict.")
    found = conflicts(snapshot, confirm, visual_similarity_min=thresholds.visual_similarity_min)
    if found:
        return _review("C3", *found)
    agree = (
        triage is not None
        and triage.category is Category.PHISHING_CLONE
        and confirm.category is Category.PHISHING_CLONE
        and confirm.confidence >= thresholds.confirm_min_confidence
    )
    if not agree:
        return _review(
            "C3",
            f"Models disagree: the confirmation says {_name(confirm.category)} with confidence "
            f"{confirm.confidence:.2f}.",
        )
    if not thresholds.auto_approve_enabled:
        return _review(
            "C2",
            "Auto-approve disabled: both models say phishing clone; approve to send notices.",
        )
    return Decision(
        DecisionOutcome.APPROVE,
        "C1",
        (
            f"Both models say phishing clone (confirmation confidence {confirm.confidence:.2f}).",
            "The code found a credential or payment form and a visual or favicon match.",
        ),
    )
