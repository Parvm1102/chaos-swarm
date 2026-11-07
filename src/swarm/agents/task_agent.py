"""The task phase on its own: one agent, one task, no peer.

run_task_phase gives the agent the task tools, the task-phase system prompt and the request (the
first user message, verbatim), and runs the tool loop until the agent writes a response without
a tool call or has used its 10 turns. Then it grades what the agent saved. It never re-prompts:
an agent that stops without saving simply has no answer (answer_saved=False, correct=False), and
the stop cause says how the phase ended.

Thinking is whatever the client sends for the model (THINKING_OFF: Sonnet 5.5 `between_tools`,
Haiku 5.5 `disabled`); the result records it. Step 3 reuses the same pieces inside an episode.
"""

import copy
import time
from dataclasses import dataclass
from typing import Any

from swarm.config import ModelConfig
from swarm.env.database import (
    TASK_TOOLS,
    TASK_TOOLS_VERSION,
    EnvEvent,
    LogEntry,
    TaskEnv,
    TaskOutcome,
)
from swarm.llm.client import THINKING_OFF, LLMClient, Tags
from swarm.llm.loop import StopCause, ToolCallRecord, run_tool_loop
from swarm.llm.pricing import cost_usd
from swarm.llm.types import Usage, text_block, user_message
from swarm.prompts import load_prompt

TASK_PROMPT = "task_phase_system"
MAX_TURNS = 10
# Output cap per model turn. At 1,024 a long query plus notes could hit the cap, and a
# max_tokens stop ends the phase; only tokens actually generated are billed.
TASK_MAX_TOKENS = 4_096
TOOL_CHOICE = {"type": "auto"}  # Sonnet 5.5 rejects a forced tool_choice; the prompt asks instead


@dataclass(frozen=True)
class TaskPhaseResult:
    messages: list[dict[str, Any]]  # the whole phase, kept for step 3's episode array
    tool_calls: list[ToolCallRecord]
    stop_cause: StopCause
    turns: int
    usage: Usage  # summed over the phase's model calls
    cost_usd: float
    wall_s: float
    outcome: TaskOutcome
    log: list[LogEntry]
    events: list[EnvEvent]
    prompt_version: str
    tools_version: str
    model: str
    thinking: dict[str, str] | None


async def run_task_phase(
    client: LLMClient, env: TaskEnv, nl_request: str, model_cfg: ModelConfig, tags: Tags
) -> TaskPhaseResult:
    """Run one solo task phase and grade it."""
    run = client.run
    if model_cfg.name != run.model.name:
        raise ValueError(f"model_cfg is {model_cfg.name} but the client calls {run.model.name}")
    prompt = load_prompt(TASK_PROMPT)
    started = time.perf_counter()
    result = await run_tool_loop(
        client,
        system=[text_block(prompt.text)],
        messages=[user_message(nl_request)],
        tools=TASK_TOOLS,
        handlers=env.handlers(),
        max_turns=MAX_TURNS,
        max_tokens=model_cfg.max_tokens,
        tags=tags,
        tool_choice=TOOL_CHOICE,
    )
    wall_s = time.perf_counter() - started
    usages = [response.usage for response in result.responses]
    return TaskPhaseResult(
        messages=result.messages,
        tool_calls=result.tool_calls,
        stop_cause=result.stop_cause,
        turns=result.turns,
        usage=Usage(
            input_tokens=sum(u.input_tokens for u in usages),
            output_tokens=sum(u.output_tokens for u in usages),
            cache_creation_input_tokens=sum(u.cache_creation_input_tokens for u in usages),
            cache_read_input_tokens=sum(u.cache_read_input_tokens for u in usages),
        ),
        # Priced per call, not on the sum: a rate card can depend on one call's prompt size.
        cost_usd=sum(
            cost_usd(run.model.name, u, provider=run.provider, cache_ttl=run.cache_ttl)
            for u in usages
        ),
        wall_s=wall_s,
        outcome=env.outcome(),
        log=copy.deepcopy(env.log),
        events=list(env.events),
        prompt_version=prompt.version,
        tools_version=TASK_TOOLS_VERSION,
        model=run.model.name,
        thinking=THINKING_OFF.get(run.model.name),
    )
