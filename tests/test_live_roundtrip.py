"""Live check of one tool round-trip per model: proves the API accepts our replayed assistant
blocks (thinking blocks included) and each model's thinking-off setting. About $0.01 in total.

Runs only when the shell exports SWARM_LIVE=1, like test_live_cache.py.
"""

import os

import pytest

from swarm.config import ModelConfig, RunConfig
from swarm.llm.client import AnthropicClient
from swarm.llm.loop import run_tool_loop
from swarm.llm.meter import CostMeter
from swarm.llm.types import ToolSpec, text_block, user_message

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SWARM_LIVE") != "1", reason="live API test: export SWARM_LIVE=1 to run"
    ),
]

ADD = ToolSpec(
    name="add",
    description="Add two integers and return their sum.",
    input_schema={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-sonnet-5-5"])
async def test_a_tool_round_trip(model, tmp_path):
    run = RunConfig(
        run_name=f"live-roundtrip-{model}",
        model=ModelConfig(name=model, max_tokens=512),
        budget_usd=0.05,
        dry_run=False,
        seed=0,
    )
    meter = CostMeter(tmp_path, run.budget_usd, provider=run.provider, cache_ttl=run.cache_ttl)
    async with AnthropicClient(run, meter) as client:
        result = await run_tool_loop(
            client,
            system=[
                text_block(
                    "You are a careful calculator. For any arithmetic, call the add tool, "
                    "then reply with just the resulting number."
                )
            ],
            messages=[user_message("What is 1234 + 4321?")],
            tools=[ADD],
            handlers={"add": lambda args: str(args["a"] + args["b"])},
            max_turns=4,
            max_tokens=512,
            tags={"phase": "live-test", "agent": "calculator", "episode": 0},
        )
    assert result.stop_cause == "end_turn"
    assert any(record.call.name == "add" for record in result.tool_calls)
    assert "5555" in result.final_text.replace(",", "")
    assert meter.total_usd < 0.05
