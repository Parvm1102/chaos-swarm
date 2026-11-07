"""run_task_phase with scripted clients: what is sent, and how each ending is recorded."""

import pytest

from swarm.agents.task_agent import MAX_TURNS, TASK_MAX_TOKENS, run_task_phase
from swarm.calibrate import scripted_solver
from swarm.config import ModelConfig
from swarm.env.database import TASK_TOOLS_VERSION, TaskEnv
from swarm.llm.client import ScriptedClient, scripted_sequence, text_response, tool_use_response
from swarm.llm.types import Usage
from swarm.prompts import load_prompt

TAGS = {"phase": "task", "agent": "solo", "episode": 0}


@pytest.fixture
def sonnet_run(make_run):
    return make_run(model={"name": "claude-sonnet-5-5", "max_tokens": TASK_MAX_TOKENS})


async def solve(task_dir, task_id, script, run, meter):
    client = ScriptedClient(script, run=run, meter=meter)
    with TaskEnv.open(task_dir, task_id) as env:
        result = await run_task_phase(client, env, env.spec.nl_request, run.model, TAGS)
    return result, client, env


async def test_the_scripted_solver_gives_a_correct_result(task_dir, sonnet_run, make_meter):
    with TaskEnv.open(task_dir, "task_0001") as env:
        script = scripted_solver(env.spec)
    result, _, env = await solve(task_dir, "task_0001", script, sonnet_run, make_meter())

    assert result.outcome.correct and result.outcome.answer_saved
    assert result.outcome.saved_ids == env.spec.reference_ids
    assert result.stop_cause == "end_turn" and result.turns == 4
    assert result.outcome.query_count == 1 and not result.outcome.peeked
    assert [entry["tool"] for entry in result.log] == [
        "inspect_database",
        "query_database",
        "save_final_answer",
    ]
    assert result.prompt_version == load_prompt("task_phase_system").version
    assert result.tools_version == TASK_TOOLS_VERSION
    assert result.model == "claude-sonnet-5-5"
    assert result.thinking == {"type": "between_tools"}
    assert result.messages[0] == {"role": "user", "content": env.spec.nl_request}
    assert result.messages[-1]["role"] == "assistant"


async def test_what_the_first_request_contains(task_dir, sonnet_run, make_meter):
    with TaskEnv.open(task_dir, "task_0002") as env:
        script = scripted_solver(env.spec)
    _, client, env = await solve(task_dir, "task_0002", script, sonnet_run, make_meter())
    request = client.calls[0].request

    assert request["system"][0]["text"] == load_prompt("task_phase_system").text
    assert request["messages"][0]["content"][0]["text"] == env.spec.nl_request  # verbatim
    assert request["tool_choice"] == {"type": "auto"}
    assert request["thinking"] == {"type": "between_tools"}
    assert request["max_tokens"] == TASK_MAX_TOKENS
    tools = {tool["name"]: tool for tool in request["tools"]}
    assert list(tools) == [
        "inspect_database",
        "query_database",
        "save_final_answer",
        "get_work_record",
    ]
    assert tools["save_final_answer"]["strict"] is True
    assert tools["save_final_answer"]["input_schema"]["additionalProperties"] is False
    assert all("strict" not in tools[name] for name in tools if name != "save_final_answer")
    assert client.calls[0].tags == TAGS


async def test_haiku_runs_with_thinking_disabled(task_dir, make_run, make_meter):
    run = make_run(model={"name": "claude-haiku-5-5", "max_tokens": TASK_MAX_TOKENS})
    with TaskEnv.open(task_dir, "task_0003") as env:
        script = scripted_solver(env.spec)
    result, client, _ = await solve(task_dir, "task_0003", script, run, make_meter())
    assert result.outcome.correct
    assert result.thinking == {"type": "disabled"}
    assert client.calls[0].request["thinking"] == {"type": "disabled"}


async def test_an_agent_that_never_saves_has_no_answer(task_dir, sonnet_run, make_meter):
    script = scripted_sequence(
        tool_use_response("inspect_database", {}), text_response("I could not work it out.")
    )
    result, _, _ = await solve(task_dir, "task_0000", script, sonnet_run, make_meter())
    assert result.stop_cause == "end_turn" and result.turns == 2
    assert not result.outcome.answer_saved and not result.outcome.correct
    assert result.outcome.saved_ids is None


async def test_running_out_of_turns_is_recorded_and_not_reprompted(
    task_dir, sonnet_run, make_meter
):
    def script(messages, tools):
        return tool_use_response("query_database", {"sql": "SELECT 1"})

    result, client, _ = await solve(task_dir, "task_0000", script, sonnet_run, make_meter())
    assert result.stop_cause == "max_turns"
    assert result.turns == len(client.calls) == MAX_TURNS
    assert result.outcome.query_count == MAX_TURNS
    assert not result.outcome.answer_saved


async def test_a_refusal_ends_the_phase_without_an_answer(task_dir, sonnet_run, make_meter):
    script = scripted_sequence(text_response("", stop_reason="refusal"))
    result, _, _ = await solve(task_dir, "task_0000", script, sonnet_run, make_meter())
    assert result.stop_cause == "refusal" and not result.outcome.answer_saved


async def test_usage_and_cost_are_summed_over_the_calls(task_dir, sonnet_run, make_meter):
    usage = Usage(input_tokens=1_000, output_tokens=100, cache_read_input_tokens=2_000)
    script = scripted_sequence(
        tool_use_response("inspect_database", {}, usage=usage), text_response("done", usage=usage)
    )
    meter = make_meter()
    result, _, _ = await solve(task_dir, "task_0000", script, sonnet_run, meter)
    assert result.usage == Usage(
        input_tokens=2_000, output_tokens=200, cache_read_input_tokens=4_000
    )
    # Sonnet 5.5: input $2, output $10, cache read $0.10 per million tokens.
    assert result.cost_usd == pytest.approx(2 * (1_000 * 2 + 100 * 10 + 2_000 * 0.10) / 1e6)
    assert result.cost_usd == pytest.approx(meter.total_usd)


async def test_the_model_config_must_match_the_client(task_dir, sonnet_run, make_meter):
    client = ScriptedClient(scripted_sequence(), run=sonnet_run, meter=make_meter())
    with TaskEnv.open(task_dir, "task_0000") as env, pytest.raises(ValueError, match="model_cfg"):
        await run_task_phase(
            client, env, env.spec.nl_request, ModelConfig(name="claude-haiku-5-5"), TAGS
        )
