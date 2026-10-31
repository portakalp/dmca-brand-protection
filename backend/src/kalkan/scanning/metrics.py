"""Browser stage metrics (architecture.md §23), exported by worker-browser.

Created on first use, like the discovery metrics: Dramatiq's Prometheus middleware switches
prometheus_client to multiprocess mode when the worker process boots, so they must not be
created at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any


@dataclass(frozen=True)
class CaptureMetrics:
    captures: Any
    proxy_bytes: Any
    deferrals: Any


@cache
def metrics() -> CaptureMetrics:
    import prometheus_client as prom

    return CaptureMetrics(
        captures=prom.Counter(
            "captures", "Browser captures per profile and attempt, by outcome.", ["outcome"]
        ),
        proxy_bytes=prom.Counter(
            "proxy_bytes", "Approximate bytes sent and received through the capture proxy."
        ),
        # The alarms "proxy configuration missing" and "daily proxy limit" (§23) read this.
        deferrals=prom.Counter(
            "capture_deferrals",
            "Capture jobs put off without using a retry (no_proxy, proxy_daily_limit).",
            ["reason"],
        ),
    )
