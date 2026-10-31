"""The Claude client of every AI stage (architecture.md §7, §13).

One :class:`AiClient` per process wraps the official ``anthropic`` SDK:

- The models come from configuration (``AI_*_MODEL``); ``temperature`` and the other sampling
  parameters are never sent. Effort is sent only to models that take it (not Haiku 4.5).
- Each request has a 60-second time-out. 429, 408, 409, 5xx (overloaded included) and
  connection errors are tried 3 times in all with exponential back-off (``retry-after`` is
  honoured up to 30 seconds), then :class:`AiUnavailableError`. The SDK's own retries are off, so
  every attempt is a row in ``ai_calls``: token counts, cache reads and writes, cost, latency
  and the error.
- At most :data:`CONCURRENCY` requests run at once in a process.
- The monthly budget (``AI_MONTHLY_BUDGET_USD``) is checked against this month's ``ai_calls``
  before every call; when it is used up, :class:`BudgetExhaustedError` and no request.
- The answer must match the output model. It is requested as structured output (the JSON
  schema the SDK's ``messages.parse`` would send, built with ``anthropic.transform_schema``)
  and validated here, so a response that fails validation still has its usage recorded. A
  refusal, a cut-off (``max_tokens``) or an invalid answer is asked once more, then
  :class:`InvalidOutputError` (K123).
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from kalkan.ai.cost import TokenUsage, UnknownModelError, cost_usd, month_spend
from kalkan.ai.prices import Price
from kalkan.ai.prompting import Prompt
from kalkan.db.models import AiCall
from kalkan.db.session import Database
from kalkan.domain.enums import AiPurpose
from kalkan.logging import get_logger

Effort = Literal["low", "medium", "high"]

TIMEOUT_SECONDS = 60.0
ATTEMPTS = 3
VALIDATION_ATTEMPTS = 2
BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER = 30.0
CONCURRENCY = 4
MAX_TOKENS = 4096
MAX_ERROR = 500
# Models that reject output_config.effort.
NO_EFFORT_PREFIXES = ("claude-haiku-4-5", "claude-3")
RETRY_STATUS = frozenset({408, 409, 429})

log = get_logger(__name__)


class AiError(RuntimeError):
    """The call gave no usable answer; the case goes to review (§13)."""

    reason = "ai_unavailable"


class AiUnavailableError(AiError):
    """The API failed on every attempt, or refused the request."""


class InvalidOutputError(AiError):
    """The answer was a refusal, cut off or did not match the schema, twice."""


class BudgetExhaustedError(AiError):
    """This month's AI spend reached AI_MONTHLY_BUDGET_USD; no request was made."""

    reason = "ai_budget_exhausted"


@dataclass(frozen=True)
class CallRecord:
    purpose: AiPurpose
    model: str
    prompt_version: str
    case_id: uuid.UUID | None
    usage: TokenUsage
    cost_usd: Decimal
    latency_ms: int | None
    error: str | None
    created_at: datetime


class Recorder(Protocol):
    def record(self, call: CallRecord) -> uuid.UUID: ...

    def month_spend(self, now: datetime) -> Decimal: ...


class DatabaseRecorder:
    """``ai_calls`` rows, written at once in their own transaction (kalkan_worker)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, call: CallRecord) -> uuid.UUID:
        row = AiCall(
            case_id=call.case_id,
            purpose=call.purpose,
            model=call.model,
            prompt_version=call.prompt_version,
            input_tokens=call.usage.input_tokens,
            output_tokens=call.usage.output_tokens,
            cache_read_tokens=call.usage.cache_read_tokens,
            cache_write_tokens=call.usage.cache_write_tokens,
            cost_usd=call.cost_usd,
            latency_ms=call.latency_ms,
            error=call.error,
            created_at=call.created_at,
        )
        with self._database.worker_session() as session:
            session.add(row)
            session.flush()
            return row.id

    def month_spend(self, now: datetime) -> Decimal:
        with self._database.worker_session() as session:
            return month_spend(session, now)[1]


@dataclass
class MemoryRecorder:
    """Calls kept in memory: an evaluation without a database, and tests."""

    calls: list[CallRecord] = field(default_factory=list)
    spent_before: Decimal = Decimal(0)

    def record(self, call: CallRecord) -> uuid.UUID:
        self.calls.append(call)
        return uuid.uuid4()

    def month_spend(self, now: datetime) -> Decimal:
        return self.spent_before + sum((call.cost_usd for call in self.calls), Decimal(0))


@dataclass(frozen=True)
class Answer[T: BaseModel]:
    output: T
    ai_call_id: uuid.UUID
    model: str
    prompt_version: str
    usage: TokenUsage
    cost_usd: Decimal


def output_format(output: type[BaseModel]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": anthropic.transform_schema(output)}


def supports_effort(model: str) -> bool:
    return not model.startswith(NO_EFFORT_PREFIXES)


def request_params(
    prompt: Prompt,
    *,
    model: str,
    output: type[BaseModel],
    effort: Effort | None,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """The Messages API body of one call (also a Batch API request's ``params``)."""
    config: dict[str, Any] = {"format": output_format(output)}
    if effort is not None and supports_effort(model):
        config["effort"] = effort
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": prompt.system}],
        "messages": [{"role": "user", "content": prompt.content}],
        "output_config": config,
    }


def _status(exc: anthropic.APIError) -> int | None:
    return exc.status_code if isinstance(exc, anthropic.APIStatusError) else None


def retryable(exc: anthropic.APIError) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    status = _status(exc)
    return status is not None and (status in RETRY_STATUS or status >= 500)


def _retry_after(exc: anthropic.APIError) -> float | None:
    if not isinstance(exc, anthropic.APIStatusError):
        return None
    raw = exc.response.headers.get("retry-after")
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(raw))) if raw is not None else None
    except ValueError:
        return None


def _error_text(exc: BaseException) -> str:
    status = _status(exc) if isinstance(exc, anthropic.APIError) else None
    prefix = f"{type(exc).__name__}" + (f" {status}" if status else "")
    return f"{prefix}: {exc}"[:MAX_ERROR]


def parse_output[T: BaseModel](message: Any, output: type[T]) -> T:
    """The validated answer of ``message``; :class:`InvalidOutputError` when there is none."""
    stop = getattr(message, "stop_reason", None)
    if stop in {"refusal", "max_tokens"}:
        raise InvalidOutputError(f"stop_reason {stop}")
    texts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    if not texts:
        raise InvalidOutputError("no text block in the answer")
    try:
        return output.model_validate_json(texts[-1])
    except ValidationError as exc:
        raise InvalidOutputError(
            f"schema: {exc.error_count()} error(s): {exc.errors()[0]['msg']}"
        ) from exc


class AiClient:
    def __init__(
        self,
        api: anthropic.Anthropic,
        *,
        prices: dict[str, Price],
        monthly_budget_usd: Decimal,
        recorder: Recorder,
        concurrency: int = CONCURRENCY,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.api = api
        self.prices = prices
        self.monthly_budget_usd = monthly_budget_usd
        self.recorder = recorder
        self._slots = threading.BoundedSemaphore(concurrency)
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def create(
        cls,
        api_key: str,
        *,
        prices: dict[str, Price],
        monthly_budget_usd: Decimal,
        recorder: Recorder,
        http_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> AiClient:
        api = anthropic.Anthropic(
            api_key=api_key,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,  # attempts are counted and recorded here
            http_client=http_client,
        )
        return cls(
            api,
            prices=prices,
            monthly_budget_usd=monthly_budget_usd,
            recorder=recorder,
            sleep=sleep,
        )

    # --- budget -------------------------------------------------------------------------------

    def budget_left(self) -> Decimal:
        return self.monthly_budget_usd - self.recorder.month_spend(self._clock())

    def check_budget(self) -> None:
        if self.budget_left() <= 0:
            log.warning("ai.budget_exhausted", budget_usd=str(self.monthly_budget_usd))
            raise BudgetExhaustedError("the monthly AI budget is used up")

    # --- one call -----------------------------------------------------------------------------

    @contextmanager
    def _slot(self) -> Iterator[None]:
        with self._slots:
            yield

    def _record(
        self,
        *,
        purpose: AiPurpose,
        model: str,
        prompt: Prompt,
        case_id: uuid.UUID | None,
        usage: TokenUsage,
        latency_ms: int | None,
        error: str | None,
        batch: bool = False,
    ) -> tuple[uuid.UUID, Decimal]:
        cost = cost_usd(self.prices, model, usage, batch=batch)
        call_id = self.recorder.record(
            CallRecord(
                purpose=purpose,
                model=model,
                prompt_version=prompt.version,
                case_id=case_id,
                usage=usage,
                cost_usd=cost,
                latency_ms=latency_ms,
                error=error,
                created_at=self._clock(),
            )
        )
        _count_cost(model, purpose, cost)
        return call_id, cost

    def _send(
        self,
        params: dict[str, Any],
        *,
        purpose: AiPurpose,
        prompt: Prompt,
        case_id: uuid.UUID | None,
    ) -> tuple[Any, int]:
        """One message, tried :data:`ATTEMPTS` times; failed attempts are recorded."""
        model = params["model"]
        for attempt in range(1, ATTEMPTS + 1):
            started = time.monotonic()
            try:
                with self._slot():
                    message = self.api.messages.create(**params)
            except anthropic.APIError as exc:
                latency = int((time.monotonic() - started) * 1000)
                self._record(
                    purpose=purpose,
                    model=model,
                    prompt=prompt,
                    case_id=case_id,
                    usage=TokenUsage(),
                    latency_ms=latency,
                    error=_error_text(exc),
                )
                log.warning(
                    "ai.call_failed",
                    model=model,
                    purpose=purpose.value,
                    attempt=attempt,
                    status=_status(exc),
                    error=type(exc).__name__,
                )
                if not retryable(exc):
                    raise AiUnavailableError(_error_text(exc)) from exc
                if attempt == ATTEMPTS:
                    raise AiUnavailableError(
                        f"{ATTEMPTS} attempts failed: {_error_text(exc)}"
                    ) from exc
                wait = _retry_after(exc)
                if wait is None:
                    wait = BACKOFF_SECONDS * 2 ** (attempt - 1) + random.uniform(0, 1)  # noqa: S311
                self._sleep(wait)
                continue
            return message, int((time.monotonic() - started) * 1000)
        raise AssertionError("unreachable")  # pragma: no cover

    def call[T: BaseModel](
        self,
        prompt: Prompt,
        *,
        output: type[T],
        model: str,
        purpose: AiPurpose,
        case_id: uuid.UUID | None = None,
        effort: Effort | None = None,
        check_budget: bool = True,
    ) -> Answer[T]:
        if model not in self.prices:
            raise AiUnavailableError(str(UnknownModelError(f"{model} has no price in AI_PRICES")))
        if check_budget:
            self.check_budget()
        params = request_params(prompt, model=model, output=output, effort=effort)
        last: InvalidOutputError | None = None
        for _ in range(VALIDATION_ATTEMPTS):
            message, latency = self._send(params, purpose=purpose, prompt=prompt, case_id=case_id)
            usage = TokenUsage.from_api(message.usage)
            try:
                parsed = parse_output(message, output)
            except InvalidOutputError as exc:
                self._record(
                    purpose=purpose,
                    model=model,
                    prompt=prompt,
                    case_id=case_id,
                    usage=usage,
                    latency_ms=latency,
                    error=f"invalid output: {exc}"[:MAX_ERROR],
                )
                log.warning("ai.invalid_output", model=model, purpose=purpose.value, error=str(exc))
                last = exc
                continue
            call_id, cost = self._record(
                purpose=purpose,
                model=model,
                prompt=prompt,
                case_id=case_id,
                usage=usage,
                latency_ms=latency,
                error=None,
            )
            return Answer(parsed, call_id, model, prompt.version, usage, cost)
        raise InvalidOutputError(str(last))

    # --- Batch API (evaluation) ---------------------------------------------------------------

    def call_batch[T: BaseModel](
        self,
        items: Sequence[tuple[str, Prompt]],
        *,
        output: type[T],
        model: str,
        purpose: AiPurpose,
        effort: Effort | None = None,
        poll_seconds: float = 30.0,
        timeout_seconds: float = 24 * 3600,
    ) -> dict[str, Answer[T] | AiError]:
        """``(custom id, prompt)`` pairs through the Message Batches API (half price).

        No retries: an errored or invalid result is returned as its error.
        """
        if model not in self.prices:
            raise AiUnavailableError(f"{model} has no price in AI_PRICES")
        self.check_budget()
        prompts = dict(items)
        requests: Any = [
            {
                "custom_id": custom_id,
                "params": request_params(prompt, model=model, output=output, effort=effort),
            }
            for custom_id, prompt in items
        ]
        batch = self.api.messages.batches.create(requests=requests)
        log.info("ai.batch_created", batch_id=batch.id, requests=len(items), model=model)
        deadline = time.monotonic() + timeout_seconds
        while batch.processing_status != "ended":
            if time.monotonic() > deadline:
                raise AiUnavailableError(f"batch {batch.id} did not end in time")
            self._sleep(poll_seconds)
            batch = self.api.messages.batches.retrieve(batch.id)
        results: dict[str, Answer[T] | AiError] = {}
        for entry in self.api.messages.batches.results(batch.id):
            prompt = prompts.get(entry.custom_id)
            if prompt is None:
                continue
            result = entry.result
            if result.type != "succeeded":
                error = AiUnavailableError(f"batch result {result.type}")
                self._record(
                    purpose=purpose,
                    model=model,
                    prompt=prompt,
                    case_id=None,
                    usage=TokenUsage(),
                    latency_ms=None,
                    error=str(error),
                    batch=True,
                )
                results[entry.custom_id] = error
                continue
            message = result.message
            usage = TokenUsage.from_api(message.usage)
            try:
                parsed = parse_output(message, output)
            except InvalidOutputError as exc:
                self._record(
                    purpose=purpose,
                    model=model,
                    prompt=prompt,
                    case_id=None,
                    usage=usage,
                    latency_ms=None,
                    error=f"invalid output: {exc}"[:MAX_ERROR],
                    batch=True,
                )
                results[entry.custom_id] = exc
                continue
            call_id, cost = self._record(
                purpose=purpose,
                model=model,
                prompt=prompt,
                case_id=None,
                usage=usage,
                latency_ms=None,
                error=None,
                batch=True,
            )
            results[entry.custom_id] = Answer(parsed, call_id, model, prompt.version, usage, cost)
        return results


def _count_cost(model: str, purpose: AiPurpose, cost: Decimal) -> None:
    from kalkan.ai.metrics import metrics

    metrics().cost.labels(model=model, purpose=purpose.value).inc(float(cost))
