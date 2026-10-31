"""Worker processes and the queues they own (architecture.md §3, §7; K30).

Every queue is consumed by exactly one worker process. A process only declares the actors
of its own modules, so a message on a queue another process consumes would fail there with
``ActorNotFound``. Add new queues and actor modules here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSpec:
    service: str
    entry_module: str
    processes: int
    threads: int
    queues: tuple[str, ...]
    actor_modules: tuple[str, ...]


WORKERS: dict[str, WorkerSpec] = {
    "default": WorkerSpec(
        service="worker-default",
        entry_module="kalkan.workers.default",
        processes=1,
        threads=8,
        queues=(
            "default",
            "discovery",
            "enrich",
            "precheck",
            "persist",
            "signals",
            "decision",
            "notify",
            "followup",
            "webhooks",
            "reports",
        ),
        actor_modules=(
            "kalkan.workers.ping",
            "kalkan.workers.discovery",
            "kalkan.workers.enrichment",
            "kalkan.workers.scanning",
            "kalkan.workers.persist",
            "kalkan.workers.signals",
            "kalkan.workers.decision",
            "kalkan.workers.notify",
            "kalkan.workers.followup",
            "kalkan.workers.reports",
            # Stand-ins for the AI actors when AI is off (kalkan.ai.queue.WITHOUT_AI).
            "kalkan.workers.without_ai",
        ),
    ),
    # Playwright's sync API is bound to its thread: one thread per process (§10).
    # worker-default and the scheduler send to browser_capture by name
    # (kalkan.scanning.capture_jobs) and never declare this actor; worker-default sends
    # print_report the same way (kalkan.reports.handoff).
    "browser": WorkerSpec(
        service="worker-browser",
        entry_module="kalkan.workers.browser",
        processes=2,
        threads=1,
        queues=("capture",),
        actor_modules=("kalkan.workers.capture", "kalkan.workers.report_print"),
    ),
    # Claude calls (§13). Four threads: the client's concurrency limit per process.
    # worker-default and the scheduler send ai_triage and ai_confirm by name (kalkan.ai.queue);
    # with AI off they send worker-default's stand-ins instead and this worker is not needed.
    "ai": WorkerSpec(
        service="worker-ai",
        entry_module="kalkan.workers.ai",
        processes=1,
        threads=4,
        queues=("ai",),
        actor_modules=(
            "kalkan.workers.assess",
            "kalkan.workers.notice_text",
            "kalkan.workers.classify_reply",
        ),
    ),
}


def dramatiq_args(spec: WorkerSpec) -> list[str]:
    """Command-line arguments for ``dramatiq`` that start ``spec``."""
    return [
        spec.entry_module,
        "--processes",
        str(spec.processes),
        "--threads",
        str(spec.threads),
        "--queues",
        *spec.queues,
        # Logging is configured by kalkan.logging (JSON) in every process.
        "--skip-logging",
    ]
