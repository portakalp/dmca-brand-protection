"""AI metrics (architecture.md §23), exported by worker-ai.

Created on first use, like the other worker metrics: Dramatiq's Prometheus middleware switches
prometheus_client to multiprocess mode when the worker process boots.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

_lock = threading.Lock()
_metrics: AiMetrics | None = None


@dataclass(frozen=True)
class AiMetrics:
    cost: Any
    budget_stops: Any


def metrics() -> AiMetrics:
    """Created once per process; worker-ai calls Claude from several threads."""
    global _metrics
    with _lock:
        if _metrics is None:
            _metrics = _create()
        return _metrics


def _create() -> AiMetrics:
    import prometheus_client as prom

    return AiMetrics(
        # The alarm "daily AI spend above the threshold" (§23) reads this.
        cost=prom.Counter("ai_cost_usd", "Cost of Claude calls in USD.", ["model", "purpose"]),
        budget_stops=prom.Counter(
            "ai_budget_stops", "Cases sent to review because the monthly AI budget was used up."
        ),
    )
