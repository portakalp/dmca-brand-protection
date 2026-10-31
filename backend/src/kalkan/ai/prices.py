"""Model prices for ``AI_PRICES`` (architecture.md §13, §26).

USD per million tokens. The default is the §13 table (platform.claude.com pricing page,
October 2026). ``AI_PRICES`` replaces the whole table, as JSON:
``{"claude-haiku-5-5": {"input": "0.10", "cache_write_5m": "0.125", ...}}``. Kept apart from
the cost calculation so the configuration can import it without the database models.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Rates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input: Decimal = Field(ge=0)
    cache_write_5m: Decimal = Field(ge=0)
    cache_write_1h: Decimal = Field(ge=0)
    cache_read: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    batch_input: Decimal = Field(ge=0)
    batch_output: Decimal = Field(ge=0)


class Price(Rates):
    long_context_threshold: int | None = Field(default=None, gt=0)
    long_context: Rates | None = None


def _rates(*values: str) -> dict[str, Decimal]:
    names = (
        "input",
        "cache_write_5m",
        "cache_write_1h",
        "cache_read",
        "output",
        "batch_input",
        "batch_output",
    )
    return dict(zip(names, (Decimal(value) for value in values), strict=True))


def default_prices() -> dict[str, Price]:
    """The §13 table."""
    return {
        "claude-haiku-5-5": Price(
            **_rates("0.10", "0.125", "0.20", "0.01", "0.50", "0.05", "0.25"),
            long_context_threshold=100_000,
            long_context=Rates(**_rates("0.50", "0.625", "1.00", "0.05", "2.50", "0.25", "1.25")),
        ),
        "claude-haiku-4-5": Price(**_rates("1.00", "1.25", "2.00", "0.10", "5.00", "0.50", "2.50")),
        "claude-sonnet-5-5": Price(
            **_rates("2.00", "2.50", "4.00", "0.10", "10.00", "1.00", "5.00")
        ),
    }
