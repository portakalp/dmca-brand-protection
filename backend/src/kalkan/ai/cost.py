"""What a Claude call costs (architecture.md §13).

``AI_PRICES`` maps a model id to its rates in USD per million tokens; the default is the
§13 table (platform.claude.com pricing page, October 2026). Cache writes are priced by their
lifetime (5 minutes or 1 hour), cache reads separately; the Batch API halves input and output,
and its discount applies to the cache rates too. A model whose prompt (input, cache reads and
cache writes together) is longer than ``long_context_threshold`` tokens is billed at its
``long_context`` rates for the whole call (Haiku 5.5 above 100K tokens).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalkan.ai.prices import Price, Rates
from kalkan.db.models import AiCall

MILLION = Decimal(1_000_000)
COST_PLACES = Decimal("0.000001")  # ai_calls.cost_usd is Numeric(12, 6)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @classmethod
    def from_api(cls, usage: Any) -> TokenUsage:
        """From the SDK's ``Usage`` (or its dict). A cache write without its lifetime split
        is counted as a 5-minute write."""
        data = usage if isinstance(usage, dict) else usage.model_dump()
        written = int(data.get("cache_creation_input_tokens") or 0)
        split = data.get("cache_creation") or {}
        one_hour = int(split.get("ephemeral_1h_input_tokens") or 0)
        five_minutes = int(split.get("ephemeral_5m_input_tokens") or 0)
        if one_hour + five_minutes != written:
            five_minutes, one_hour = written - one_hour, one_hour
        return cls(
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cache_read_tokens=int(data.get("cache_read_input_tokens") or 0),
            cache_write_5m_tokens=five_minutes,
            cache_write_1h_tokens=one_hour,
        )


class UnknownModelError(LookupError):
    """The model has no entry in AI_PRICES: its calls could not be counted against the budget."""


def cost_usd(
    prices: dict[str, Price], model: str, usage: TokenUsage, *, batch: bool = False
) -> Decimal:
    price = prices.get(model)
    if price is None:
        raise UnknownModelError(f"{model} has no price in AI_PRICES")
    rates: Rates = price
    if (
        price.long_context is not None
        and price.long_context_threshold is not None
        and usage.prompt_tokens > price.long_context_threshold
    ):
        rates = price.long_context
    discount = rates.batch_input / rates.input if batch and rates.input else Decimal(1)
    total = (
        usage.input_tokens * (rates.batch_input if batch else rates.input)
        + usage.output_tokens * (rates.batch_output if batch else rates.output)
        + usage.cache_read_tokens * rates.cache_read * discount
        + usage.cache_write_5m_tokens * rates.cache_write_5m * discount
        + usage.cache_write_1h_tokens * rates.cache_write_1h * discount
    )
    return (total / MILLION).quantize(COST_PLACES)


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(session: Session, now: datetime) -> tuple[int, Decimal]:
    """``(calls, USD)`` of the ``ai_calls`` rows of the current UTC month."""
    calls, cost = session.execute(
        select(func.count(AiCall.id), func.coalesce(func.sum(AiCall.cost_usd), 0)).where(
            AiCall.created_at >= month_start(now)
        )
    ).one()
    return int(calls), Decimal(cost)
