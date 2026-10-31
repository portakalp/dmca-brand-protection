"""Deciding review cases with the keyboard alone, in Chromium (at most three keys
per case). Chromium: ``uv run playwright install chromium``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from kalkan.db.models import Case, Label
from kalkan.domain.enums import CaseState, Category
from kalkan.ops.web import Services
from tests.integration.test_decision import review_case
from tests.integration.test_ops_keyboard import ops_page, server

__all__ = ["ops_page", "server"]  # the fixtures of the labelling keyboard test


def test_review_decisions_take_at_most_three_keys(
    ops_page: Any, server: str, ops_services: Services
) -> None:
    page = ops_page
    cases = sorted((review_case(ops_services) for _ in range(3)), key=lambda case: case.number)
    page.goto(f"{server}/review")
    assert cases[0].number in page.inner_text("#review-case")

    with page.expect_navigation():
        page.keyboard.press("a")
        page.keyboard.press("p")  # approve on the phishing basis: two keys
    assert cases[1].number in page.inner_text("#review-case")

    page.keyboard.press("a")
    assert "waiting: A" in page.inner_text("#key-pending")
    page.keyboard.press("Escape")  # changed my mind
    with page.expect_navigation():
        page.keyboard.press("r")
        page.keyboard.press("4")  # reject as parked: three keys with the Escape
    assert cases[2].number in page.inner_text("#review-case")

    with page.expect_navigation():
        page.keyboard.press("w")  # watch: one key
    assert "Nothing to review" in page.inner_text("main")

    with ops_services.database.worker_session() as session:
        states = {row.id: row.state for row in session.scalars(select(Case))}
        labels = {row.case_id: row.category for row in session.scalars(select(Label))}
    assert [states[case.id] for case in cases] == [
        CaseState.APPROVED,
        CaseState.DISMISSED,
        CaseState.WATCHING,
    ]
    assert labels == {cases[0].id: Category.PHISHING_CLONE, cases[1].id: Category.PARKED_OR_EMPTY}
