"""The ``ai`` and ``decision`` queues as seen from other processes (architecture.md §7, §13).

worker-ai declares ``ai_triage(case_id)`` and ``ai_confirm(case_id)``; worker-default sends
the first after ``compute_signals`` and the scheduler's reconciler both; ``decide`` sends the
second. worker-default declares ``decide(case_id)`` and ``send_notice(notice_id)``; worker-ai
sends both by name, the scheduler sends ``ai_notice_text`` by name; worker-default and the ops
panel send ``ai_classify_reply`` by name. A message sent by name needs
no actor in the sending process.

Without AI (``Settings.use_ai`` false, set once per process by :func:`set_ai_enabled`) a message
for an AI actor goes to its worker-default stand-in (:data:`WITHOUT_AI`) instead, with the same
arguments: triage and confirmation send the case to the review queue (``ai_disabled``), notice
text uses the fixed paragraph, a reply is left to the operator. Nothing then waits on the
``ai`` queue for a worker-ai that is not running. The flag is process state, not read from the
settings here, so code that never booted a process (tests, tools) keeps sending to worker-ai.
"""

from __future__ import annotations

import dramatiq

AI_QUEUE = "ai"
AI_TRIAGE_ACTOR = "ai_triage"
AI_CONFIRM_ACTOR = "ai_confirm"
AI_NOTICE_TEXT_ACTOR = "ai_notice_text"
AI_CLASSIFY_REPLY_ACTOR = "ai_classify_reply"
DECISION_QUEUE = "decision"
DECIDE_ACTOR = "decide"
NOTIFY_QUEUE = "notify"
SEND_NOTICE_ACTOR = "send_notice"
WEBHOOKS_QUEUE = "webhooks"
# worker-default's stand-ins for the AI actors (kalkan.workers.without_ai): actor -> queue.
TRIAGE_WITHOUT_AI_ACTOR = "triage_without_ai"
CONFIRM_WITHOUT_AI_ACTOR = "confirm_without_ai"
NOTICE_TEXT_WITHOUT_AI_ACTOR = "notice_text_without_ai"
CLASSIFY_REPLY_WITHOUT_AI_ACTOR = "classify_reply_without_ai"
# Actors other processes reach by name: actor -> queue.
BY_NAME = {
    AI_TRIAGE_ACTOR: AI_QUEUE,
    AI_CONFIRM_ACTOR: AI_QUEUE,
    AI_NOTICE_TEXT_ACTOR: AI_QUEUE,
    AI_CLASSIFY_REPLY_ACTOR: AI_QUEUE,
    DECIDE_ACTOR: DECISION_QUEUE,
    SEND_NOTICE_ACTOR: NOTIFY_QUEUE,
}
# AI actor -> (stand-in actor, its queue); the stand-ins take the same keyword arguments.
WITHOUT_AI = {
    AI_TRIAGE_ACTOR: (TRIAGE_WITHOUT_AI_ACTOR, DECISION_QUEUE),
    AI_CONFIRM_ACTOR: (CONFIRM_WITHOUT_AI_ACTOR, DECISION_QUEUE),
    AI_NOTICE_TEXT_ACTOR: (NOTICE_TEXT_WITHOUT_AI_ACTOR, NOTIFY_QUEUE),
    AI_CLASSIFY_REPLY_ACTOR: (CLASSIFY_REPLY_WITHOUT_AI_ACTOR, WEBHOOKS_QUEUE),
}

_ai_enabled = True


def set_ai_enabled(enabled: bool) -> None:
    """Called once at start-up by every process that sends (``Settings.use_ai``)."""
    global _ai_enabled
    _ai_enabled = enabled


def ai_enabled() -> bool:
    return _ai_enabled


def route(actor_name: str) -> tuple[str, str]:
    """The actor and queue a message for ``actor_name`` goes to in this process."""
    if not _ai_enabled and actor_name in WITHOUT_AI:
        return WITHOUT_AI[actor_name]
    return actor_name, BY_NAME[actor_name]


def send_by_name(actor_name: str, **kwargs: str) -> None:
    """A message for an actor of another process; its arguments go as keywords.

    Without AI, an AI actor's message goes to its stand-in (:func:`route`).
    """
    target, queue_name = route(actor_name)
    message: dramatiq.Message[None] = dramatiq.Message(
        queue_name=queue_name,
        actor_name=target,
        args=(),
        kwargs=kwargs,
        options={},
    )
    dramatiq.get_broker().enqueue(message)


def send_ai_triage(case_id: str) -> None:
    send_by_name(AI_TRIAGE_ACTOR, case_id=case_id)


def send_ai_confirm(case_id: str) -> None:
    send_by_name(AI_CONFIRM_ACTOR, case_id=case_id)


def send_decide(case_id: str) -> None:
    send_by_name(DECIDE_ACTOR, case_id=case_id)


def send_notice(notice_id: str, customer_id: str) -> None:
    send_by_name(SEND_NOTICE_ACTOR, notice_id=notice_id, customer_id=customer_id)


def send_classify_reply(message_id: str) -> None:
    send_by_name(AI_CLASSIFY_REPLY_ACTOR, message_id=message_id)
