"""Live check that prompt caching works: real, billed calls (well under $0.05 per model).

Runs only when the shell exports SWARM_LIVE=1 (a SWARM_LIVE=1 line in .env is not enough
here), and needs ANTHROPIC_API_KEY in .env or the environment.
"""

import os

import pytest

from swarm.cli import run_ping

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SWARM_LIVE") != "1", reason="live API test: export SWARM_LIVE=1 to run"
    ),
]


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-sonnet-5-5"])
async def test_a_second_identical_call_reads_the_cache(model, tmp_path):
    (first, second), meter = await run_ping(
        provider="anthropic", model=model, budget_usd=0.05, run_dir=tmp_path / model
    )
    # The first call writes the prefix (or reads it, if an earlier ping wrote it within the hour).
    assert first.usage.cache_creation_input_tokens + first.usage.cache_read_input_tokens > 0
    assert second.usage.cache_read_input_tokens > 0
    assert meter.total_usd < 0.05
