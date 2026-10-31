"""Running without AI (``AI_ENABLED``): AI work goes to worker-default's stand-ins, not to the
``ai`` queue; with AI on, nothing changes."""

from __future__ import annotations

import importlib
import inspect
import uuid
from typing import Any

import dramatiq
import pytest

from kalkan.ai import queue
from kalkan.workers.specs import WORKERS
from tests.support import make_settings

AI_ACTORS = (
    queue.AI_TRIAGE_ACTOR,
    queue.AI_CONFIRM_ACTOR,
    queue.AI_NOTICE_TEXT_ACTOR,
    queue.AI_CLASSIFY_REPLY_ACTOR,
)


class Recording:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def enqueue(self, message: dramatiq.Message[Any]) -> None:
        self.sent.append((message.queue_name, message.actor_name, message.kwargs))


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> Recording:
    recording = Recording()
    monkeypatch.setattr(queue.dramatiq, "get_broker", lambda: recording)
    return recording


@pytest.fixture
def ai_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # The process flag; monkeypatch puts the original back after the test.
    monkeypatch.setattr(queue, "_ai_enabled", True)
    queue.set_ai_enabled(False)


# --- the setting ------------------------------------------------------------------------------


def test_ai_follows_the_key_unless_ai_enabled_says_otherwise(
    clean_env: pytest.MonkeyPatch,
) -> None:
    assert make_settings(env="local").use_ai is False
    assert make_settings(env="local", anthropic_api_key="sk-ant-test").use_ai is True
    # worker-default and the scheduler never get the key: AI_ENABLED tells them.
    assert make_settings(env="local", ai_enabled=True).use_ai is True
    assert make_settings(env="local", anthropic_api_key="sk-ant-test", ai_enabled=False).use_ai is (
        False
    )


def test_ai_enabled_is_read_from_the_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("AI_ENABLED", "false")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert make_settings(env="local").use_ai is False
    clean_env.setenv("AI_ENABLED", "true")
    clean_env.delenv("ANTHROPIC_API_KEY")
    assert make_settings(env="local").use_ai is True
    # An empty value (as in .env.example) means unset.
    clean_env.setenv("AI_ENABLED", "")
    assert make_settings(env="local").ai_enabled is None


# --- routing ----------------------------------------------------------------------------------


def test_with_ai_on_messages_go_to_worker_ai(
    monkeypatch: pytest.MonkeyPatch, broker: Recording
) -> None:
    monkeypatch.setattr(queue, "_ai_enabled", True)
    queue.send_ai_triage("case-1")
    queue.send_ai_confirm("case-2")
    queue.send_classify_reply("message-3")
    queue.send_by_name(queue.AI_NOTICE_TEXT_ACTOR, notice_id="notice-4", customer_id="c")
    assert broker.sent == [
        ("ai", "ai_triage", {"case_id": "case-1"}),
        ("ai", "ai_confirm", {"case_id": "case-2"}),
        ("ai", "ai_classify_reply", {"message_id": "message-3"}),
        ("ai", "ai_notice_text", {"notice_id": "notice-4", "customer_id": "c"}),
    ]


@pytest.mark.usefixtures("ai_off")
def test_with_ai_off_nothing_goes_to_the_ai_queue(broker: Recording) -> None:
    queue.send_ai_triage("case-1")
    queue.send_ai_confirm("case-2")
    queue.send_classify_reply("message-3")
    queue.send_by_name(queue.AI_NOTICE_TEXT_ACTOR, notice_id="notice-4", customer_id="c")
    assert broker.sent == [
        ("decision", "triage_without_ai", {"case_id": "case-1"}),
        ("decision", "confirm_without_ai", {"case_id": "case-2"}),
        ("webhooks", "classify_reply_without_ai", {"message_id": "message-3"}),
        ("notify", "notice_text_without_ai", {"notice_id": "notice-4", "customer_id": "c"}),
    ]
    assert all(queue_name != queue.AI_QUEUE for queue_name, _, _ in broker.sent)


@pytest.mark.usefixtures("ai_off")
def test_with_ai_off_other_messages_are_unchanged(broker: Recording) -> None:
    queue.send_decide("case-1")
    queue.send_notice("notice-2", "customer-3")
    assert broker.sent == [
        ("decision", "decide", {"case_id": "case-1"}),
        ("notify", "send_notice", {"notice_id": "notice-2", "customer_id": "customer-3"}),
    ]


@pytest.mark.usefixtures("ai_off")
def test_the_scheduler_routes_the_outbox_and_the_reconciler(broker: Recording) -> None:
    # Both send through send_message: outbox kinds ai_notice_text and ai_classify_reply, the
    # reconciler's ai_triage and ai_confirm.
    from kalkan.scheduler.__main__ import send_message

    send_message("ai_triage", case_id="case-1")
    send_message("ai_confirm", case_id="case-2")
    send_message("ai_notice_text", notice_id="notice-3", customer_id="c")
    send_message("ai_classify_reply", message_id="message-4", customer_id="c")
    assert [(queue_name, actor) for queue_name, actor, _ in broker.sent] == [
        ("decision", "triage_without_ai"),
        ("decision", "confirm_without_ai"),
        ("notify", "notice_text_without_ai"),
        ("webhooks", "classify_reply_without_ai"),
    ]


def test_every_ai_actor_has_a_stand_in_on_a_worker_default_queue() -> None:
    assert set(queue.WITHOUT_AI) == set(AI_ACTORS)
    default = WORKERS["default"]
    assert "kalkan.workers.without_ai" in default.actor_modules
    for module in default.actor_modules:
        importlib.import_module(module)
    broker = dramatiq.get_broker()
    for ai_actor, (stand_in, queue_name) in queue.WITHOUT_AI.items():
        assert queue_name in default.queues
        assert queue_name != queue.AI_QUEUE
        actor = broker.get_actor(stand_in)
        assert actor.queue_name == queue_name
        # The stand-in takes the AI actor's message as it is (outbox payloads add customer_id).
        parameters = inspect.signature(actor.fn).parameters
        assert "customer_id" in parameters, ai_actor


def test_worker_processes_set_the_flag_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    from kalkan.workers import boot

    monkeypatch.setattr(queue, "_ai_enabled", True)
    settings = make_settings(
        env="local",
        database_url_worker="postgresql+psycopg://x@localhost/x",
        redis_url="redis://localhost:6379/15",
        ai_enabled=False,
    )
    monkeypatch.setattr(boot, "get_settings", lambda: settings)
    monkeypatch.setattr(boot, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(boot, "init_sentry", lambda *args, **kwargs: None)
    monkeypatch.setattr(boot, "setup_broker", lambda *args, **kwargs: object())
    boot.boot_worker("default")
    assert queue.ai_enabled() is False


# --- the stand-ins ----------------------------------------------------------------------------


def test_the_notice_text_stand_in_uses_the_fixed_paragraph(monkeypatch: pytest.MonkeyPatch) -> None:
    from kalkan.notices.text import TextResult
    from kalkan.workers import without_ai

    notice_id, customer_id = uuid.uuid4(), uuid.uuid4()
    clients: list[object] = []
    sent: list[tuple[str, str]] = []

    def write(database: object, client: object, *args: object, **kwargs: object) -> TextResult:
        clients.append(client)
        return TextResult(notice_id, customer_id, queued=True)

    monkeypatch.setattr(without_ai, "get_database", lambda: object())
    monkeypatch.setattr(without_ai, "get_settings", lambda: make_settings(env="local"))
    monkeypatch.setattr(without_ai, "write_explanation", write)
    monkeypatch.setattr(without_ai, "send_notice", lambda *args: sent.append(args))
    without_ai.notice_text_without_ai.fn(str(notice_id), customer_id=str(customer_id))
    # No client: notices/text.py falls back to the fixed paragraph.
    assert clients == [None]
    assert sent == [(str(notice_id), str(customer_id))]


def test_the_reply_stand_in_leaves_the_reply_to_the_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kalkan.workers import without_ai

    clients: list[object] = []
    monkeypatch.setattr(without_ai, "get_database", lambda: object())
    monkeypatch.setattr(without_ai, "get_settings", lambda: make_settings(env="local"))
    monkeypatch.setattr(
        without_ai,
        "classify_reply",
        lambda database, client, *args, **kwargs: clients.append(client),
    )
    without_ai.classify_reply_without_ai.fn(str(uuid.uuid4()))
    # No client: followup/classify.py marks it "other" and opens a reply task.
    assert clients == [None]
