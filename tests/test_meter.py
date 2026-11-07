import asyncio

import orjson
import pytest

from swarm.llm.meter import USAGE_FILE, BudgetExceeded, read_usage_lines, summarize_usage_file
from swarm.llm.types import Usage

CALL = Usage(input_tokens=1_000, output_tokens=100)
HAIKU_CALL_USD = 0.00015  # 1,000 x 0.10 + 100 x 0.50 = 150 per million
SONNET_CALL_USD = 0.003  # 1,000 x 2 + 100 x 10 = 3,000 per million


async def test_concurrent_records_sum_exactly(make_meter):
    meter = make_meter(budget_usd=10.0)

    async def one_call(i: int) -> None:
        model = "claude-haiku-5-5" if i % 2 else "claude-sonnet-5-5"
        phase = "task" if i < 10 else "verdict"
        await meter.record(model, CALL, phase=phase, agent=f"agent-{i % 2}", episode=i)

    await asyncio.gather(*(one_call(i) for i in range(20)))

    assert meter.total_usd == pytest.approx(10 * HAIKU_CALL_USD + 10 * SONNET_CALL_USD)
    lines = list(read_usage_lines(meter.path))
    assert len(lines) == 20
    assert sorted(line["episode"] for line in lines) == list(range(20))
    # Each line carries the running total at the moment it was written.
    assert max(line["total_usd"] for line in lines) == pytest.approx(meter.total_usd)

    summary = meter.summary()
    assert summary["calls"] == 20
    assert summary["overall"]["input_tokens"] == 20_000
    assert summary["by_model"]["claude-haiku-5-5"]["calls"] == 10
    assert summary["by_model"]["claude-sonnet-5-5"]["usd"] == pytest.approx(10 * SONNET_CALL_USD)
    assert summary["by_phase"]["task"]["calls"] == summary["by_phase"]["verdict"]["calls"] == 10
    assert summary["by_provider"]["anthropic"]["calls"] == 20


async def test_one_line_per_call_with_every_field(make_meter):
    meter = make_meter()
    usd = await meter.record("claude-haiku-5-5", CALL, phase="task", agent="A", episode=3)
    assert usd == pytest.approx(HAIKU_CALL_USD)
    (line,) = read_usage_lines(meter.path)
    assert set(line) == {
        "ts",
        "provider",
        "model",
        "phase",
        "agent",
        "episode",
        "cache_ttl",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "usd",
        "total_usd",
    }
    assert line["provider"] == "anthropic" and line["model"] == "claude-haiku-5-5"
    assert (line["phase"], line["agent"], line["episode"]) == ("task", "A", 3)
    assert line["cache_ttl"] == "5m" and line["input_tokens"] == 1_000


async def test_budget_exceeded_at_the_call_that_crosses_it(make_meter):
    meter = make_meter(budget_usd=0.01)
    call = Usage(output_tokens=300)  # Sonnet: 300 x 10 per million = $0.003
    for episode in range(3):  # $0.003, $0.006, $0.009: all within $0.01
        await meter.record("claude-sonnet-5-5", call, phase="p", agent="a", episode=episode)
    meter.ensure_within_budget()

    with pytest.raises(BudgetExceeded, match="over"):
        await meter.record("claude-sonnet-5-5", call, phase="p", agent="a", episode=3)  # $0.012

    assert len(list(read_usage_lines(meter.path))) == 4  # the crossing call is still logged
    with pytest.raises(BudgetExceeded):
        meter.ensure_within_budget()  # and no new call may start


async def test_spending_exactly_the_budget_is_allowed(make_meter):
    meter = make_meter(budget_usd=2.0)
    one_dollar = Usage(output_tokens=2_000_000)  # Haiku output: 2,000,000 x 0.50 = exactly $1
    await meter.record("claude-haiku-5-5", one_dollar, phase="p", agent="a", episode=0)
    await meter.record("claude-haiku-5-5", one_dollar, phase="p", agent="a", episode=1)
    assert meter.total_usd == 2.0
    meter.ensure_within_budget()  # equal to the cap is fine; only "over" raises
    with pytest.raises(BudgetExceeded):
        await meter.record("claude-haiku-5-5", one_dollar, phase="p", agent="a", episode=2)


async def test_a_reopened_meter_continues_from_the_file(make_meter):
    first = make_meter(budget_usd=1.0)
    await first.record("claude-haiku-5-5", CALL, phase="task", agent="A", episode=0)
    await first.record("claude-sonnet-5-5", CALL, phase="task", agent="B", episode=0)

    second = make_meter(budget_usd=1.0)
    assert second.total_usd == pytest.approx(first.total_usd)
    assert second.summary()["calls"] == 2


async def test_summarize_usage_file_matches_the_meter(make_meter):
    meter = make_meter()
    await meter.record("claude-haiku-5-5", CALL, phase="task", agent="A", episode=0)
    await meter.record("claude-haiku-5-5", CALL, phase="verdict", agent="A", episode=0)
    from_file = summarize_usage_file(meter.path)
    from_meter = meter.summary()
    for key in ("total_usd", "calls", "overall", "by_provider", "by_model", "by_phase"):
        assert from_file[key] == from_meter[key]


def test_a_run_folder_holds_one_provider(make_meter, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    line = {"provider": "agentrouter", "model": "x", "phase": "p", "usd": 0.0} | dict.fromkeys(
        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
        0,
    )
    (run_dir / USAGE_FILE).write_bytes(orjson.dumps(line) + b"\n")
    with pytest.raises(ValueError, match="one provider"):
        make_meter()


def test_a_damaged_line_is_reported_with_its_number(make_meter, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / USAGE_FILE).write_text('{"provider": "anthropic"\n')
    with pytest.raises(ValueError, match=":1:"):
        make_meter()


def test_budget_must_be_positive(make_meter):
    with pytest.raises(ValueError, match="budget_usd"):
        make_meter(budget_usd=0)


async def test_the_refusal_breaker_trips_at_five_reasoning_extraction_refusals(make_meter):
    from swarm.llm.meter import BREAKER_FILE, RefusalBreakerTripped, breaker_history

    meter = make_meter()
    tags = {"phase": "task", "agent": "A", "episode": 1}
    for _ in range(4):
        await meter.record("claude-haiku-5-5", Usage(), **tags, stop_reason="refusal",
                           refusal_category="reasoning_extraction")  # fmt: skip
    await meter.record("claude-haiku-5-5", Usage(), **tags, stop_reason="end_turn")
    meter.ensure_within_budget()  # 4 refusals: still running
    with pytest.raises(RefusalBreakerTripped, match="5 reasoning_extraction refusals"):
        await meter.record("claude-haiku-5-5", Usage(), **tags, stop_reason="refusal",
                           refusal_category="reasoning_extraction")  # fmt: skip
    with pytest.raises(RefusalBreakerTripped, match="no new calls"):
        meter.ensure_within_budget()
    lines = list(read_usage_lines(meter.path))
    assert [line.get("refusal") for line in lines].count("reasoning_extraction") == 5
    assert "refusal" not in lines[4]
    assert (meter.run_dir / BREAKER_FILE).exists()
    assert breaker_history(meter.run_dir)[0]["refusals"] == {"reasoning_extraction": 5}
    # A resumed run (a new meter on the same folder) starts counting from zero.
    resumed = make_meter()
    resumed.ensure_within_budget()
    assert resumed.refusals == {} and resumed.tripped is None


async def test_the_refusal_breaker_trips_at_ten_refusals_of_any_category(make_meter):
    from swarm.llm.meter import RefusalBreakerTripped

    meter = make_meter()
    tags = {"phase": "judge_coordination", "agent": "pair", "episode": 3}
    categories = ["cyber", "bio", None, "reasoning_extraction"] * 2 + ["cyber"]
    for category in categories:  # 9 refusals, only 2 of them reasoning_extraction
        await meter.record("claude-haiku-5-5", Usage(), **tags, stop_reason="refusal",
                           refusal_category=category)  # fmt: skip
    with pytest.raises(RefusalBreakerTripped, match="10 refusals"):
        await meter.record("claude-haiku-5-5", Usage(), **tags, stop_reason="refusal",
                           refusal_category="bio")  # fmt: skip
    assert meter.tripped["refusals"]["unknown"] == 2


async def test_a_run_may_lower_the_breaker_but_never_raise_it(tmp_path):
    from swarm.llm.meter import CostMeter, RefusalBreakerTripped

    strict = CostMeter(
        tmp_path / "pilot",
        1.0,
        provider="anthropic",
        cache_ttl="5m",
        breaker_category_limits={"reasoning_extraction": 1},
        breaker_any_limit=1,
    )
    with pytest.raises(RefusalBreakerTripped, match="1 refusals"):
        await strict.record("claude-sonnet-5", Usage(), phase="judge", agent="pair", episode=1,
                            stop_reason="refusal", refusal_category="cyber")  # fmt: skip
    with pytest.raises(ValueError, match="only be lowered"):
        CostMeter(tmp_path / "x", 1.0, provider="anthropic", cache_ttl="5m", breaker_any_limit=11)
    with pytest.raises(ValueError, match="only be lowered"):
        CostMeter(tmp_path / "y", 1.0, provider="anthropic", cache_ttl="5m",
                  breaker_category_limits={"reasoning_extraction": 6})  # fmt: skip


def test_the_sonnet_5_pilot_config_stops_at_the_first_refusal():
    from pathlib import Path

    from swarm.analysis.judges import JudgeConfig

    root = Path(__file__).resolve().parents[1] / "configs" / "judges"
    pilot = JudgeConfig.load(root / "sonnet5.yaml")
    assert (pilot.model.name, pilot.model.thinking, pilot.concurrency, pilot.budget_usd) == (
        "claude-sonnet-5", "off", 2, 1.0,
    )  # fmt: skip
    assert (pilot.breaker_reasoning_extraction, pilot.breaker_any) == (1, 1)
    full = JudgeConfig.load(root / "sonnet5_full.yaml")
    assert (full.judge, full.concurrency, full.budget_usd, full.breaker_any) == (
        "sonnet5",
        5,
        4.0,
        10,
    )
