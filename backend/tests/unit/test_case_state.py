from __future__ import annotations

import pytest

from kalkan.domain.case_state import (
    TRANSITIONS,
    PanelStatus,
    TransitionError,
    can_transition,
    event_type_for,
    panel_status,
    states_for,
)
from kalkan.domain.enums import CaseState, EventType

S = CaseState

# architecture.md §6, one line per arrow of the state diagram.
ALLOWED = {
    (S.DETECTED, S.WATCHING),
    (S.DETECTED, S.IN_REVIEW),
    (S.DETECTED, S.VERIFIED),
    (S.DETECTED, S.DISMISSED),
    (S.WATCHING, S.DETECTED),
    (S.WATCHING, S.DISMISSED),
    (S.IN_REVIEW, S.DETECTED),  # Recapture (K131)
    (S.IN_REVIEW, S.VERIFIED),
    (S.IN_REVIEW, S.WATCHING),
    (S.IN_REVIEW, S.DISMISSED),
    (S.VERIFIED, S.APPROVED),
    (S.VERIFIED, S.DISMISSED),
    (S.APPROVED, S.NOTIFIED),
    (S.APPROVED, S.DISMISSED),
    (S.NOTIFIED, S.TAKEN_DOWN),
    (S.NOTIFIED, S.DISMISSED),
    (S.TAKEN_DOWN, S.NOTIFIED),
    (S.TAKEN_DOWN, S.ARCHIVED),
    (S.TAKEN_DOWN, S.DISMISSED),
    (S.DISMISSED, S.DETECTED),
}

FORBIDDEN = [
    (S.DETECTED, S.APPROVED),
    (S.DETECTED, S.NOTIFIED),
    (S.DETECTED, S.TAKEN_DOWN),
    (S.WATCHING, S.VERIFIED),
    (S.WATCHING, S.NOTIFIED),
    (S.IN_REVIEW, S.APPROVED),
    (S.IN_REVIEW, S.NOTIFIED),
    (S.VERIFIED, S.NOTIFIED),
    (S.VERIFIED, S.IN_REVIEW),
    (S.APPROVED, S.TAKEN_DOWN),
    (S.NOTIFIED, S.ARCHIVED),
    (S.NOTIFIED, S.APPROVED),
    (S.ARCHIVED, S.NOTIFIED),
    (S.ARCHIVED, S.DETECTED),
    (S.DISMISSED, S.VERIFIED),
    (S.DISMISSED, S.NOTIFIED),
]


def test_transition_table_matches_the_architecture() -> None:
    table = {(source, target) for source, targets in TRANSITIONS.items() for target in targets}
    assert table == ALLOWED
    assert set(TRANSITIONS) == set(CaseState)


@pytest.mark.parametrize(("source", "target"), sorted(ALLOWED))
def test_allowed_transitions(source: CaseState, target: CaseState) -> None:
    assert can_transition(source, target)


@pytest.mark.parametrize(("source", "target"), FORBIDDEN)
def test_forbidden_transitions(source: CaseState, target: CaseState) -> None:
    assert not can_transition(source, target)


def test_no_state_moves_to_itself() -> None:
    assert not any(can_transition(state, state) for state in CaseState)


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (S.DETECTED, S.IN_REVIEW, EventType.REVIEW_REQUESTED),
        (S.DETECTED, S.VERIFIED, EventType.VERIFIED),
        (S.VERIFIED, S.APPROVED, EventType.APPROVED),
        (S.APPROVED, S.NOTIFIED, EventType.NOTICE_SENT),
        (S.TAKEN_DOWN, S.NOTIFIED, EventType.REOPENED),
        (S.NOTIFIED, S.TAKEN_DOWN, EventType.TAKEN_DOWN),
        (S.TAKEN_DOWN, S.ARCHIVED, EventType.ARCHIVED),
        (S.DETECTED, S.WATCHING, EventType.WATCHING),
        (S.WATCHING, S.DETECTED, EventType.DETECTED),
        (S.DETECTED, S.DISMISSED, EventType.DISMISSED),
    ],
)
def test_event_type_for_each_transition(
    source: CaseState, target: CaseState, expected: EventType
) -> None:
    assert event_type_for(source, target) is expected


def test_dismissal_may_be_recorded_as_whitelisted() -> None:
    assert event_type_for(S.NOTIFIED, S.DISMISSED, EventType.WHITELISTED) is EventType.WHITELISTED
    with pytest.raises(TransitionError):
        event_type_for(S.DETECTED, S.VERIFIED, EventType.WHITELISTED)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (S.DETECTED, PanelStatus.REVIEWING),
        (S.IN_REVIEW, PanelStatus.REVIEWING),
        (S.VERIFIED, PanelStatus.ACTIVE),
        (S.APPROVED, PanelStatus.ACTIVE),
        (S.NOTIFIED, PanelStatus.NOTICE_SENT),
        (S.TAKEN_DOWN, PanelStatus.TAKEN_DOWN),
        (S.ARCHIVED, PanelStatus.TAKEN_DOWN),
        (S.WATCHING, PanelStatus.WATCHING),
        (S.DISMISSED, PanelStatus.DISMISSED),
    ],
)
def test_panel_status_mapping(state: CaseState, expected: PanelStatus) -> None:
    """product.md §12: Panel <- system states."""
    assert panel_status(state) is expected


def test_states_for_panel_status() -> None:
    assert states_for(PanelStatus.ACTIVE) == {S.VERIFIED, S.APPROVED}
    assert states_for(PanelStatus.TAKEN_DOWN) == {S.TAKEN_DOWN, S.ARCHIVED}
    assert set().union(*(states_for(status) for status in PanelStatus)) == set(CaseState)
