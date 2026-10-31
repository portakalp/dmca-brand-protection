"""The decision rules (architecture.md §14) as a table of scenarios."""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kalkan.decision.rules import (
    Decision,
    Thresholds,
    VerdictView,
    decide_after_confirm,
    decide_after_triage,
)
from kalkan.domain.enums import Category, DecisionOutcome
from kalkan.signals.snapshot import CaseSnapshot

BASE = json.loads((Path(__file__).parents[1] / "fixtures" / "snapshot_v1.json").read_text())
C = Category
D = DecisionOutcome
FORMS = ("form_password", "form_card", "form_iban", "deposit_cta", "external_form_action")
MISSING = object()


def snapshot(
    *,
    official: object = False,
    forms: bool = True,
    visual: float = 0.95,
    favicon: bool = True,
    redirect: bool = False,
    score: int = 90,
    outcome: str = "ok",
) -> CaseSnapshot:
    """The fixture clone with the signals the rules read set as given."""
    data = copy.deepcopy(BASE)
    for scan in data["scans"]:
        if scan["stage"] == "browser":
            scan["outcome"] = outcome
            for name in FORMS:
                scan["signals"][name] = {"value": forms, "detail": {}}
            scan["signals"]["redirects_to_official"] = {"value": redirect, "detail": {}}
        else:
            scan["signals"]["favicon_match"] = {"value": favicon, "detail": {}}
    data["signals"]["visual_similarity"] = {"value": visual, "detail": {}}
    if official is not MISSING:
        data["signals"]["official_infrastructure"] = {"value": official, "detail": {}}
    data["match"]["score"] = score
    return CaseSnapshot.model_validate(data)


def verdict(
    category: Category, confidence: float = 0.95, collects: bool | None = None
) -> VerdictView:
    if collects is None:
        collects = category is C.PHISHING_CLONE
    return VerdictView(category, confidence, collects)


@dataclass(frozen=True)
class Case:
    name: str
    outcome: DecisionOutcome
    rule: str
    snap: dict[str, Any] = field(default_factory=dict)
    triage: VerdictView | None = None
    confirm: VerdictView | None = None
    after_confirm: bool = False
    thresholds: Thresholds = field(default_factory=Thresholds)
    next_check: timedelta | None = None


AUTO_ON = Thresholds(auto_approve_enabled=True)
PHISHING = verdict(C.PHISHING_CLONE)
NO_FORMS = {"forms": False}
LOOKS_OTHER = {"visual": 0.2, "favicon": False}

SCENARIOS = [
    # After triage.
    Case("clone with every signal goes to confirmation", D.CONFIRM, "R1", triage=PHISHING),
    Case("no triage verdict (AI failed)", D.REVIEW, "R2", triage=None),
    Case("challenge page", D.REVIEW, "R2", {"outcome": "challenge"}, PHISHING),
    Case("no successful capture", D.REVIEW, "R2", {"outcome": "error"}, PHISHING),
    Case("official infrastructure true", D.REVIEW, "R2", {"official": True}, PHISHING),
    Case("official infrastructure unknown (null)", D.REVIEW, "R2", {"official": None}, PHISHING),
    Case("official infrastructure not computed", D.REVIEW, "R2", {"official": MISSING}, PHISHING),
    Case("phishing with low confidence", D.REVIEW, "R2", triage=verdict(C.PHISHING_CLONE, 0.8)),
    Case(
        "phishing without any form",
        D.REVIEW,
        "R2",
        NO_FORMS,
        verdict(C.PHISHING_CLONE, collects=False),
    ),
    Case("model says it collects, code found no form", D.REVIEW, "R2", NO_FORMS, PHISHING),
    Case("phishing that does not look like the site", D.REVIEW, "R2", LOOKS_OTHER, PHISHING),
    Case("favicon match is enough similarity", D.CONFIRM, "R1", {"visual": 0.2}, PHISHING),
    Case(
        "phishing that redirects to the official site (R5)",
        D.WATCH,
        "R5",
        {"redirect": True},
        PHISHING,
        next_check=timedelta(days=1),
    ),
    Case(
        "redirect with a low match score is not R5",
        D.WATCH,
        "R3",
        {"redirect": True, "score": 70, **NO_FORMS},
        verdict(C.PARKED_OR_EMPTY),
        next_check=timedelta(days=7),
    ),
    Case(
        "R5 comes before R4",
        D.WATCH,
        "R5",
        {"redirect": True, **NO_FORMS, **LOOKS_OTHER},
        verdict(C.AFFILIATE_OR_REVIEW),
    ),
    Case(
        "parked page",
        D.WATCH,
        "R3",
        {**NO_FORMS, **LOOKS_OTHER},
        verdict(C.PARKED_OR_EMPTY),
        next_check=timedelta(days=7),
    ),
    Case("parked page with a form", D.REVIEW, "R2", LOOKS_OTHER, verdict(C.PARKED_OR_EMPTY)),
    Case(
        "affiliate above the threshold",
        D.DISMISS,
        "R4",
        {**NO_FORMS, **LOOKS_OTHER},
        verdict(C.AFFILIATE_OR_REVIEW, 0.9),
        next_check=timedelta(days=30),
    ),
    Case(
        "affiliate exactly at the threshold",
        D.DISMISS,
        "R4",
        {**NO_FORMS, **LOOKS_OTHER},
        verdict(C.AFFILIATE_OR_REVIEW, 0.8),
    ),
    Case(
        "affiliate below the threshold",
        D.REVIEW,
        "R2",
        {**NO_FORMS, **LOOKS_OTHER},
        verdict(C.AFFILIATE_OR_REVIEW, 0.7),
    ),
    Case(
        "affiliate with a login form",
        D.REVIEW,
        "R2",
        LOOKS_OTHER,
        verdict(C.AFFILIATE_OR_REVIEW, 0.95),
    ),
    Case(
        "not a threat says the model, form and similarity say the code",
        D.REVIEW,
        "R2",
        {},
        verdict(C.UNRELATED, 0.95),
    ),
    Case(
        "unrelated page",
        D.DISMISS,
        "R4",
        {**NO_FORMS, **LOOKS_OTHER},
        verdict(C.UNRELATED, 0.95),
    ),
    Case("brand impersonation", D.REVIEW, "R2", NO_FORMS, verdict(C.BRAND_IMPERSONATION, 0.99)),
    Case("unclear", D.REVIEW, "R2", triage=verdict(C.UNCLEAR, 0.99)),
    Case(
        "a stricter triage threshold from settings",
        D.REVIEW,
        "R2",
        triage=verdict(C.PHISHING_CLONE, 0.94),
        thresholds=Thresholds(triage_min_confidence=0.95),
    ),
    # After confirmation.
    Case(
        "both models agree, auto-approve on",
        D.APPROVE,
        "C1",
        triage=PHISHING,
        confirm=PHISHING,
        after_confirm=True,
        thresholds=AUTO_ON,
    ),
    Case(
        "both models agree, auto-approve off",
        D.REVIEW,
        "C2",
        triage=PHISHING,
        confirm=PHISHING,
        after_confirm=True,
    ),
    Case(
        "models disagree",
        D.REVIEW,
        "C3",
        triage=PHISHING,
        confirm=verdict(C.BRAND_IMPERSONATION),
        after_confirm=True,
        thresholds=AUTO_ON,
    ),
    Case(
        "confirmation with low confidence",
        D.REVIEW,
        "C3",
        triage=PHISHING,
        confirm=verdict(C.PHISHING_CLONE, 0.85),
        after_confirm=True,
        thresholds=AUTO_ON,
    ),
    Case(
        "no confirmation verdict (AI failed)",
        D.REVIEW,
        "C3",
        triage=PHISHING,
        after_confirm=True,
        thresholds=AUTO_ON,
    ),
    Case(
        "confirmation conflicts with the code",
        D.REVIEW,
        "C3",
        NO_FORMS,
        triage=PHISHING,
        confirm=PHISHING,
        after_confirm=True,
        thresholds=AUTO_ON,
    ),
]


def _decide(case: Case) -> Decision:
    snap = snapshot(**case.snap)
    if case.after_confirm:
        return decide_after_confirm(snap, case.triage, case.confirm, case.thresholds)
    return decide_after_triage(snap, case.triage, case.thresholds)


def test_there_are_at_least_25_scenarios() -> None:
    assert len(SCENARIOS) >= 25
    assert len({case.name for case in SCENARIOS}) == len(SCENARIOS)


@pytest.mark.parametrize("case", SCENARIOS, ids=[case.name for case in SCENARIOS])
def test_scenario(case: Case) -> None:
    decision = _decide(case)
    assert (decision.outcome, decision.rule) == (case.outcome, case.rule), decision.reasons
    if case.next_check is not None:
        assert decision.next_check == case.next_check
    assert decision.reasons
    for reason in decision.reasons:
        assert reason[0].isupper()
        assert reason.endswith(".")
        assert len(reason) < 200


def test_nothing_is_approved_while_auto_approve_is_off() -> None:
    off = Thresholds(auto_approve_enabled=False)
    categories = list(Category)
    options: list[dict[str, Any]] = [
        {},
        NO_FORMS,
        LOOKS_OTHER,
        {"official": None},
        {"redirect": True},
    ]
    for snap, triage, confirm, confidence in itertools.product(
        options, categories, categories, (0.5, 0.95, 1.0)
    ):
        snapped = snapshot(**snap)
        first = verdict(triage, confidence)
        second = verdict(confirm, confidence)
        assert decide_after_triage(snapped, first, off).outcome is not D.APPROVE
        assert decide_after_confirm(snapped, first, second, off).outcome is not D.APPROVE


def test_thresholds_come_from_the_settings_values() -> None:
    values = {
        "auto_approve_enabled": True,
        "triage_min_confidence": 0.5,
        "confirm_min_confidence": 0.6,
        "visual_similarity_min": 0.7,
        "dismiss_min_confidence": 0.8,
    }
    assert Thresholds.from_settings(values) == Thresholds(True, 0.5, 0.6, 0.7, 0.8)
    # Only a real true switches automatic approval on.
    assert not Thresholds.from_settings(
        {**values, "auto_approve_enabled": "true"}
    ).auto_approve_enabled
