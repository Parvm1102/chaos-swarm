"""run_tool_loop driven by a ScriptedClient: no network, same builder and meter as live."""

import asyncio
import copy
from typing import Any

import pytest

from swarm.llm.client import (
    ScriptedClient,
    count_cache_markers,
    scripted_sequence,
    text_response,
    tool_use_response,
    tool_uses_response,
)
from swarm.llm.loop import NOT_EXECUTED, STOP, Stop, ToolError, run_tool_loop
from swarm.llm.meter import BudgetExceeded, read_usage_lines
from swarm.llm.types import ToolSpec, Usage, text_block, user_message

ADD = ToolSpec(
    name="add",
    description="Add two integers.",
    input_schema={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)
SUBMIT = ToolSpec(
    name="submit",
    description="Submit the final answer.",
    input_schema={
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
)
SYSTEM = [text_block("You are a calculator agent.")]
TAGS = {"phase": "task", "agent": "A", "episode": 1}


def add(args: dict[str, Any]) -> str:
    return str(args["a"] + args["b"])


@pytest.fixture
def client_for(make_run, make_meter):
    def make(script) -> ScriptedClient:
        return ScriptedClient(script, run=make_run(), meter=make_meter())

    return make


async def loop(client, *, handlers, tools=(ADD,), max_turns=5, messages=None):
    return await run_tool_loop(
        client,
        system=SYSTEM,
        messages=messages if messages is not None else [user_message("Add 2+3, then 5+7.")],
        tools=list(tools),
        handlers=handlers,
        max_turns=max_turns,
        max_tokens=256,
        tags=TAGS,
    )


async def test_add_twice_then_end(client_for):
    first = tool_use_response("add", {"a": 2, "b": 3})
    second = tool_use_response("add", {"a": 5, "b": 7})
    client = client_for(scripted_sequence(first, second, text_response("5 and 12.")))
    seen: list[tuple[int, int]] = []

    def spy_add(args: dict[str, Any]) -> str:
        seen.append((args["a"], args["b"]))
        return add(args)

    result = await loop(client, handlers={"add": spy_add})

    assert seen == [(2, 3), (5, 7)]  # handlers ran in order
    assert result.stop_cause == "end_turn"
    assert result.turns == 3
    assert result.final_text == "5 and 12."
    roles = [message["role"] for message in result.messages]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    # The assistant turn is the response's blocks, unchanged ...
    first_id = first.tool_calls[0].id
    assert result.messages[1]["content"] == [
        {"type": "tool_use", "id": first_id, "name": "add", "input": {"a": 2, "b": 3}}
    ]
    # ... and its tool result comes back in exactly the Messages API shape.
    assert result.messages[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": first_id, "content": "5"}],
    }
    assert [record.result for record in result.tool_calls] == ["5", "12"]
    # Every call went through the meter, at $0.
    lines = list(read_usage_lines(client.meter.path))
    assert len(lines) == 3
    assert all(line["usd"] == 0 and line["phase"] == "task" for line in lines)


async def test_the_cache_marker_moves_each_turn_but_never_enters_the_history(client_for):
    script = scripted_sequence(
        tool_use_response("add", {"a": 2, "b": 3}),
        tool_use_response("add", {"a": 5, "b": 7}),
        text_response("done"),
    )
    client = client_for(script)
    result = await loop(client, handlers={"add": add})

    assert [len(call.request["messages"]) for call in client.calls] == [1, 3, 5]
    for call in client.calls:
        assert "cache_control" in call.request["messages"][-1]["content"][-1]
        assert count_cache_markers(call.request) == 3  # tool, system, newest message
    assert count_cache_markers({"messages": result.messages}) == 0


async def test_a_script_that_never_ends_stops_at_max_turns(client_for):
    calls = 0

    def script(messages, tools):
        nonlocal calls
        calls += 1
        return tool_use_response("add", {"a": calls, "b": 1})

    client = client_for(script)
    result = await loop(client, handlers={"add": add}, max_turns=4)

    assert result.stop_cause == "max_turns"
    assert calls == result.turns == len(client.calls) == 4
    assert len(result.tool_calls) == 4  # the last turn's tool call still ran
    assert result.messages[-1]["content"][0]["type"] == "tool_result"  # transcript is complete


async def test_stop_ends_the_loop_and_skips_the_rest_of_the_turn(client_for):
    both = tool_uses_response([("submit", {"answer": "42"}), ("add", {"a": 1, "b": 2})])
    client = client_for(scripted_sequence(both))
    added: list[dict[str, Any]] = []

    def submit(args: dict[str, Any]) -> Stop:
        return Stop("Answer recorded.")

    def spy_add(args: dict[str, Any]) -> str:
        added.append(args)
        return add(args)

    result = await loop(client, tools=(SUBMIT, ADD), handlers={"submit": submit, "add": spy_add})

    assert result.stop_cause == "stop_tool"
    assert added == []
    submit_result, add_result = result.messages[-1]["content"]
    assert submit_result == {
        "type": "tool_result",
        "tool_use_id": both.tool_calls[0].id,
        "content": "Answer recorded.",
    }
    assert add_result == {
        "type": "tool_result",
        "tool_use_id": both.tool_calls[1].id,
        "content": NOT_EXECUTED,
        "is_error": True,
    }
    assert [record.executed for record in result.tool_calls] == [True, False]


async def test_the_plain_stop_constant(client_for):
    client = client_for(scripted_sequence(tool_use_response("submit", {"answer": "x"})))
    result = await loop(client, tools=(SUBMIT,), handlers={"submit": lambda args: STOP})
    assert result.stop_cause == "stop_tool"
    assert result.tool_calls[0].result == STOP.content


async def test_tool_error_goes_back_to_the_model_and_the_loop_continues(client_for):
    def careful_add(args: dict[str, Any]) -> str:
        if args["b"] == 0:
            raise ToolError("b must not be zero")
        return add(args)

    bad_call = tool_use_response("add", {"a": 1, "b": 0})
    client = client_for(scripted_sequence(bad_call, text_response("ok")))
    result = await loop(client, handlers={"add": careful_add})

    assert result.stop_cause == "end_turn"
    assert result.messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": bad_call.tool_calls[0].id,
            "content": "b must not be zero",
            "is_error": True,
        }
    ]
    assert result.tool_calls[0].is_error and result.tool_calls[0].executed


async def test_other_handler_exceptions_propagate(client_for):
    def broken(args: dict[str, Any]) -> str:
        raise KeyError("harness bug")

    client = client_for(scripted_sequence(tool_use_response("add", {"a": 1, "b": 2})))
    with pytest.raises(KeyError, match="harness bug"):
        await loop(client, handlers={"add": broken})


async def test_an_unknown_tool_gets_an_error_result(client_for):
    client = client_for(
        scripted_sequence(tool_use_response("subtract", {"a": 1, "b": 2}), text_response("sorry"))
    )
    result = await loop(client, handlers={"add": add})
    record = result.tool_calls[0]
    assert record.is_error and not record.executed
    assert "Unknown tool" in record.result
    assert result.stop_cause == "end_turn"


async def test_async_handlers_and_json_results(client_for):
    async def add_async(args: dict[str, Any]) -> dict[str, int]:
        await asyncio.sleep(0)
        return {"sum": args["a"] + args["b"], "a": args["a"]}

    client = client_for(
        scripted_sequence(tool_use_response("add", {"a": 2, "b": 3}), text_response("5"))
    )
    result = await loop(client, handlers={"add": add_async})
    assert result.tool_calls[0].result == '{"a":2,"sum":5}'  # JSON with sorted keys


async def test_a_declared_tool_without_a_handler_fails_before_any_call(client_for):
    client = client_for(scripted_sequence(text_response("never sent")))
    with pytest.raises(ValueError, match="without a handler"):
        await loop(client, handlers={})
    assert client.calls == []


async def test_the_input_messages_are_not_modified(client_for):
    messages = [user_message("Add 2+3.")]
    snapshot = copy.deepcopy(messages)
    client = client_for(
        scripted_sequence(tool_use_response("add", {"a": 2, "b": 3}), text_response("5"))
    )
    result = await loop(client, handlers={"add": add}, messages=messages)
    assert messages == snapshot
    assert len(result.messages) == 4


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
async def test_a_truncated_or_refused_reply_ends_the_loop(client_for, stop_reason):
    client = client_for(scripted_sequence(text_response("", stop_reason=stop_reason)))
    result = await loop(client, handlers={"add": add})
    assert result.stop_cause == stop_reason
    assert result.turns == 1


async def test_an_unexpected_stop_reason_fails_loudly(client_for):
    client = client_for(scripted_sequence(text_response("", stop_reason="pause_turn")))
    with pytest.raises(RuntimeError, match="pause_turn"):
        await loop(client, handlers={"add": add})


async def test_going_over_budget_stops_the_loop(make_run, make_meter):
    half_a_dollar = Usage(output_tokens=1_000_000)  # Haiku output: 1,000,000 x 0.50 = $0.50

    def script(messages, tools):
        return tool_use_response("add", {"a": 1, "b": 1}, usage=half_a_dollar)

    client = ScriptedClient(script, run=make_run(), meter=make_meter(budget_usd=1.0))
    with pytest.raises(BudgetExceeded):
        await loop(client, handlers={"add": add}, max_turns=10)
    assert len(client.calls) == 3  # $0.50, $1.00 (equal to the cap: allowed), $1.50: stop


async def test_a_refusal_inside_a_tool_call_is_answered_not_executed(client_for):
    started = tool_use_response("add", {"a": 1}, text="Adding.")
    refused = started.model_copy(update={"stop_reason": "refusal"})
    client = client_for(scripted_sequence(refused))
    ran: list[dict[str, Any]] = []
    result = await loop(client, handlers={"add": lambda args: ran.append(args) or "x"})

    assert result.stop_cause == "refusal" and ran == []  # the cut-off call never runs
    record = result.tool_calls[0]
    assert record.is_error and not record.executed and "'refusal'" in record.result
    assert result.messages[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": refused.tool_calls[0].id,
                "content": record.result,
                "is_error": True,
            }
        ],
    }
