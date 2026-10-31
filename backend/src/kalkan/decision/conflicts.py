"""Where the model and the code signals disagree (architecture.md §14 R2).

Every finding is one short English sentence for the review screen. A conflict sends the case to
review whatever the rules would otherwise do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kalkan.domain.enums import Category
from kalkan.signals.snapshot import CaseSnapshot

if TYPE_CHECKING:
    from kalkan.decision.rules import VerdictView

# A form that asks for credentials or money, as the code classified it.
FORM_SIGNALS = ("form_password", "form_card", "form_iban")
NOT_A_THREAT = {Category.AFFILIATE_OR_REVIEW, Category.UNRELATED, Category.PARKED_OR_EMPTY}


def collecting_form(snapshot: CaseSnapshot) -> bool:
    return any(snapshot.flag(name) for name in (*FORM_SIGNALS, "deposit_cta"))


def _similarity(snapshot: CaseSnapshot) -> float | None:
    value = snapshot.value("visual_similarity")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def conflicts(
    snapshot: CaseSnapshot, verdict: VerdictView, *, visual_similarity_min: float
) -> list[str]:
    found: list[str] = []
    forms = collecting_form(snapshot)
    if verdict.collects_credentials_or_payment and not forms:
        found.append(
            "The model says the page collects credentials or payment, "
            "but the code found no such form."
        )
    similarity = _similarity(snapshot)
    looks_official = (similarity is not None and similarity >= visual_similarity_min) or bool(
        snapshot.flag("favicon_match")
    )
    if verdict.category in NOT_A_THREAT and forms and looks_official:
        shown = verdict.category.value.replace("_", " ")
        found.append(
            f"The model says {shown}, but the code found a login or payment form "
            "on a page that looks like the official site."
        )
    elif verdict.category is Category.PARKED_OR_EMPTY and forms:
        found.append("The model says the page is parked or empty, but the code found a form.")
    return found
