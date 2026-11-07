"""USD prices per million tokens, and the cost of one API call.

Verified 2026-11-06 against https://platform.claude.com/docs/en/about-claude/pricing
(the bundled SDK notes said $0.20 for Sonnet 5.5 cache reads; the live page says $0.10 and wins).

How one call is priced:
- `input_tokens` (the uncached part of the prompt) at the input rate;
- `cache_creation_input_tokens` at the cache-write rate for the run's TTL
  (1.25x input for 5 minutes, 2x input for 1 hour);
- `cache_read_input_tokens` at the cache-read rate (0.05x input on Sonnet 5.5 and Opus 5.5,
  0.1x on Haiku 5.5);
- `output_tokens` at the output rate.
Claude Haiku 5.5 bills the *whole* request on a second, higher rate card when its prompt
(input + cache writes + cache reads) is over 100,000 tokens.
A Message Batches call (`batch=True`, the optional judge backend) costs half of every rate.
"""

from dataclasses import dataclass

from swarm.config import CacheTTL
from swarm.llm.types import Usage


class UnknownModelError(KeyError):
    """No price on file for this provider/model. We refuse rather than guess."""


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


# provider -> model -> rates
PRICES: dict[str, dict[str, Rates]] = {
    "anthropic": {
        "claude-sonnet-5-5": Rates(
            input=2.00, output=10.00, cache_write_5m=2.50, cache_write_1h=4.00, cache_read=0.10
        ),
        # Sonnet 5 (previous generation; step-5 second judge): verified 2026-11-07 on the live
        # pricing page. Its cache reads are 0.1x input ($0.20), not Sonnet 5.5's 0.05x.
        "claude-sonnet-5": Rates(
            input=2.00, output=10.00, cache_write_5m=2.50, cache_write_1h=4.00, cache_read=0.20
        ),
        "claude-opus-5-5": Rates(
            input=4.00, output=20.00, cache_write_5m=5.00, cache_write_1h=8.00, cache_read=0.20
        ),
        "claude-haiku-5-5": Rates(
            input=0.10, output=0.50, cache_write_5m=0.125, cache_write_1h=0.20, cache_read=0.01
        ),
    },
}

# provider -> model -> (prompt-token threshold, rates for prompts strictly over it)
LONG_PROMPT_PRICES: dict[str, dict[str, tuple[int, Rates]]] = {
    "anthropic": {
        "claude-haiku-5-5": (
            100_000,
            Rates(
                input=0.50, output=2.50, cache_write_5m=0.625, cache_write_1h=1.00, cache_read=0.05
            ),
        ),
    },
}


def rates_for(provider: str, model: str, prompt_tokens: int) -> Rates:
    """The rate card for one request of `prompt_tokens` prompt tokens."""
    try:
        rates = PRICES[provider][model]
    except KeyError:
        raise UnknownModelError(
            f"no price for {provider}/{model}; add it to swarm.llm.pricing.PRICES"
        ) from None
    long_prompt = LONG_PROMPT_PRICES.get(provider, {}).get(model)
    if long_prompt is not None and prompt_tokens > long_prompt[0]:
        return long_prompt[1]
    return rates


BATCH_DISCOUNT = 0.5  # Message Batches API: 50% of standard prices on every token class


def cost_usd(
    model: str, usage: Usage, *, provider: str, cache_ttl: CacheTTL, batch: bool = False
) -> float:
    """USD cost of one call. Raises UnknownModelError for a model without a price."""
    if cache_ttl not in ("5m", "1h"):
        raise ValueError(f"cache_ttl must be '5m' or '1h', got {cache_ttl!r}")
    rates = rates_for(provider, model, usage.prompt_tokens)
    cache_write = rates.cache_write_1h if cache_ttl == "1h" else rates.cache_write_5m
    per_million = (
        usage.input_tokens * rates.input
        + usage.cache_creation_input_tokens * cache_write
        + usage.cache_read_input_tokens * rates.cache_read
        + usage.output_tokens * rates.output
    )
    return per_million / 1_000_000 * (BATCH_DISCOUNT if batch else 1.0)
