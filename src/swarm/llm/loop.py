"""The generic tool loop: call the model, run the tools it asks for, repeat.

One turn is one model call. After each call:
- stop_reason "tool_use": run each requested tool in order, send all the results back in one
  user message, and call the model again;
- "end_turn", "max_tokens" or "refusal": stop, and report which.
A handler can also end the loop by returning STOP, and the loop always ends after max_turns
calls. On the last allowed turn the requested tools still run (so a final submit counts). A
response that ends any other way while holding tool_use blocks (a refusal can stop mid-call) gets
"not executed" results for them, so a finished transcript always has every tool_use answered.

Handlers are harness code. A handler that raises ToolError is reporting a mistake by the
*model* (bad arguments, an action not allowed now): the message goes back as an is_error
tool_result and the loop continues. Any other exception is a harness bug and propagates.

The task phase (step 2) and the verdict phase (step 3) both run on this loop.
"""

import copy
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import orjson

from swarm.llm.client import LLMClient, Tags
from swarm.llm.types import (
    LLMResponse,
    ToolCall,
    ToolSpec,
    assistant_message,
    tool_result_block,
    tool_results_message,
)

StopCause = Literal["end_turn", "stop_tool", "max_turns", "max_tokens", "refusal"]

# Stop reasons that end the loop by themselves. Anything else except "tool_use" is unexpected.
_ENDING_STOP_REASONS: dict[str, StopCause] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}

NOT_EXECUTED = "Not executed: an earlier tool call in this turn ended the loop."


@dataclass(frozen=True)
class Stop:
    """Return this from a handler to end the loop after the current tool call.

    `content` goes back as that call's tool_result, so the transcript stays complete.
    """

    content: str = "Received."


STOP = Stop()


class ToolError(Exception):
    """Raised by a handler when the model used a tool wrongly. It goes back to the model as an
    is_error tool_result, and the loop continues."""


HandlerResult = str | dict[str, Any] | list[Any] | Stop
Handler = Callable[[dict[str, Any]], HandlerResult | Awaitable[HandlerResult]]


@dataclass(frozen=True)
class ToolCallRecord:
    turn: int  # the 1-based model call that requested it
    call: ToolCall
    result: str  # the tool_result text sent back to the model
    is_error: bool
    executed: bool  # False for an unknown tool, or a call skipped after STOP


@dataclass(frozen=True)
class LoopResult:
    messages: list[dict[str, Any]]  # the input messages plus everything the loop appended
    tool_calls: list[ToolCallRecord]
    responses: list[LLMResponse]
    stop_cause: StopCause

    @property
    def turns(self) -> int:
        return len(self.responses)

    @property
    def final_text(self) -> str:
        return self.responses[-1].text if self.responses else ""


async def run_tool_loop(
    client: LLMClient,
    *,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[ToolSpec] | None,
    handlers: Mapping[str, Handler],
    max_turns: int,
    max_tokens: int,
    tags: Tags,
    tool_choice: dict[str, Any] | None = None,
) -> LoopResult:
    """Run the model with tools until it stops, a handler returns STOP, or max_turns calls."""
    if max_turns < 1:
        raise ValueError(f"max_turns must be >= 1, got {max_turns}")
    missing = sorted({tool.name for tool in tools or []} - set(handlers))
    if missing:
        raise ValueError(f"tools without a handler: {missing}")

    history = copy.deepcopy(messages)  # the caller's list is never modified
    records: list[ToolCallRecord] = []
    responses: list[LLMResponse] = []
    for turn in range(1, max_turns + 1):
        response = await client.complete(
            system=system,
            messages=history,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            tags=tags,
        )
        responses.append(response)
        history.append(assistant_message(response))

        if response.stop_reason != "tool_use":
            cause = _ENDING_STOP_REASONS.get(response.stop_reason)
            if cause is None:
                raise RuntimeError(
                    f"unexpected stop_reason {response.stop_reason!r} on turn {turn}"
                )
            if response.tool_calls:
                # A refusal (or a max_tokens cut) can stop a response after a tool_use block was
                # started. Its input may be partial, so it never runs; it is answered "not
                # executed", so the history stays valid for the next request without editing it.
                unanswered = [
                    ToolCallRecord(
                        turn, call, _not_executed(response.stop_reason), True, executed=False
                    )
                    for call in response.tool_calls
                ]
                records.extend(unanswered)
                history.append(
                    tool_results_message(
                        [tool_result_block(r.call.id, r.result, is_error=True) for r in unanswered]
                    )
                )
            return LoopResult(history, records, responses, cause)
        if not response.tool_calls:
            raise RuntimeError(f"stop_reason 'tool_use' without a tool call on turn {turn}")

        results: list[dict[str, Any]] = []
        stopped = False
        for call in response.tool_calls:
            if stopped:
                record = ToolCallRecord(turn, call, NOT_EXECUTED, is_error=True, executed=False)
            else:
                record, stopped = await _run_tool(turn, call, handlers)
            records.append(record)
            results.append(tool_result_block(call.id, record.result, is_error=record.is_error))
        history.append(tool_results_message(results))
        if stopped:
            return LoopResult(history, records, responses, "stop_tool")

    return LoopResult(history, records, responses, "max_turns")


async def _run_tool(
    turn: int, call: ToolCall, handlers: Mapping[str, Handler]
) -> tuple[ToolCallRecord, bool]:
    """Run one tool call. Returns its record, and whether the handler asked to stop."""
    handler = handlers.get(call.name)
    if handler is None:
        unknown = f"Unknown tool: {call.name!r}"
        return ToolCallRecord(turn, call, unknown, is_error=True, executed=False), False
    try:
        result = handler(copy.deepcopy(call.input))
        if inspect.isawaitable(result):
            result = await result
    except ToolError as error:
        return ToolCallRecord(turn, call, str(error), is_error=True, executed=True), False
    if isinstance(result, Stop):
        return ToolCallRecord(turn, call, result.content, is_error=False, executed=True), True
    return ToolCallRecord(turn, call, _as_text(result), is_error=False, executed=True), False


def _not_executed(stop_reason: str) -> str:
    return f"Not executed: the response ended with stop reason {stop_reason!r}."


def _as_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict | list):
        return orjson.dumps(result, option=orjson.OPT_SORT_KEYS).decode()
    raise TypeError(f"a handler returned {type(result).__name__}; expected str, dict, list or Stop")
