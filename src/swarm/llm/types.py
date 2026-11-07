"""Plain data types for model calls. No SDK objects get past swarm.llm.client.

Messages are plain dicts in the Anthropic Messages API shape, so a transcript is
JSON-serialisable and can be sent back to the API exactly as stored.
"""

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolSpec(BaseModel):
    """A tool the model may call. `input_schema` is a JSON Schema object.

    `strict=True` asks the API to guarantee that the call's input validates against the schema.
    A strict schema needs `additionalProperties: false` and `required` on every object, and may
    not use numeric, string-length or complex array constraints (minimum, maxLength, minItems,
    ...); handlers check those themselves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = False

    def to_anthropic(self) -> dict[str, Any]:
        tool = {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
        }
        if self.strict:
            tool["strict"] = True
        return tool


class ToolCall(BaseModel):
    """One tool_use block from a response."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    input: dict[str, Any]


class Usage(BaseModel):
    """Token counts for one call. `input_tokens` is only the uncached part of the prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)

    @property
    def prompt_tokens(self) -> int:
        """The whole prompt: uncached input + cache writes + cache reads."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


class LLMResponse(BaseModel):
    """One model reply, converted from the SDK.

    `content` holds the assistant blocks exactly as the API returned them, thinking blocks
    included. It is what goes back into the history, because the API requires thinking blocks
    to be passed back unchanged. `text` and `tool_calls` are convenience views of it.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str
    usage: Usage = Field(default_factory=Usage)
    content: list[dict[str, Any]]
    raw: dict[str, Any] = Field(default_factory=dict)


# Message builders, in the Messages API shape.


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def user_message(content: str | list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": content}


def assistant_message(response: LLMResponse) -> dict[str, Any]:
    """The assistant turn for the history: the response's blocks, unchanged."""
    return {"role": "assistant", "content": copy.deepcopy(response.content)}


def tool_result_block(tool_use_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def tool_results_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """All tool results for one assistant turn go back together, in a single user message."""
    return {"role": "user", "content": list(blocks)}
