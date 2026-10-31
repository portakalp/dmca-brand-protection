from __future__ import annotations

import importlib

import dramatiq
import pytest
from dramatiq.cli import make_argument_parser

from kalkan.workers.ping import ping
from kalkan.workers.specs import WORKERS, dramatiq_args


def _import_all_actor_modules() -> None:
    for spec in WORKERS.values():
        for module in spec.actor_modules:
            importlib.import_module(module)


def test_every_queue_belongs_to_exactly_one_worker_process() -> None:
    owners: dict[str, list[str]] = {}
    for name, spec in WORKERS.items():
        for queue in spec.queues:
            owners.setdefault(queue, []).append(name)
    assert {queue: names for queue, names in owners.items() if len(names) > 1} == {}


def test_every_actor_runs_in_the_process_that_owns_its_queue() -> None:
    _import_all_actor_modules()
    broker = dramatiq.get_broker()
    for actor_name in broker.get_declared_actors():
        actor = broker.get_actor(actor_name)
        owners = [spec for spec in WORKERS.values() if actor.queue_name in spec.queues]
        assert len(owners) == 1, f"{actor_name}: queue {actor.queue_name} has no single owner"
        assert actor.fn.__module__ in owners[0].actor_modules, actor_name


DEFAULT_QUEUES = [
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
]


def test_dramatiq_args_for_worker_default() -> None:
    args = dramatiq_args(WORKERS["default"])
    assert args[0] == "kalkan.workers.default"
    assert args[args.index("--processes") + 1] == "1"
    assert args[args.index("--threads") + 1] == "8"
    assert args[args.index("--queues") + 1 :][: len(WORKERS["default"].queues)] == DEFAULT_QUEUES
    assert "--skip-logging" in args


def test_dramatiq_accepts_the_generated_arguments() -> None:
    parsed = make_argument_parser().parse_args(dramatiq_args(WORKERS["default"]))
    assert parsed.broker == "kalkan.workers.default"
    assert parsed.queues == DEFAULT_QUEUES
    assert parsed.processes == 1
    assert parsed.threads == 8
    assert parsed.skip_logging is True


def test_worker_browser_owns_only_the_capture_queue() -> None:
    # Playwright's sync API is bound to its thread: 2 processes x 1 thread (§3, §10).
    spec = WORKERS["browser"]
    assert spec.service == "worker-browser"
    parsed = make_argument_parser().parse_args(dramatiq_args(spec))
    assert parsed.broker == "kalkan.workers.browser"
    assert parsed.queues == ["capture"]
    assert (parsed.processes, parsed.threads) == (2, 1)
    assert spec.actor_modules == ("kalkan.workers.capture", "kalkan.workers.report_print")
    # worker-default sends to browser_capture and print_report by name and never declares
    # them (K30).
    assert "kalkan.workers.capture" not in WORKERS["default"].actor_modules
    assert "kalkan.workers.report_print" not in WORKERS["default"].actor_modules


def test_report_actors_use_queues_the_browser_may_write() -> None:
    # The browser's Redis user writes only capture and persist (K82).
    from kalkan.workers.report_print import print_report
    from kalkan.workers.reports import build_report, persist_report, report_print_failed

    assert (print_report.actor_name, print_report.queue_name) == ("print_report", "capture")
    assert print_report.options["max_retries"] == 0
    assert persist_report.queue_name == "persist"
    assert report_print_failed.queue_name == "persist"
    assert build_report.queue_name == "reports"


def test_capture_hand_off_actor_options() -> None:
    from kalkan.workers.persist import clean_capture_folders, persist_capture
    from kalkan.workers.signals import compute_signals

    assert (persist_capture.actor_name, persist_capture.queue_name) == (
        "persist_capture",
        "persist",
    )
    assert persist_capture.options["max_retries"] == 5
    assert persist_capture.options["time_limit"] == 120_000
    assert clean_capture_folders.queue_name == "persist"
    assert (compute_signals.actor_name, compute_signals.queue_name) == (
        "compute_signals",
        "signals",
    )
    assert compute_signals.options["max_retries"] == 3
    assert compute_signals.options["time_limit"] == 60_000


def test_ping_actor_options_and_result() -> None:
    assert ping.queue_name == "default"
    assert ping.options["store_results"] is True
    assert ping.options["max_retries"] == 0
    assert ping.fn("x") == "pong:x"


def test_worker_ai_owns_only_the_ai_queue() -> None:
    spec = WORKERS["ai"]
    assert (spec.service, spec.queues, spec.threads) == ("worker-ai", ("ai",), 4)
    parsed = make_argument_parser().parse_args(dramatiq_args(spec))
    assert parsed.broker == "kalkan.workers.ai"
    # worker-default and the scheduler send ai_triage and ai_confirm by name.
    assert "kalkan.workers.assess" not in WORKERS["default"].actor_modules


def test_by_name_sends_reach_their_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    from kalkan.ai import queue

    sent: list[tuple[str, str, dict[str, object]]] = []

    class Recording:
        def enqueue(self, message: dramatiq.Message[None]) -> None:
            sent.append((message.queue_name, message.actor_name, message.kwargs))

    monkeypatch.setattr(dramatiq, "get_broker", Recording)
    queue.send_ai_triage("case-1")
    queue.send_ai_confirm("case-2")
    queue.send_decide("case-3")
    assert sent == [
        ("ai", "ai_triage", {"case_id": "case-1"}),
        ("ai", "ai_confirm", {"case_id": "case-2"}),
        ("decision", "decide", {"case_id": "case-3"}),
    ]
