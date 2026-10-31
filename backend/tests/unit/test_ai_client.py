"""The Claude client against recorded API responses (architecture.md §13)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from kalkan.ai.client import (
    AiUnavailableError,
    BudgetExhaustedError,
    InvalidOutputError,
    MemoryRecorder,
)
from kalkan.ai.prompting import Prompt
from kalkan.ai.schema import Verdict
from kalkan.domain.enums import AiPurpose
from tests.ai_support import FakeApi, recorded, verdict_message

PROMPT = Prompt(system="system prompt", content=[{"type": "text", "text": "candidate"}])
TRIAGE: dict[str, Any] = {
    "output": Verdict,
    "model": "claude-haiku-5-5",
    "purpose": AiPurpose.TRIAGE,
}


def test_a_verdict_is_parsed_and_its_call_recorded() -> None:
    api = FakeApi().queue("message_verdict.json")
    recorder = MemoryRecorder()
    case_id = uuid.uuid4()
    answer = api.client(recorder=recorder).call(PROMPT, case_id=case_id, effort="low", **TRIAGE)
    assert answer.output.category == "phishing_clone"
    assert answer.output.confidence == pytest.approx(0.94)
    [call] = recorder.calls
    assert (call.case_id, call.purpose, call.model, call.error) == (
        case_id,
        AiPurpose.TRIAGE,
        "claude-haiku-5-5",
        None,
    )
    assert call.usage.cache_write_1h_tokens == 2600
    assert call.usage.input_tokens == 1450
    # 1450*0.10 + 2600*0.20 + 210*0.50 per million
    assert call.cost_usd == answer.cost_usd == Decimal("0.000770")
    assert call.prompt_version == PROMPT.version


def test_the_request_has_the_schema_and_effort_but_no_sampling_parameters() -> None:
    api = FakeApi().queue("message_verdict.json")
    api.client().call(PROMPT, effort="low", **TRIAGE)
    [body] = api.bodies()
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert schema["properties"]["category"]["enum"][0] == "phishing_clone"
    assert schema["additionalProperties"] is False
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert body["system"] == [{"type": "text", "text": "system prompt"}]
    request = api.requests[0]
    assert request.headers["authorization"] == "Bearer sk-ant-test-not-a-key"
    assert request.headers["x-stainless-retry-count"] == "0"  # the SDK's retries are off


def test_haiku_4_5_gets_no_effort() -> None:
    api = FakeApi().queue("message_verdict.json")
    api.client().call(
        PROMPT, output=Verdict, model="claude-haiku-4-5", purpose=AiPurpose.EVAL, effort="low"
    )
    assert "effort" not in api.bodies()[0]["output_config"]


def test_enum_values_are_compared_without_case() -> None:
    api = FakeApi().queue("message_verdict_mixed_case.json")
    answer = api.client().call(PROMPT, **TRIAGE)
    assert answer.output.category == "phishing_clone"
    assert answer.output.copied_elements == ["logo", "layout"]
    assert answer.output.suggested_basis == "phishing"


def test_an_overloaded_api_is_tried_three_times_and_every_attempt_recorded() -> None:
    api = FakeApi().queue(
        (529, recorded("error_overloaded.json")), (529, recorded("error_overloaded.json"))
    )
    api.queue("message_verdict.json")
    recorder = MemoryRecorder()
    waits: list[float] = []
    answer = api.client(recorder=recorder, sleeps=waits).call(PROMPT, **TRIAGE)
    assert answer.output.category == "phishing_clone"
    assert [call.error is not None for call in recorder.calls] == [True, True, False]
    assert "OverloadedError 529" in (recorder.calls[0].error or "")
    assert len(waits) == 2
    assert waits[0] < waits[1]


def test_after_three_failures_the_ai_is_unavailable() -> None:
    api = FakeApi().queue(*[(529, recorded("error_overloaded.json"))] * 3)
    recorder = MemoryRecorder()
    with pytest.raises(AiUnavailableError) as raised:
        api.client(recorder=recorder).call(PROMPT, **TRIAGE)
    assert raised.value.reason == "ai_unavailable"
    assert len(recorder.calls) == 3
    assert all(call.cost_usd == 0 for call in recorder.calls)


def test_a_bad_request_is_not_retried() -> None:
    api = FakeApi().queue((400, recorded("error_bad_request.json")))
    with pytest.raises(AiUnavailableError):
        api.client().call(PROMPT, **TRIAGE)
    assert len(api.requests) == 1


def test_an_invalid_answer_is_asked_once_more() -> None:
    api = FakeApi().queue("message_invalid.json", "message_verdict.json")
    recorder = MemoryRecorder()
    answer = api.client(recorder=recorder).call(PROMPT, **TRIAGE)
    assert answer.output.category == "phishing_clone"
    assert (recorder.calls[0].error or "").startswith("invalid output: schema")
    # The invalid answer's tokens are paid for and counted.
    assert recorder.calls[0].usage.input_tokens == 4050
    assert recorder.calls[0].cost_usd > 0


def test_two_invalid_answers_are_an_error() -> None:
    api = FakeApi().queue("message_refusal.json", "message_invalid.json")
    recorder = MemoryRecorder()
    with pytest.raises(InvalidOutputError):
        api.client(recorder=recorder).call(PROMPT, **TRIAGE)
    assert "stop_reason refusal" in (recorder.calls[0].error or "")
    assert len(recorder.calls) == 2


def test_a_used_up_budget_makes_no_request() -> None:
    api = FakeApi()
    recorder = MemoryRecorder(spent_before=Decimal("50.00"))
    with pytest.raises(BudgetExhaustedError) as raised:
        api.client(recorder=recorder, budget=Decimal("50")).call(PROMPT, **TRIAGE)
    assert raised.value.reason == "ai_budget_exhausted"
    assert api.requests == []
    assert recorder.calls == []


def test_an_unpriced_model_makes_no_request() -> None:
    api = FakeApi()
    with pytest.raises(AiUnavailableError, match="no price"):
        api.client().call(PROMPT, output=Verdict, model="claude-unknown-1", purpose=AiPurpose.EVAL)
    assert api.requests == []


def test_a_batch_is_polled_and_its_results_scored_at_half_price() -> None:
    def results(requests: list[dict[str, object]]) -> list[dict[str, object]]:
        first, second = (str(request["custom_id"]) for request in requests)
        return [
            {"custom_id": first, "result": {"type": "succeeded", "message": verdict_message()}},
            {
                "custom_id": second,
                "result": {
                    "type": "errored",
                    "error": {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "x"},
                    },
                },
            },
        ]

    api = FakeApi(batch_results=results)
    recorder = MemoryRecorder()
    waits: list[float] = []
    answers = api.client(recorder=recorder, sleeps=waits).call_batch(
        [("case-a", PROMPT), ("case-b", PROMPT)],
        output=Verdict,
        model="claude-haiku-5-5",
        purpose=AiPurpose.EVAL,
        poll_seconds=5,
    )
    assert not isinstance(answers["case-a"], Exception)
    assert answers["case-a"].output.category == "phishing_clone"
    assert isinstance(answers["case-b"], AiUnavailableError)
    # Half of the standard price of the same usage (0.000770).
    assert answers["case-a"].cost_usd == Decimal("0.000385")
    assert waits == [5, 5]  # in progress, in progress, ended
    [batch] = api.bodies("/v1/messages/batches")
    assert [item["custom_id"] for item in batch["requests"]] == ["case-a", "case-b"]
    assert batch["requests"][0]["params"]["output_config"]["format"]["type"] == "json_schema"
