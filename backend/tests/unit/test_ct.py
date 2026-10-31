"""CT consumer pieces that need no services (architecture.md §8)."""

from __future__ import annotations

import pytest

from kalkan.discovery.ct import Backoff


@pytest.mark.parametrize(("attempt", "base"), [(0, 1), (1, 2), (3, 8), (6, 60), (20, 60)])
def test_backoff_grows_exponentially_up_to_a_cap(attempt: int, base: float) -> None:
    backoff = Backoff(initial=1, maximum=60, jitter=0.2)
    assert backoff.delay(attempt, rand=lambda: 0.5) == pytest.approx(base)
    assert backoff.delay(attempt, rand=lambda: 0.0) == pytest.approx(base * 0.8)
    assert backoff.delay(attempt, rand=lambda: 1.0) == pytest.approx(base * 1.2)


def test_backoff_survives_a_very_long_outage() -> None:
    assert Backoff(initial=1, maximum=60, jitter=0).delay(5_000) == 60
