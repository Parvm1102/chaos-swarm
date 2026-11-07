"""The request builder, tested without the network: where the cache markers land, and the
request rules the API enforces (thinking switch, no sampling parameters, no forced tools)."""

import copy
from typing import Any

import pytest

from swarm.config import ModelConfig
from swarm.llm.client import MAX_CACHE_MARKERS, build_request_kwargs, count_cache_markers
from swarm.llm.types import ToolSpec, text_block, user_message

HAIKU = ModelConfig(name="claude-haiku-5-5")
SONNET = ModelConfig(name="claude-sonnet-5-5")
OPUS = ModelConfig(name="claude-opus-5-5")


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"The {name} tool.",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )


TOOLS = [_tool("add"), _tool("mul")]
SYSTEM = [text_block("You are agent A."), text_block("Follow the verification rule.")]
HISTORY: list[dict[str, Any]] = [
    user_message("Episode 1: solve the task."),
    {
        "role": "assistant",
        "content": [
            text_block("Calling add."),
            {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"x": 1}},
        ],
    },
    {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "2"}],
    },
]


def build(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "model": HAIKU,
        "system": SYSTEM,
        "messages": HISTORY,
        "tools": TOOLS,
        "tool_choice": None,
        "max_tokens": 256,
        "cache_ttl": "1h",
        "cache_until": None,
    }
    args.update(overrides)
    return build_request_kwargs(**args)


def marked(request: dict[str, Any]) -> list[str]:
    """Where the cache markers are, as readable paths."""
    where = [f"tools[{i}]" for i, t in enumerate(request.get("tools", [])) if "cache_control" in t]
    where += [f"system[{i}]" for i, b in enumerate(request["system"]) if "cache_control" in b]
    for m, message in enumerate(request["messages"]):
        for b, block in enumerate(message["content"]):
            if "cache_control" in block:
                where.append(f"messages[{m}][{b}]")
    return where


def markers(request: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [*request.get("tools", []), *request["system"]]
    blocks += [block for message in request["messages"] for block in message["content"]]
    return [block["cache_control"] for block in blocks if "cache_control" in block]


def test_markers_on_last_tool_last_system_block_and_last_message():
    request = build()
    assert marked(request) == ["tools[1]", "system[1]", "messages[2][0]"]
    assert markers(request) == [{"type": "ephemeral", "ttl": "1h"}] * 3


def test_marker_ttl_comes_from_the_run_config():
    assert markers(build(cache_ttl="5m")) == [{"type": "ephemeral", "ttl": "5m"}] * 3


def test_cache_until_marks_that_message_and_nothing_after_it():
    # messages[1] ends with a tool_use block, which can carry a marker.
    assert marked(build(cache_until=1)) == ["tools[1]", "system[1]", "messages[1][1]"]
    assert marked(build(cache_until=0)) == ["tools[1]", "system[1]", "messages[0][0]"]


def test_string_content_is_always_sent_as_one_text_block():
    reply = {"role": "assistant", "content": [text_block("Noted.")]}
    request = build(messages=[user_message("first"), reply, user_message("last")])
    assert request["messages"][0]["content"] == [{"type": "text", "text": "first"}]  # unmarked
    assert request["messages"][2]["content"] == [
        {"type": "text", "text": "last", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]


def test_marker_skips_thinking_and_empty_text_blocks():
    assistant = {
        "role": "assistant",
        "content": [
            text_block("Checking."),
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": ""},
        ],
    }
    request = build(messages=[user_message("go"), assistant])
    assert marked(request)[-1] == "messages[1][0]"


def test_a_message_with_nothing_markable_is_rejected():
    only_thinking = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}],
    }
    with pytest.raises(ValueError, match="no block that can carry a cache marker"):
        build(messages=[user_message("go"), only_thinking])


@pytest.mark.parametrize("cache_until", [None, 0, 1, 2])
@pytest.mark.parametrize("tools", [None, TOOLS[:1], TOOLS])
def test_never_more_than_four_markers(cache_until, tools):
    request = build(tools=tools, cache_until=cache_until)
    assert count_cache_markers(request) == (3 if tools else 2)
    assert count_cache_markers(request) <= MAX_CACHE_MARKERS


def test_a_long_history_still_gets_one_message_marker():
    history = [
        user_message(f"turn {i}") if i % 2 == 0 else {"role": "assistant", "content": f"reply {i}"}
        for i in range(41)
    ]
    request = build(messages=history)
    assert count_cache_markers(request) == 3
    assert marked(request)[-1] == "messages[40][0]"


def test_inputs_are_never_modified():
    system, history = copy.deepcopy(SYSTEM), copy.deepcopy(HISTORY)
    build(system=system, messages=history, tools=TOOLS)
    assert system == SYSTEM
    assert history == HISTORY
    assert all("cache_control" not in tool.to_anthropic() for tool in TOOLS)


def test_caller_placed_markers_are_rejected():
    with pytest.raises(ValueError, match="cache_control"):
        build(system=[text_block("x") | {"cache_control": {"type": "ephemeral"}}])
    marked_message = {"role": "user", "content": [text_block("hi") | {"cache_control": {}}]}
    with pytest.raises(ValueError, match="cache_control"):
        build(messages=[marked_message])


@pytest.mark.parametrize("cache_until", [-1, 3, 99])
def test_cache_until_out_of_range_is_rejected(cache_until):
    with pytest.raises(ValueError, match="cache_until"):
        build(cache_until=cache_until)


def test_thinking_off_switch_per_model():
    assert build(model=SONNET)["thinking"] == {"type": "between_tools"}
    assert build(model=HAIKU)["thinking"] == {"type": "disabled"}
    assert "thinking" not in build(model=OPUS)  # Opus 5.5 cannot turn thinking off


@pytest.mark.parametrize("model", [HAIKU, SONNET, OPUS])
def test_request_has_only_the_expected_keys(model):
    request = build(model=model)
    expected = {"model", "max_tokens", "system", "messages", "tools"}
    if model is not OPUS:
        expected.add("thinking")
    assert set(request) == expected  # in particular: no temperature, top_p or top_k
    assert request["model"] == model.name and request["max_tokens"] == 256


def test_forced_tool_choice_is_rejected_where_the_api_would_400():
    for model in (SONNET, OPUS):
        for choice in ({"type": "any"}, {"type": "tool", "name": "add"}):
            with pytest.raises(ValueError, match="forced"):
                build(model=model, tool_choice=choice)
    assert build(model=HAIKU, tool_choice={"type": "any"})["tool_choice"] == {"type": "any"}
    assert build(model=SONNET, tool_choice={"type": "auto"})["tool_choice"] == {"type": "auto"}


def test_tool_choice_needs_tools():
    with pytest.raises(ValueError, match="without tools"):
        build(tools=None, tool_choice={"type": "auto"})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": ModelConfig(name="claude-sonnet-4-6")}, "no request rules"),
        ({"messages": []}, "messages"),
        ({"messages": [user_message("")]}, "empty"),
        ({"system": []}, "system"),
        ({"system": [text_block("")]}, "system"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"tools": [TOOLS[0], TOOLS[0]]}, "duplicate"),
    ],
)
def test_requests_the_api_would_reject_fail_early(overrides, message):
    with pytest.raises(ValueError, match=message):
        build(**overrides)


def test_a_tool_use_without_its_result_is_refused_before_any_call():
    unanswered = [
        *HISTORY[:2],
        user_message("The next phase begins."),  # no tool_result for toolu_1
    ]
    with pytest.raises(ValueError, match=r"messages\[1\] has tool_use ids without a tool_result"):
        build(messages=unanswered)
    answered = [*HISTORY, user_message("The next phase begins.")]  # result first, then text
    assert build(messages=answered)["messages"][-1]["role"] == "user"


def test_adaptive_thinking_with_an_effort_level():
    adaptive = ModelConfig(name="claude-sonnet-5-5", thinking="adaptive", effort="low")
    request = build(model=adaptive)
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}
    assert "output_config" not in build(model=SONNET)  # the default sends neither
    assert build(model=SONNET)["thinking"] == {"type": "between_tools"}
