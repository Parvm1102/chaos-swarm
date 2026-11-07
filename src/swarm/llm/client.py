"""Model clients: the live Anthropic client, the scripted dry-run client, and the request
builder they share.

Prompt-cache layout
-------------------
The API reads a request as one long prefix, always in this order: tools, then system, then
messages. A ``cache_control`` marker on a block caches the whole prefix up to and including
that block. A later request whose prefix is byte-identical up to a marker reads that part from
the cache (0.05x-0.1x of the input price) instead of paying the full input price.

    |<------------------------- cached prefix -------------------------->|
    [ tool_1 .. tool_t (1) ][ system_1 .. system_s (2) ][ m_0 .. m_k (3) ][ m_k+1 .. m_n ]
      tools                   system blocks               stable history    new turns
                                                          k = cache_until   (not cached)

    (1) the last tool definition: caches the tool set
    (2) the last system block: caches tools + system (an agent's fixed instructions)
    (3) the last block of messages[cache_until] that can carry a marker: caches tools + system
        + stable history (e.g. memory from earlier episodes). cache_until defaults to the last
        message, which is what a tool loop wants.

Every marker is ``{"type": "ephemeral", "ttl": run.cache_ttl}``. We place at most three (the
API allows four). Thinking blocks and empty text blocks cannot carry a marker, so (3) walks back
to the last eligible block of its message. A prefix shorter than the model's minimum (512 tokens
on the 5.5 models) is silently not cached. Markers go on a deep copy of the request, so stored
history never contains them; in a tool loop (3) moves to the newest message every turn, and
each turn reads the previous turn's prefix from the cache. String message content is sent as one
text block, so a message renders the same whether or not it carries the marker that turn.

Other request rules (from the API docs, checked 2026-11-06)
- ``temperature`` is never sent: SDK 2.x and API version 2026-10-21 reject it at any value.
- v1 runs without extended thinking, but Sonnet 5.5 and Haiku 5.5 think by default, so each
  model gets its off switch (``THINKING_OFF``). Opus 5.5 cannot turn thinking off at all.
- Sonnet 5.5 and Opus 5.5 answer a forced ``tool_choice`` (``any`` / ``tool``) with a 400, so
  the builder refuses it up front and a dry-run catches it.
- No refusal fallbacks: a fallback re-runs a declined request on a *different* model, which
  would mix models inside a trajectory. A refusal comes back as ``stop_reason == "refusal"``.
- Structured output (the step-5 judges): ``output_format`` is sent as ``output_config.format``
  (``{"type": "json_schema", "schema": ...}``), beside any effort. The first text block of the
  reply is then JSON that validates against the schema, unless the call stopped early (refusal,
  max_tokens).

Retries: tenacity is the only retry layer (the SDK's own retries are off). It retries 429, 5xx
(including 529 overloaded) and network errors or timeouts with jittered exponential backoff
(1 s doubling to 60 s, or the server's ``retry-after`` when longer), at most 8 attempts, and
logs every retry at WARNING. Other 4xx errors are never retried: they are bugs or bad config.

Pacing: at most ``run.concurrency`` calls are in flight at once (a semaphore), and every attempt,
retries included, first waits for the provider's ``Pacer``, which spaces request starts at least
``PACING_S[provider]`` seconds apart. Anthropic needs no spacing (0 s); AgentRouter will.
"""

import asyncio
import copy
import itertools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, Self, TypedDict

import anthropic
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from swarm.config import CacheTTL, LiveCallRefused, ModelConfig, RunConfig, Settings
from swarm.llm.meter import CostMeter
from swarm.llm.types import LLMResponse, ToolCall, ToolSpec, Usage

log = logging.getLogger(__name__)

MAX_CACHE_MARKERS = 4  # the API's limit per request

# What each model needs to hear for "no extended thinking" (v1). Sonnet 5.5 and Haiku 5.5 run
# adaptive thinking when `thinking` is omitted. None means the model cannot turn thinking off
# (Opus 5.5): `thinking` is omitted and it thinks adaptively, a documented deviation.
THINKING_OFF: dict[str, dict[str, str] | None] = {
    "claude-sonnet-5-5": {"type": "between_tools"},  # its lowest setting: no extended thinking
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-opus-5-5": None,
}

# Models that answer a forced tool_choice ({"type": "any"} or {"type": "tool"}) with a 400.
FORCED_TOOL_CHOICE_REJECTED = frozenset({"claude-sonnet-5-5", "claude-opus-5-5"})

# Content blocks that can never carry a cache marker (empty text blocks can't either).
_UNMARKABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    anthropic.RateLimitError,  # 429
    anthropic.InternalServerError,  # 5xx, including 529 OverloadedError
    anthropic.APIConnectionError,  # network failures, including APITimeoutError
)
MAX_ATTEMPTS = 8
MAX_WAIT_S = 60.0
REQUEST_TIMEOUT_S = 120.0

# Minimum seconds between the starts of two requests, per provider. Anthropic needs none: the
# in-flight semaphore and the 429 backoff are enough. AgentRouter will need about 2.5 s.
PACING_S: dict[str, float] = {"anthropic": 0.0}

RetryWait = Callable[[RetryCallState], float]


class Tags(TypedDict):
    """Who is calling. Every usage line records these."""

    phase: str
    agent: str
    episode: int


class LLMClient(Protocol):
    """What the tool loop and every later phase call: AnthropicClient (live) or
    ScriptedClient (dry-run). `run` says which provider, model and cache TTL the calls use."""

    run: RunConfig

    async def complete(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        tool_choice: dict[str, Any] | None,
        max_tokens: int,
        tags: Tags,
        cache_until: int | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


# The request builder (pure) ---------------------------------------------------------------


def build_request_kwargs(
    *,
    model: ModelConfig,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[ToolSpec] | None,
    tool_choice: dict[str, Any] | None,
    max_tokens: int,
    cache_ttl: CacheTTL,
    cache_until: int | None = None,
    output_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact keyword arguments for ``messages.create``.

    Pure: no network, and the inputs are never modified (markers go on deep copies).
    Raises ValueError for a request the API would reject.
    """
    if model.name not in THINKING_OFF:
        raise ValueError(f"no request rules for model {model.name!r}; add it to THINKING_OFF")
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")
    if not system or any(b.get("type") != "text" or not b.get("text") for b in system):
        raise ValueError("system must be a non-empty list of non-empty text blocks")
    if not messages:
        raise ValueError("messages must not be empty")
    for block in _content_blocks({"system": system, "messages": messages}):
        if "cache_control" in block:
            raise ValueError("a block already has cache_control; the client places all markers")
    _check_tool_results_follow(messages)
    if tool_choice is not None and not tools:
        raise ValueError("tool_choice was given without tools")
    if (
        tool_choice is not None
        and tool_choice.get("type") in ("any", "tool")
        and model.name in FORCED_TOOL_CHOICE_REJECTED
    ):
        raise ValueError(
            f"{model.name} rejects a forced tool_choice {tool_choice!r}; use "
            "{'type': 'auto'} and ask for the tool in the prompt"
        )

    marker = {"type": "ephemeral", "ttl": cache_ttl}

    system_blocks = copy.deepcopy(system)
    system_blocks[-1]["cache_control"] = dict(marker)  # (2)

    history = [_with_block_content(message, i) for i, message in enumerate(messages)]
    index = len(history) - 1 if cache_until is None else cache_until
    if not 0 <= index < len(history):
        raise ValueError(f"cache_until={cache_until} is outside messages[0..{len(history) - 1}]")
    _mark_last_markable_block(history[index], index, marker)  # (3)

    request: dict[str, Any] = {
        "model": model.name,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": history,
    }
    if tools:
        tool_defs = [tool.to_anthropic() for tool in tools]
        names = [tool["name"] for tool in tool_defs]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tool names: {names}")
        tool_defs[-1]["cache_control"] = dict(marker)  # (1)
        request["tools"] = tool_defs
    if tool_choice is not None:
        request["tool_choice"] = copy.deepcopy(tool_choice)
    thinking = thinking_config(model)
    if thinking is not None:
        request["thinking"] = dict(thinking)
    output_config: dict[str, Any] = {}
    if model.effort is not None:
        output_config["effort"] = model.effort
    if output_format is not None:
        if output_format.get("type") != "json_schema" or "schema" not in output_format:
            raise ValueError("output_format must be {'type': 'json_schema', 'schema': {...}}")
        output_config["format"] = copy.deepcopy(output_format)
    if output_config:
        request["output_config"] = output_config

    markers = count_cache_markers(request)
    if markers > MAX_CACHE_MARKERS:  # impossible by construction; the API would answer 400
        raise RuntimeError(f"{markers} cache markers in one request (the API allows 4)")
    return request


def thinking_config(model: ModelConfig) -> dict[str, str] | None:
    """The `thinking` parameter sent for this model config (None: the parameter is omitted)."""
    if model.thinking == "adaptive":
        return {"type": "adaptive"}
    return THINKING_OFF[model.name]


def thinking_provenance(model: ModelConfig) -> dict[str, str] | None:
    """What a record stores about thinking: the `thinking` parameter sent, plus any effort."""
    sent = thinking_config(model)
    if sent is None and model.effort is None:
        return None
    return {**(sent or {}), **({"effort": model.effort} if model.effort else {})}


def _check_tool_results_follow(messages: list[dict[str, Any]]) -> None:
    """The API's rule: every tool_use block must be answered by a tool_result in the very next
    message. Checked here, in the builder both clients share, so a dry run catches a history the
    API would reject with a 400 (and a live run fails before it spends anything)."""
    for index, message in enumerate(messages):
        if message["role"] != "assistant" or not isinstance(message["content"], list):
            continue
        ids = [block["id"] for block in message["content"] if block.get("type") == "tool_use"]
        if not ids:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        answered = set()
        if following and following["role"] == "user" and isinstance(following["content"], list):
            answered = {
                block.get("tool_use_id")
                for block in following["content"]
                if block.get("type") == "tool_result"
            }
        missing = [tool_id for tool_id in ids if tool_id not in answered]
        if missing:
            raise ValueError(
                f"messages[{index}] has tool_use ids without a tool_result in the next message: "
                f"{missing}"
            )


def count_cache_markers(request: dict[str, Any]) -> int:
    """How many blocks of a request (tools, system, message content) carry cache_control."""
    return sum(1 for block in _content_blocks(request) if "cache_control" in block)


def _content_blocks(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from request.get("tools", [])
    yield from request.get("system", [])
    for message in request.get("messages", []):
        if isinstance(message["content"], list):
            yield from message["content"]


def _with_block_content(message: dict[str, Any], index: int) -> dict[str, Any]:
    """A deep copy of the message whose content is always a list of blocks."""
    copied = copy.deepcopy(message)
    if isinstance(copied["content"], str):
        if not copied["content"]:
            raise ValueError(f"messages[{index}] has empty content")
        copied["content"] = [{"type": "text", "text": copied["content"]}]
    return copied


def _mark_last_markable_block(message: dict[str, Any], index: int, marker: dict[str, str]) -> None:
    for block in reversed(message["content"]):
        if block.get("type") in _UNMARKABLE_BLOCK_TYPES:
            continue
        if block.get("type") == "text" and not block.get("text"):
            continue
        block["cache_control"] = dict(marker)
        return
    raise ValueError(f"messages[{index}] has no block that can carry a cache marker")


# SDK response -> plain types (pure) ---------------------------------------------------------


def to_llm_response(message: anthropic.types.Message) -> LLMResponse:
    """Convert an SDK Message into plain types."""
    # to_dict() dumps a block the way the SDK sends it back (only the fields the API set), so a
    # replayed assistant turn is exactly what the model produced, thinking signatures included.
    content = [block.to_dict(mode="json") for block in message.content]
    text = "\n".join(b.text for b in message.content if b.type == "text" and b.text)
    tool_calls = [
        ToolCall(id=b.id, name=b.name, input=dict(b.input))
        for b in message.content
        if b.type == "tool_use"
    ]
    usage = Usage(
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_creation_input_tokens=message.usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=message.usage.cache_read_input_tokens or 0,
    )
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=message.stop_reason or "",
        usage=usage,
        content=content,
        raw=message.to_dict(mode="json"),
    )


# Pacing ---------------------------------------------------------------------------------------


class Pacer:
    """Spaces request starts at least `interval_s` seconds apart; 0 means no spacing.

    `clock` and `sleep` exist so tests can run it on a fake clock.
    """

    def __init__(
        self,
        interval_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_s < 0:
            raise ValueError(f"interval_s must be >= 0, got {interval_s}")
        self.interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait(self) -> None:
        """Return when the next request may start."""
        if self.interval_s == 0:
            return
        async with self._lock:
            delay = self._next_start - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_start = self._clock() + self.interval_s


# The live client ----------------------------------------------------------------------------


class AnthropicClient:
    """The live client. It refuses to exist unless both live switches are on and a key is set."""

    def __init__(
        self,
        run: RunConfig,
        meter: CostMeter,
        *,
        settings: Settings | None = None,
        sdk: Any = None,
        retry_wait: RetryWait | None = None,
    ) -> None:
        # `sdk` and `retry_wait` exist so tests can drive this class offline: a fake SDK object,
        # and no sleeping between retries.
        settings = settings if settings is not None else Settings()
        if run.dry_run:
            raise LiveCallRefused(f"run {run.run_name!r} is dry_run=true; use ScriptedClient")
        if not settings.SWARM_LIVE:
            raise LiveCallRefused("SWARM_LIVE is off; refusing to make live API calls")
        api_key = settings.anthropic_api_key()
        if api_key is None:
            raise LiveCallRefused("ANTHROPIC_API_KEY is not set (put it in .env)")
        if run.provider != "anthropic":
            raise ValueError(f"AnthropicClient cannot serve provider {run.provider!r}")
        _check_meter(run, meter)
        self.run = run
        self.meter = meter
        self._retry_wait = retry_wait if retry_wait is not None else _backoff_or_retry_after
        self._in_flight = asyncio.Semaphore(run.concurrency)
        self._pacer = Pacer(PACING_S[run.provider])
        # max_retries=0: tenacity (below) is the only retry layer, so every retry is logged.
        self._sdk = (
            sdk
            if sdk is not None
            else anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=REQUEST_TIMEOUT_S)
        )

    async def complete(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        tool_choice: dict[str, Any] | None,
        max_tokens: int,
        tags: Tags,
        cache_until: int | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        tags = _checked_tags(tags)
        self.meter.ensure_within_budget()
        request = build_request_kwargs(
            model=self.run.model,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            cache_ttl=self.run.cache_ttl,
            cache_until=cache_until,
            output_format=output_format,
        )
        async with self._in_flight:
            message = await self._create_with_retries(request, tags)
        if message.model != self.run.model.name:
            log.warning(
                "asked for %s but the response says %s [%s]",
                self.run.model.name,
                message.model,
                _tag_text(tags),
            )
        response = to_llm_response(message)
        await self.meter.record(self.run.model.name, response.usage, **tags)
        return response

    async def _create_with_retries(self, request: dict[str, Any], tags: Tags) -> Any:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE_ERRORS),
            wait=self._retry_wait,
            stop=stop_after_attempt(MAX_ATTEMPTS),
            before_sleep=_retry_logger(tags),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                await self._pacer.wait()
                message = await self._sdk.messages.create(**request)
        return message

    @property
    def sdk(self) -> Any:
        """The SDK client (the optional judge batch backend submits through it)."""
        return self._sdk

    async def aclose(self) -> None:
        await self._sdk.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


_exponential_backoff = wait_exponential_jitter(initial=1, max=MAX_WAIT_S)


def _backoff_or_retry_after(state: RetryCallState) -> float:
    """Jittered exponential backoff (1 s doubling to 60 s), or the server's `retry-after`
    header when that asks for longer (also capped at 60 s)."""
    delay = _exponential_backoff(state)
    error = state.outcome.exception() if state.outcome else None
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is not None:
        retry_after = _seconds(headers.get("retry-after"))
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_WAIT_S))
    return delay


def _seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:  # an HTTP date: ignore it and keep the backoff
        return None


def _retry_logger(tags: Tags) -> Callable[[RetryCallState], None]:
    def before_sleep(state: RetryCallState) -> None:
        error = state.outcome.exception() if state.outcome else None
        status = getattr(error, "status_code", None)
        reason = f"{type(error).__name__} {status}" if status else type(error).__name__
        sleep = state.next_action.sleep if state.next_action else 0.0
        log.warning(
            "API attempt %d/%d failed (%s); retrying in %.1fs [%s]",
            state.attempt_number,
            MAX_ATTEMPTS,
            reason,
            sleep,
            _tag_text(tags),
        )

    return before_sleep


def _tag_text(tags: Tags) -> str:
    return f"phase={tags['phase']} agent={tags['agent']} episode={tags['episode']}"


def _checked_tags(tags: Tags) -> Tags:
    missing = sorted({"phase", "agent", "episode"} - set(tags))
    if missing:
        raise ValueError(f"tags is missing {missing}")
    return Tags(phase=str(tags["phase"]), agent=str(tags["agent"]), episode=int(tags["episode"]))


def _check_meter(run: RunConfig, meter: CostMeter) -> None:
    if (meter.provider, meter.cache_ttl) != (run.provider, run.cache_ttl):
        raise ValueError(
            f"meter ({meter.provider}, ttl {meter.cache_ttl}) does not match "
            f"run ({run.provider}, ttl {run.cache_ttl})"
        )


# The dry-run client ---------------------------------------------------------------------------

Script = Callable[[list[dict[str, Any]], list[ToolSpec] | None], LLMResponse]


@dataclass(frozen=True)
class RecordedCall:
    """One request a ScriptedClient received: exactly what AnthropicClient would have sent."""

    request: dict[str, Any]
    tags: Tags


class ScriptedClient:
    """The dry-run model. A script decides each reply; everything else (the request builder,
    the budget check, the meter) is the same code the live client runs. Scripted usage is zero
    unless a script says otherwise, but every call still goes through the meter."""

    def __init__(self, script: Script, *, run: RunConfig, meter: CostMeter) -> None:
        _check_meter(run, meter)
        self.script = script
        self.run = run
        self.meter = meter
        self.calls: list[RecordedCall] = []

    async def complete(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        tool_choice: dict[str, Any] | None,
        max_tokens: int,
        tags: Tags,
        cache_until: int | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        tags = _checked_tags(tags)
        self.meter.ensure_within_budget()
        request = build_request_kwargs(
            model=self.run.model,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            cache_ttl=self.run.cache_ttl,
            cache_until=cache_until,
            output_format=output_format,
        )
        self.calls.append(RecordedCall(request=request, tags=tags))
        response = self.script(copy.deepcopy(messages), tools)
        await self.meter.record(self.run.model.name, response.usage, **tags)
        return response


_scripted_ids = itertools.count(1)


def text_response(
    text: str, *, stop_reason: str = "end_turn", usage: Usage | None = None
) -> LLMResponse:
    """A scripted reply with plain text (zero usage unless given)."""
    content = [{"type": "text", "text": text}] if text else []
    return LLMResponse(
        text=text,
        stop_reason=stop_reason,
        usage=usage or Usage(),
        content=content,
        raw={"scripted": True},
    )


def tool_uses_response(
    calls: list[tuple[str, dict[str, Any]]], *, text: str = "", usage: Usage | None = None
) -> LLMResponse:
    """A scripted reply that calls one or more tools, in order (zero usage unless given)."""
    tool_calls = [
        ToolCall(id=f"toolu_scripted_{next(_scripted_ids):04d}", name=name, input=dict(args))
        for name, args in calls
    ]
    content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    content.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": dict(call.input)}
        for call in tool_calls
    )
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason="tool_use",
        usage=usage or Usage(),
        content=content,
        raw={"scripted": True},
    )


def tool_use_response(
    name: str, tool_input: dict[str, Any], *, text: str = "", usage: Usage | None = None
) -> LLMResponse:
    """A scripted reply that calls one tool."""
    return tool_uses_response([(name, tool_input)], text=text, usage=usage)


def scripted_sequence(*responses: LLMResponse) -> Script:
    """A script that returns these responses in order, and fails loudly if asked for more."""
    pending = list(responses)

    def script(messages: list[dict[str, Any]], tools: list[ToolSpec] | None) -> LLMResponse:
        if not pending:
            raise RuntimeError("scripted_sequence ran out of responses")
        return pending.pop(0)

    return script
