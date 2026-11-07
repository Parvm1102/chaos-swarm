"""One episode: two agents, their tasks, the channel, verdicts, feedback and notes.

run_episode(spec, client_for) runs, in order:
1. Open. Each agent gets a fresh message array. Its system prompt is episode_intro +
   task_phase_system (verbatim) + episode_rules, and one tool list (EPISODE_TOOLS) serves the
   whole episode, so the cached prefix never changes. The first user message holds its notes
   from earlier episodes (step 4 renders them; "none yet" here), the episode number and its own
   request.
2. Task phase: as in step 2 (up to 10 calls, tool_choice auto), A and B at the same time.
3. Communication: the opening (with the peer's request, not its answer), then ROUNDS rounds,
   A first in every round. A turn is up to CHANNEL_TURN_CALLS model calls; it ends when a
   message is delivered (send_message returns STOP) or the agent replies without a tool call.
4. Verdict, A and B at the same time and privately: submit_verdict. If none arrives, one
   re-prompt; if still none, the reply to the re-prompt is parsed for exactly one of ACCEPT
   and REJECT; otherwise the verdict is missing (scored as REJECT, and excluded from pathway
   classification later). Never retried further.
5. Scoring and feedback (swarm.agents.verdict), then a reflection: one call with tool_choice
   "none" (the tools stay listed, for the cache) asking for notes for the agent's future self.

The phase gate: every handler checks the agent's current phase against PHASE_TOOLS. A tool
called outside its phase gets an error result and a recorded phase violation; task tools called
out of phase never reach the raw log. get_work_record works in the task, communication and verdict
phases and is never logged. Within an episode each agent's history only grows: every request
extends the previous one, and assistant turns (thinking blocks included) are replayed as is.
"""

import asyncio
import copy
import hashlib
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from swarm.agents.task_agent import MAX_TURNS
from swarm.agents.verdict import (
    SUBMIT_VERDICT_TOOL,
    VERDICT,
    VERDICTS,
    VerdictRecord,
    feedback_text,
    parse_verdict_text,
    shared_reward,
    verdict_correct,
)
from swarm.config import RunConfig
from swarm.env.channel import (
    MESSAGE_LIMIT,
    ROUNDS,
    SEND,
    SEND_MESSAGE_TOOL,
    SPEAKING_ORDER,
    Channel,
    Delivery,
)
from swarm.env.database import GET_RECORD, INSPECT, QUERY, SAVE, TASK_TOOLS, TaskEnv, TaskOutcome
from swarm.llm.client import LLMClient, Tags, thinking_provenance
from swarm.llm.loop import Handler, LoopResult, Stop, ToolError, run_tool_loop
from swarm.llm.meter import TOKEN_FIELDS
from swarm.llm.pricing import cost_usd
from swarm.llm.types import text_block, user_message
from swarm.prompts import Prompt, load_prompt, render

CHANNEL_TURN_CALLS = 3  # model calls per channel turn (room to retry a refused message)
VERDICT_CALLS = 2  # model calls per verdict turn; the re-prompt gets as many again
AUTO = {"type": "auto"}
NO_TOOLS = {"type": "none"}

EPISODE_TOOLS = [*TASK_TOOLS, SEND_MESSAGE_TOOL, SUBMIT_VERDICT_TOOL]
EPISODE_TOOLS_VERSION = hashlib.sha256(
    orjson.dumps([tool.to_anthropic() for tool in EPISODE_TOOLS], option=orjson.OPT_SORT_KEYS)
).hexdigest()
PHASE_TOOLS: dict[str, frozenset[str]] = {
    "task": frozenset({INSPECT, QUERY, SAVE, GET_RECORD}),
    "communication": frozenset({SEND, GET_RECORD}),
    "verdict": frozenset({VERDICT, GET_RECORD}),
    "reflection": frozenset(),
}
PROMPT_NAMES = (
    "episode_intro",
    "task_phase_system",
    "episode_rules",
    "episode_open",
    "memory_none",
    "communication_open",
    "communication_round",
    "channel_message",
    "channel_silence",
    "verdict_open",
    "verdict_reprompt",
    "feedback",
    "reflection",
)


@dataclass(frozen=True)
class EpisodeSpec:
    run: RunConfig
    task_dir: Path
    sequence_id: int
    episode: int  # 1-based
    task_ids: dict[str, str]  # agent -> task id
    memory: dict[str, str] | None = None  # agent -> rendered notes; None: no earlier episodes


@dataclass(frozen=True)
class PhaseCall:
    phase: str
    result: LoopResult


class EpisodeAgent:
    """One agent's state within an episode: its env, history, phase and what it produced."""

    def __init__(
        self,
        name: str,
        env: TaskEnv,
        spec: EpisodeSpec,
        prompts: dict[str, Prompt],
        channel: Channel,
    ) -> None:
        self.name = name
        self.peer = channel.peer(name)
        self.env = env
        self.spec = spec
        self.prompts = prompts
        self.channel = channel
        self.client: LLMClient | None = None
        self.phase = "task"
        self.system = [
            text_block(render(prompts["episode_intro"], agent=name, peer=self.peer)),
            text_block(prompts["task_phase_system"].text),  # verbatim, as calibrated
            text_block(
                render(prompts["episode_rules"], peer=self.peer, rounds=ROUNDS, limit=MESSAGE_LIMIT)
            ),
        ]
        self.messages: list[dict[str, Any]] = []
        self.calls: list[PhaseCall] = []
        self.violations: list[dict[str, Any]] = []
        self.verdict: VerdictRecord | None = None
        self.reflection = ""
        self._verdict_source = "tool"
        task_handlers = env.handlers()
        own: dict[str, Handler] = {SEND: self._send, VERDICT: self._submit_verdict}
        self.handlers = {
            tool.name: self._gated(tool.name, task_handlers.get(tool.name) or own[tool.name])
            for tool in EPISODE_TOOLS
        }

    # Tools -----------------------------------------------------------------------------------

    def _gated(self, name: str, inner: Handler) -> Handler:
        def handler(arguments: dict[str, Any]) -> Any:
            if name not in PHASE_TOOLS[self.phase]:
                self.violations.append(
                    {"phase": self.phase, "tool": name, "arguments": copy.deepcopy(arguments)}
                )
                raise ToolError(f"error: {name} is not available in the {self.phase} phase")
            return inner(arguments)

        return handler

    def _send(self, arguments: dict[str, Any]) -> Stop:
        text = arguments.get("text")
        if not isinstance(text, str):
            raise ToolError("error: send_message needs one argument, text: a string")
        return Stop(self.channel.send(self.name, text))  # a delivered message ends the turn

    def _submit_verdict(self, arguments: dict[str, Any]) -> Stop:
        verdict, justification = arguments.get("verdict"), arguments.get("justification")
        if verdict not in VERDICTS or not isinstance(justification, str):
            raise ToolError(
                "error: submit_verdict needs verdict (ACCEPT or REJECT) and justification"
            )
        self.verdict = VerdictRecord(
            verdict=verdict, justification=justification, source=self._verdict_source
        )
        return Stop("verdict recorded")

    # Turns -----------------------------------------------------------------------------------

    async def turn(
        self, text: str, *, phase: str, max_turns: int, tool_choice: dict[str, str] = AUTO
    ) -> LoopResult:
        """Append one user message and let the agent act on it (up to max_turns calls)."""
        if self.client is None:
            raise RuntimeError(f"agent {self.name} has no client")
        self.phase = phase
        result = await run_tool_loop(
            self.client,
            system=self.system,
            messages=[*self.messages, user_message(text)],
            tools=EPISODE_TOOLS,
            handlers=self.handlers,
            max_turns=max_turns,
            max_tokens=self.spec.run.model.max_tokens,
            tags=Tags(phase=phase, agent=self.name, episode=self.spec.episode),
            tool_choice=tool_choice,
        )
        self.messages = result.messages
        self.calls.append(PhaseCall(phase, result))
        return result

    async def run_verdict(self, opening: str) -> None:
        self._verdict_source = "tool"
        result = await self.turn(opening, phase="verdict", max_turns=VERDICT_CALLS)
        if self.verdict is None:
            self._verdict_source = "reprompt_tool"
            reprompt = render(self.prompts["verdict_reprompt"], peer=self.peer)
            result = await self.turn(reprompt, phase="verdict", max_turns=VERDICT_CALLS)
        if self.verdict is None:
            parsed = parse_verdict_text(result.final_text)
            if parsed is not None:
                self.verdict = VerdictRecord(
                    verdict=parsed[0], justification=parsed[1], source="text"
                )
            else:
                self.verdict = VerdictRecord(
                    verdict=None, justification=result.final_text.strip(), source="missing"
                )

    async def run_reflection(self, text: str) -> None:
        result = await self.turn(text, phase="reflection", max_turns=1, tool_choice=NO_TOOLS)
        self.reflection = result.final_text.strip()
        self.phase = "done"

    # Accounting ------------------------------------------------------------------------------

    def task_call(self) -> PhaseCall:
        return next(call for call in self.calls if call.phase == "task")

    def refusals(self) -> list[dict[str, Any]]:
        """Every response the API stopped as a refusal, with its category (recorded, never
        retried: a refusal ends that turn, like any other ending)."""
        found = []
        for call in self.calls:
            for number, response in enumerate(call.result.responses, start=1):
                if response.stop_reason == "refusal":
                    details = response.raw.get("stop_details") or {}
                    found.append(
                        {
                            "phase": call.phase,
                            "call": number,
                            "category": details.get("category"),
                            "explanation": details.get("explanation"),
                            "text": response.text,
                            "tool_calls": [
                                {"name": c.name, "input": c.input} for c in response.tool_calls
                            ],
                        }
                    )
        return found

    def refusal_counts(self) -> dict[str, dict[str, int]]:
        """{phase: {category: count}}: kept in every record for the report's discussion."""
        counts: dict[str, dict[str, int]] = {}
        for refusal in self.refusals():
            by_category = counts.setdefault(refusal["phase"], {})
            category = refusal["category"] or "unknown"
            by_category[category] = by_category.get(category, 0) + 1
        return counts

    def reflection_complete(self) -> bool:
        """The note was written: the reflection call ended normally with some text."""
        call = next((c for c in self.calls if c.phase == "reflection"), None)
        return call is not None and call.result.stop_cause == "end_turn" and bool(self.reflection)

    def usage_by_phase(self) -> dict[str, dict[str, float]]:
        run = self.spec.run
        phases: dict[str, dict[str, float]] = {}
        for call in self.calls:
            bucket = phases.setdefault(
                call.phase, {"calls": 0, "usd": 0.0, **dict.fromkeys(TOKEN_FIELDS, 0)}
            )
            for response in call.result.responses:
                bucket["calls"] += 1
                bucket["usd"] += cost_usd(
                    run.model.name, response.usage, provider=run.provider, cache_ttl=run.cache_ttl
                )
                for field in TOKEN_FIELDS:
                    bucket[field] += getattr(response.usage, field)
        return phases


@dataclass
class EpisodeResult:
    spec: EpisodeSpec
    agents: dict[str, EpisodeAgent]
    channel: Channel
    outcomes: dict[str, TaskOutcome]
    verdicts: dict[str, VerdictRecord]
    correct: dict[str, bool]
    reward: int
    feedback: dict[str, str]
    prompt_versions: dict[str, str]
    raw_logs: dict[str, list[dict[str, Any]]]
    env_events: dict[str, list[dict[str, Any]]]
    wall_s: float

    @property
    def mutual_accept(self) -> bool:
        return all(self.verdicts[name].scored == "ACCEPT" for name in SPEAKING_ORDER)

    def record(self) -> dict[str, Any]:
        """The episode record (runs/<run>/episodes/s<seq>_e<ep>.json)."""
        run = self.spec.run
        agents = {}
        for name, agent in self.agents.items():
            peer = agent.peer
            task = agent.task_call().result
            usage = agent.usage_by_phase()
            agents[name] = {
                "model": agent.client.run.model.name if agent.client else None,
                "thinking": thinking_provenance(run.model),
                "task_id": agent.env.spec.task_id,
                "template": agent.env.spec.template,
                "n_answer": agent.env.spec.n_answer,
                "task": {
                    **self.outcomes[name].model_dump(),
                    "stop_cause": task.stop_cause,
                    "turns": task.turns,
                },
                "messages_sent": [
                    {"round": d.round, "text": d.text, "chars": d.chars}
                    for d in self.channel.sent_by(name)
                ],
                "silent_rounds": self.channel.silent_rounds(name),
                "refused_messages": [
                    {"round": r.round, "chars": r.chars, "reason": r.reason}
                    for r in self.channel.refusals
                    if r.sender == name
                ],
                # The proof, per episode, that the peer's complete log never came through.
                "delivered_chars": self.channel.delivered_chars(name),
                "peer_log_chars": self.outcomes[peer].log_chars,
                "verdict": {
                    **self.verdicts[name].model_dump(),
                    "scored": self.verdicts[name].scored,
                    "missing": self.verdicts[name].missing,
                },
                "verdict_correct": self.correct[name],
                "feedback": self.feedback[name],
                "reflection": agent.reflection,
                "phase_violations": agent.violations,
                "refusals": agent.refusals(),
                "refusal_counts": agent.refusal_counts(),
                "reflection_complete": agent.reflection_complete(),
                "usage_by_phase": usage,
                "cost_usd": sum(bucket["usd"] for bucket in usage.values()),
            }
        return {
            "run_name": run.run_name,
            "provider": run.provider,
            "cache_ttl": run.cache_ttl,
            "dry_run": run.dry_run,
            "seed": run.seed,
            "task_dir": str(self.spec.task_dir),
            "sequence_id": self.spec.sequence_id,
            "episode": self.spec.episode,
            "rounds": ROUNDS,
            "message_limit": MESSAGE_LIMIT,
            "speaking_order": list(SPEAKING_ORDER),
            # Everything that passed through the channel, in order (text None: a silence notice).
            # Step 4 rebuilds each agent's memory of the channel from this list.
            "channel": [
                {"round": d.round, "sender": d.sender, "text": d.text}
                for d in self.channel.deliveries
            ],
            "prompt_versions": self.prompt_versions,
            "tools_version": EPISODE_TOOLS_VERSION,
            "agents": agents,
            "mutual_accept": self.mutual_accept,
            "refusals_total": sum(len(agent["refusals"]) for agent in agents.values()),
            "reflections_complete": sum(agent["reflection_complete"] for agent in agents.values()),
            "reward": self.reward,
            "cost_usd": sum(agent["cost_usd"] for agent in agents.values()),
            "wall_s": self.wall_s,
        }

    def transcript(self, name: str) -> dict[str, Any]:
        """Everything one agent saw and did (runs/<run>/transcripts/s<seq>_e<ep>_<agent>.json)."""
        agent = self.agents[name]
        return {
            "agent": name,
            "sequence_id": self.spec.sequence_id,
            "episode": self.spec.episode,
            "system": [block["text"] for block in agent.system],
            "tools": [tool.name for tool in EPISODE_TOOLS],
            "messages": agent.messages,
            "tool_calls": [
                {
                    "phase": call.phase,
                    "turn": record.turn,
                    "name": record.call.name,
                    "input": record.call.input,
                    "result": record.result,
                    "is_error": record.is_error,
                    "executed": record.executed,
                }
                for call in agent.calls
                for record in call.result.tool_calls
            ],
            "raw_log": self.raw_logs[name],
            "env_events": self.env_events[name],
        }


async def run_episode(
    spec: EpisodeSpec, client_for: Callable[[EpisodeAgent], LLMClient]
) -> EpisodeResult:
    """Run one episode for the pair A, B and return its result."""
    if set(spec.task_ids) != set(SPEAKING_ORDER):
        raise ValueError(f"task_ids must name agents {SPEAKING_ORDER}, got {sorted(spec.task_ids)}")
    prompts = {name: load_prompt(name) for name in PROMPT_NAMES}
    channel = Channel()
    started = time.perf_counter()
    with (
        TaskEnv.open(spec.task_dir, spec.task_ids["A"]) as env_a,
        TaskEnv.open(spec.task_dir, spec.task_ids["B"]) as env_b,
    ):
        agents = {
            "A": EpisodeAgent("A", env_a, spec, prompts, channel),
            "B": EpisodeAgent("B", env_b, spec, prompts, channel),
        }
        for agent in agents.values():
            agent.client = client_for(agent)
            if agent.client.run.model.name != spec.run.model.name:
                raise ValueError(f"agent {agent.name}'s client calls another model")

        # 1-2. Open, and the task phase.
        memory = spec.memory or {}
        await _together(
            agent.turn(
                render(
                    prompts["episode_open"],
                    episode=spec.episode,
                    memory=memory.get(agent.name) or prompts["memory_none"].text.strip(),
                    request=agent.env.spec.nl_request,
                ),
                phase="task",
                max_turns=MAX_TURNS,
            )
            for agent in agents.values()
        )

        # 3. Communication: A then B in every round.
        for round_number in range(1, ROUNDS + 1):
            for name in SPEAKING_ORDER:
                agent = agents[name]
                parts = []
                if round_number == 1:
                    parts.append(
                        render(
                            prompts["communication_open"],
                            peer=agent.peer,
                            rounds=ROUNDS,
                            limit=MESSAGE_LIMIT,
                            peer_request=agents[agent.peer].env.spec.nl_request,
                        )
                    )
                parts.extend(_delivery_text(prompts, d) for d in channel.take_inbox(name))
                parts.append(
                    render(
                        prompts["communication_round"],
                        round=round_number,
                        rounds=ROUNDS,
                        peer=agent.peer,
                        limit=MESSAGE_LIMIT,
                    )
                )
                channel.open_turn(name, round_number)
                await agent.turn(
                    "\n\n".join(parts), phase="communication", max_turns=CHANNEL_TURN_CALLS
                )
                channel.close_turn()

        # 4. Verdicts; whatever is still in an inbox (B's last message) arrives with the opening.
        openings = {
            name: "\n\n".join(
                [
                    *(_delivery_text(prompts, d) for d in channel.take_inbox(name)),
                    render(prompts["verdict_open"], peer=agents[name].peer),
                ]
            )
            for name in SPEAKING_ORDER
        }
        await _together(agents[name].run_verdict(openings[name]) for name in SPEAKING_ORDER)

        # 5. Scoring, feedback and reflections.
        outcomes = {name: agent.env.outcome() for name, agent in agents.items()}
        verdicts = {name: _require_verdict(agent) for name, agent in agents.items()}
        correct = {
            name: verdict_correct(verdicts[name].scored, outcomes[agents[name].peer].correct)
            for name in SPEAKING_ORDER
        }
        reward = shared_reward(*correct.values())
        feedback = {
            name: feedback_text(
                prompts["feedback"],
                episode=spec.episode,
                peer=agent.peer,
                own_verdict=verdicts[name],
                peer_verdict=verdicts[agent.peer],
                own_outcome=outcomes[name],
                peer_outcome=outcomes[agent.peer],
            )
            for name, agent in agents.items()
        }
        reflection_prompt = prompts["reflection"].text.strip()
        await _together(
            agents[name].run_reflection(f"{feedback[name]}\n\n{reflection_prompt}")
            for name in SPEAKING_ORDER
        )
        raw_logs = {name: copy.deepcopy(agent.env.log) for name, agent in agents.items()}
        env_events = {
            name: [event.model_dump() for event in agent.env.events]
            for name, agent in agents.items()
        }

    return EpisodeResult(
        spec=spec,
        agents=agents,
        channel=channel,
        outcomes=outcomes,
        verdicts=verdicts,
        correct=correct,
        reward=reward,
        feedback=feedback,
        prompt_versions={name: prompt.version for name, prompt in prompts.items()},
        raw_logs=raw_logs,
        env_events=env_events,
        wall_s=time.perf_counter() - started,
    )


def _delivery_text(prompts: dict[str, Prompt], delivery: Delivery) -> str:
    if delivery.text is None:
        return render(prompts["channel_silence"], round=delivery.round, sender=delivery.sender)
    return render(
        prompts["channel_message"],
        round=delivery.round,
        sender=delivery.sender,
        text=delivery.text,
    )


def _require_verdict(agent: EpisodeAgent) -> VerdictRecord:
    if agent.verdict is None:  # run_verdict always records one; this is a harness bug
        raise RuntimeError(f"agent {agent.name} has no verdict record")
    return agent.verdict


async def _together(coroutines: Iterable[Coroutine[Any, Any, None]]) -> None:
    """Run both agents' turns at once; the first failure cancels the other and is raised."""
    try:
        async with asyncio.TaskGroup() as group:
            for coroutine in coroutines:
                group.create_task(coroutine)
    except BaseExceptionGroup as failed:
        first = failed.exceptions[0]
        while isinstance(first, BaseExceptionGroup):
            first = first.exceptions[0]
        raise first from None
