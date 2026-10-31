"""A fake Claude API for the AI tests: recorded response bodies through ``httpx2.MockTransport``.

Messages answers are served in the order they were queued; every request is kept, so a test
can read what was sent. Files uploads and the Message Batches endpoints answer with the
recorded bodies of ``tests/fixtures/ai``. Nothing leaves the process.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2

from kalkan.ai.client import AiClient, MemoryRecorder, Recorder
from kalkan.ai.prices import default_prices

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
API_KEY = "sk-ant-test-not-a-key"


def recorded(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def candidate_text(body: dict[str, Any]) -> str:
    """The candidate block of a Messages request body."""
    text: str = body["messages"][0]["content"][-1]["text"]
    return text


def verdict_message(**changes: Any) -> dict[str, Any]:
    """The recorded verdict answer with some verdict fields changed."""
    message = recorded("message_verdict.json")
    verdict = json.loads(message["content"][1]["text"])
    verdict.update(changes)
    message["content"][1]["text"] = json.dumps(verdict)
    return message


Reply = tuple[int, dict[str, Any]]


@dataclass
class FakeApi:
    messages: list[Reply] = field(default_factory=list)
    batch_results: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    # Chooses the answer from the request body instead of the queue (parallel calls).
    respond: Callable[[dict[str, Any]], Reply] | None = None
    requests: list[httpx2.Request] = field(default_factory=list)
    batch_polls: int = 0
    _batch_requests: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, *replies: Reply | str) -> FakeApi:
        for reply in replies:
            self.messages.append((200, recorded(reply)) if isinstance(reply, str) else reply)
        return self

    def posts(self, path: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == path and r.method == "POST"]

    def bodies(self, path: str = "/v1/messages") -> list[dict[str, Any]]:
        """The JSON bodies of the POST requests to ``path``."""
        return [json.loads(request.content) for request in self.posts(path)]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/messages" and request.method == "POST":
            if self.respond is not None:
                status, body = self.respond(json.loads(request.content))
                return httpx2.Response(status, json=body)
            if not self.messages:
                raise AssertionError("an unexpected Messages API call")
            status, body = self.messages.pop(0)
            return httpx2.Response(status, json=body)
        if path == "/v1/files" and request.method == "POST":
            return httpx2.Response(200, json=recorded("file_upload.json"))
        if path == "/v1/messages/batches" and request.method == "POST":
            self._batch_requests = json.loads(request.content)["requests"]
            return httpx2.Response(200, json=recorded("batch_in_progress.json"))
        if path.endswith("/results"):
            assert self.batch_results is not None
            lines = self.batch_results(self._batch_requests)
            text = "".join(json.dumps(line) + "\n" for line in lines)
            return httpx2.Response(200, content=text.encode())
        if path.startswith("/v1/messages/batches/"):
            self.batch_polls += 1
            name = "batch_ended.json" if self.batch_polls > 1 else "batch_in_progress.json"
            return httpx2.Response(200, json=recorded(name))
        raise AssertionError(f"unexpected request {request.method} {path}")

    def client(
        self,
        *,
        recorder: Recorder | None = None,
        budget: Decimal = Decimal(50),
        sleeps: list[float] | None = None,
    ) -> AiClient:
        return AiClient.create(
            API_KEY,
            prices=default_prices(),
            monthly_budget_usd=budget,
            recorder=recorder if recorder is not None else MemoryRecorder(),
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)),
            sleep=(sleeps if sleeps is not None else []).append,
        )
