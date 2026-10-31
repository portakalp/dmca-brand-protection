"""Discovery metrics (architecture.md §23).

The metrics are created on first use. In worker processes that is after Dramatiq's
Prometheus middleware has switched prometheus_client to multiprocess mode (it sets
``PROMETHEUS_MULTIPROC_DIR`` at process boot), so importing prometheus_client at module
level here would make these metrics invisible to the worker's exposition server.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any


@dataclass(frozen=True)
class DiscoveryMetrics:
    ct_events: Any
    matches: Any
    candidates: Any
    matcher_seconds: Any
    czds_zone_seconds: Any
    czds_zone_names: Any
    permutation_checks: Any


@cache
def metrics() -> DiscoveryMetrics:
    import prometheus_client as prom

    return DiscoveryMetrics(
        ct_events=prom.Counter("ct_events", "Certificate updates read from the CT stream."),
        matches=prom.Counter(
            "matches", "New domain-brand matches, once per reason.", ["source", "reason"]
        ),
        candidates=prom.Counter("candidates", "New domain-brand matches.", ["source"]),
        matcher_seconds=prom.Histogram(
            "matcher_seconds",
            "Time to normalize and match one host.",
            buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05),
        ),
        czds_zone_seconds=prom.Gauge(
            "czds_zone_seconds",
            "Duration of the last scan of a TLD zone.",
            ["tld"],
            multiprocess_mode="mostrecent",
        ),
        czds_zone_names=prom.Gauge(
            "czds_zone_names",
            "Delegated names read in the last scan of a TLD zone.",
            ["tld"],
            multiprocess_mode="mostrecent",
        ),
        permutation_checks=prom.Counter(
            "permutation_dns_checks", "DNS checks of brand permutations.", ["result"]
        ),
    )
