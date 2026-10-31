"""The cost of a Claude call (architecture.md §13), cache reads and writes included."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalkan.ai.cost import TokenUsage, UnknownModelError, cost_usd
from kalkan.ai.prices import default_prices

PRICES = default_prices()


def test_the_defaults_are_the_architecture_table() -> None:
    haiku = PRICES["claude-haiku-5-5"]
    assert (haiku.input, haiku.cache_write_5m, haiku.cache_write_1h, haiku.cache_read) == (
        Decimal("0.10"),
        Decimal("0.125"),
        Decimal("0.20"),
        Decimal("0.01"),
    )
    assert (haiku.output, haiku.batch_input, haiku.batch_output) == (
        Decimal("0.50"),
        Decimal("0.05"),
        Decimal("0.25"),
    )
    assert haiku.long_context is not None
    assert haiku.long_context.input == Decimal("0.50")
    assert PRICES["claude-haiku-4-5"].input == Decimal("1.00")
    sonnet = PRICES["claude-sonnet-5-5"]
    assert (sonnet.input, sonnet.cache_read, sonnet.output) == (
        Decimal("2.00"),
        Decimal("0.10"),
        Decimal("10.00"),
    )


def test_every_token_kind_is_priced() -> None:
    usage = TokenUsage(
        input_tokens=1_000,
        output_tokens=200,
        cache_read_tokens=10_000,
        cache_write_5m_tokens=2_000,
        cache_write_1h_tokens=4_000,
    )
    # 1000*0.10 + 200*0.50 + 10000*0.01 + 2000*0.125 + 4000*0.20 = 1350 per million
    assert cost_usd(PRICES, "claude-haiku-5-5", usage) == Decimal("0.001350")
    # Sonnet 5.5: 1000*2 + 200*10 + 10000*0.10 + 2000*2.5 + 4000*4 = 26000 per million
    assert cost_usd(PRICES, "claude-sonnet-5-5", usage) == Decimal("0.026000")


def test_a_cache_read_costs_a_tenth_of_input_on_haiku() -> None:
    cached = cost_usd(PRICES, "claude-haiku-4-5", TokenUsage(cache_read_tokens=1_000_000))
    fresh = cost_usd(PRICES, "claude-haiku-4-5", TokenUsage(input_tokens=1_000_000))
    assert (cached, fresh) == (Decimal("0.100000"), Decimal("1.000000"))


def test_a_long_prompt_uses_the_long_context_rates_for_the_whole_call() -> None:
    # 90K cached + 20K fresh = 110K prompt tokens, above Haiku 5.5's 100K tier.
    usage = TokenUsage(input_tokens=20_000, cache_read_tokens=90_000, output_tokens=1_000)
    # 20000*0.50 + 90000*0.05 + 1000*2.50 = 17000 per million
    assert cost_usd(PRICES, "claude-haiku-5-5", usage) == Decimal("0.017000")
    short = TokenUsage(input_tokens=10_000, cache_read_tokens=90_000, output_tokens=1_000)
    # Exactly 100K is still the lower tier: 10000*0.10 + 90000*0.01 + 1000*0.50
    assert cost_usd(PRICES, "claude-haiku-5-5", short) == Decimal("0.002400")


def test_the_batch_discount_covers_the_cache_rates() -> None:
    usage = TokenUsage(
        input_tokens=1_000, output_tokens=200, cache_read_tokens=10_000, cache_write_1h_tokens=4_000
    )
    # Half of 1000*0.10 + 200*0.50 + 10000*0.01 + 4000*0.20 = 1100 / 2 per million
    assert cost_usd(PRICES, "claude-haiku-5-5", usage, batch=True) == Decimal("0.000550")


def test_an_unpriced_model_is_refused() -> None:
    with pytest.raises(UnknownModelError):
        cost_usd(PRICES, "claude-unknown-1", TokenUsage(input_tokens=1))


def test_usage_from_the_api_splits_cache_writes_by_lifetime() -> None:
    usage = TokenUsage.from_api(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 30,
            "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20},
        }
    )
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (10, 20)
    assert (usage.cache_write_tokens, usage.prompt_tokens) == (30, 47)
    # Without the split the write counts as a 5-minute one.
    legacy = TokenUsage.from_api({"input_tokens": 1, "cache_creation_input_tokens": 8})
    assert (legacy.cache_write_5m_tokens, legacy.cache_write_1h_tokens) == (8, 0)
