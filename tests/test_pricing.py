"""Hand-computed costs. Rates are USD per million tokens, so each expected value below is the
sum of tokens x rate, divided by 1,000,000."""

import pytest

from swarm.llm.pricing import LONG_PROMPT_PRICES, PRICES, UnknownModelError, cost_usd
from swarm.llm.types import Usage


def cost(model: str, cache_ttl: str = "1h", **tokens: int) -> float:
    return cost_usd(model, Usage(**tokens), provider="anthropic", cache_ttl=cache_ttl)


def test_sonnet_input_and_output():
    # 1,000 x 2 + 500 x 10 = 7,000
    assert cost("claude-sonnet-5-5", input_tokens=1_000, output_tokens=500) == pytest.approx(0.007)


def test_sonnet_cache_write_rate_follows_the_ttl():
    # 10,000 x 4 (1 hour) vs 10,000 x 2.50 (5 minutes)
    assert cost("claude-sonnet-5-5", "1h", cache_creation_input_tokens=10_000) == pytest.approx(
        0.04
    )
    assert cost("claude-sonnet-5-5", "5m", cache_creation_input_tokens=10_000) == pytest.approx(
        0.025
    )


def test_sonnet_cache_read_is_much_cheaper_than_input():
    # 10,000 x 0.10 (cache read) vs 10,000 x 2 (uncached input)
    assert cost("claude-sonnet-5-5", cache_read_input_tokens=10_000) == pytest.approx(0.001)
    assert cost("claude-sonnet-5-5", input_tokens=10_000) == pytest.approx(0.02)


def test_sonnet_mixed_call():
    # 200 x 2 + 3,000 x 4 + 7,000 x 0.10 + 50 x 10 = 400 + 12,000 + 700 + 500 = 13,600
    usd = cost(
        "claude-sonnet-5-5",
        input_tokens=200,
        cache_creation_input_tokens=3_000,
        cache_read_input_tokens=7_000,
        output_tokens=50,
    )
    assert usd == pytest.approx(0.0136)


def test_haiku_short_prompt_read_vs_write():
    tokens = {
        "input_tokens": 2_000,
        "cache_creation_input_tokens": 1_000,
        "cache_read_input_tokens": 5_000,
        "output_tokens": 100,
    }
    # 1h: 2,000 x 0.10 + 1,000 x 0.20 + 5,000 x 0.01 + 100 x 0.50 = 200 + 200 + 50 + 50 = 500
    assert cost("claude-haiku-5-5", "1h", **tokens) == pytest.approx(0.0005)
    # 5m: the write costs 1,000 x 0.125 = 125 instead of 200
    assert cost("claude-haiku-5-5", "5m", **tokens) == pytest.approx(0.000425)


def test_haiku_long_prompt_card_starts_above_100k_tokens():
    # A 100,000-token prompt stays on the short card:
    # 40,000 x 0.10 + 60,000 x 0.01 + 1,000 x 0.50 = 4,000 + 600 + 500 = 5,100
    at_limit = cost(
        "claude-haiku-5-5", input_tokens=40_000, cache_read_input_tokens=60_000, output_tokens=1_000
    )
    assert at_limit == pytest.approx(0.0051)
    # One more token moves the whole request to the long card:
    # 40,001 x 0.50 + 60,000 x 0.05 + 1,000 x 2.50 = 20,000.5 + 3,000 + 2,500 = 25,500.5
    over = cost(
        "claude-haiku-5-5", input_tokens=40_001, cache_read_input_tokens=60_000, output_tokens=1_000
    )
    assert over == pytest.approx(0.0255005)


def test_haiku_cache_writes_count_toward_the_long_prompt_threshold():
    # 1 + 100,000 written = 100,001-token prompt -> long card: 1 x 0.50 + 100,000 x 1.00
    usd = cost("claude-haiku-5-5", input_tokens=1, cache_creation_input_tokens=100_000)
    assert usd == pytest.approx(0.1000005)


def test_opus_call():
    # 1,000 x 4 + 10,000 x 0.20 + 1,000 x 20 = 4,000 + 2,000 + 20,000 = 26,000
    usd = cost(
        "claude-opus-5-5", input_tokens=1_000, cache_read_input_tokens=10_000, output_tokens=1_000
    )
    assert usd == pytest.approx(0.026)


def test_zero_usage_costs_nothing():
    assert cost("claude-sonnet-5-5") == 0.0


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError, match="claude-sonnet-4-6"):
        cost("claude-sonnet-4-6", input_tokens=1)


def test_unknown_provider_raises():
    with pytest.raises(UnknownModelError):
        cost_usd("claude-sonnet-5-5", Usage(input_tokens=1), provider="nowhere", cache_ttl="1h")


def test_unknown_ttl_raises():
    with pytest.raises(ValueError, match="cache_ttl"):
        cost("claude-sonnet-5-5", "10m", input_tokens=1)


def test_every_rate_card_has_the_documented_write_multipliers():
    cards = [rates for models in PRICES.values() for rates in models.values()]
    cards += [rates for models in LONG_PROMPT_PRICES.values() for _, rates in models.values()]
    for rates in cards:
        assert rates.cache_write_5m == pytest.approx(1.25 * rates.input)
        assert rates.cache_write_1h == pytest.approx(2 * rates.input)


def test_a_batch_call_costs_half():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_input_tokens=10_000)
    full = cost_usd("claude-sonnet-5-5", usage, provider="anthropic", cache_ttl="5m")
    half = cost_usd("claude-sonnet-5-5", usage, provider="anthropic", cache_ttl="5m", batch=True)
    assert half == pytest.approx(full / 2) and full == pytest.approx(2 + 10 + 0.001)


def test_sonnet_5_reads_its_cache_at_a_tenth_of_input():
    usage = Usage(input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5", usage, provider="anthropic", cache_ttl="5m") == (
        pytest.approx(2.0 + 0.20)
    )
