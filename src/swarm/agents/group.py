"""Experiment 4: one group of agents playing its episodes in rotating pairs (the scheduler).

round_robin(6, 10) is the fixed pairing table: agent 0 stays, agents 1-5 rotate (the circle
method), so each agent meets every other agent once per cycle of 5 episodes. Episodes 6-10
repeat episodes 1-5 with the roles swapped (whoever was A, speaking first, is now B).

run_group runs a group's episodes in order. In each episode the three pairs run at the same
time; each pair runs the standard episode (swarm.agents.episode, group mode: "your peer", the
group intro and, in the board arms, post_to_board and the snapshot). What a pair-episode needs
from the past depends only on EARLIER episodes:
- each agent's memory: memory_text_group over the pair-episodes it took part in, each with the
  role it had there;
- the board snapshot: the last 12 posts from earlier episodes (swarm.env.board).
So the pairs of one episode are independent, and resume is exact: records live in
<out_dir>/episodes/eEE_pP.json (P = pair index 0-2), transcripts in <out_dir>/transcripts/, and a
rerun skips every pair-episode whose record exists, rebuilding the inputs of the missing ones
from the stored records. A record is written only when its pair-episode has finished.

Planted agents (the group indices in `planted`) are harness-scripted (scripted_group_proposer);
every other agent is a real agent. Each record carries a `group` block: group id, episode, pair
index, the group index of each role, which roles are planted, and the board posts shown.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from swarm.agents.episode import EpisodeAgent, EpisodeSpec, run_episode
from swarm.agents.memory import (
    GROUP_MEMORY_PROMPTS,
    MEMORY_MESSAGE_MAX,
    load_memory_prompts,
    memory_text_group,
)
from swarm.calibrate import first_error
from swarm.config import RunConfig
from swarm.env.board import SNAPSHOT_PROMPTS, board_shown, board_snapshot
from swarm.episodes import GROUP_PROPOSER_PROMPTS
from swarm.llm.client import LLMClient
from swarm.prompts import load_prompt

EPISODES_DIR = "episodes"
TRANSCRIPTS_DIR = "transcripts"
GROUP_FILE = "group.json"
ROLES = ("A", "B")
PLANTED_POSITIONS = {0: (), 1: (0,), 2: (0, 3)}


def round_robin(group_size: int = 6, episodes: int = 10) -> list[list[tuple[int, int]]]:
    """pairings[e - 1]: the (A, B) group indices of each pair in episode e (module docstring)."""
    if group_size % 2:
        raise ValueError("a round robin needs an even group size")
    others = list(range(1, group_size))
    rounds = []
    for r in range(group_size - 1):
        a = others[r:] + others[:r]
        pairs = [(0, a[0])] + [(a[i], a[-i]) for i in range(1, group_size // 2)]
        rounds.append([(min(p), max(p)) for p in pairs])
    table = []
    for e in range(episodes):
        cycle, index = divmod(e, group_size - 1)
        table.append([(i, j) if cycle % 2 == 0 else (j, i) for i, j in rounds[index]])
    return table


# client_for(agent, group index, planted) -> the client that plays that agent in this episode
GroupClientFor = Callable[[EpisodeAgent, int, bool], LLMClient]


@dataclass(frozen=True)
class GroupSpec:
    run: RunConfig
    task_dir: Path
    group_id: int
    tasks: list[list[str]]  # tasks[i][e - 1]: agent i's task in episode e
    memory: bool
    out_dir: Path
    planted: tuple[int, ...] = ()
    board: bool = False
    rules: str = "explicit"
    message_limit: int = 200
    task_prompt: str = "task_phase_system"
    query_cap: int | None = None

    @property
    def group_size(self) -> int:
        return len(self.tasks)

    @property
    def episodes(self) -> int:
        return len(self.tasks[0])


def load_group_records(out_dir: Path) -> list[dict[str, Any]]:
    """Every pair-episode record of a group, by episode and pair index."""
    records = [
        orjson.loads(path.read_bytes()) for path in (out_dir / EPISODES_DIR).glob("e*_p*.json")
    ]
    return sorted(records, key=lambda r: (r["episode"], r["group"]["pair_index"]))


def entries_for(member: int, records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(role, record) of every pair-episode the agent took part in, in episode order."""
    return [
        (role, record)
        for record in records
        for role in ROLES
        if record["group"]["agents"][role] == member
    ]


async def run_group(spec: GroupSpec, client_for: GroupClientFor) -> dict[str, Any]:
    """Run (or finish) one group and return its group.json content."""
    memory_prompts = load_memory_prompts() | {n: load_prompt(n) for n in GROUP_MEMORY_PROMPTS}
    snapshot_prompts = {name: load_prompt(name) for name in SNAPSHOT_PROMPTS}
    extra_versions = {
        p.name: p.version for p in (*memory_prompts.values(), *snapshot_prompts.values())
    } | {name: load_prompt(name).version for name in GROUP_PROPOSER_PROMPTS}
    pairings = round_robin(spec.group_size, spec.episodes)
    records = load_group_records(spec.out_dir)

    for episode in range(1, spec.episodes + 1):
        done = {r["group"]["pair_index"] for r in records if r["episode"] == episode}
        todo = [p for p in range(len(pairings[episode - 1])) if p not in done]
        if not todo:
            continue
        earlier = [r for r in records if r["episode"] < episode]
        shown = board_shown(earlier, episode) if spec.board else []
        board_text = board_snapshot(shown, snapshot_prompts) if spec.board else None

        async def one(pair_index: int, episode: int = episode, earlier=earlier, shown=shown,
                      board_text=board_text) -> dict[str, Any]:  # fmt: skip
            a, b = pairings[episode - 1][pair_index]
            members = {"A": a, "B": b}
            planted = {role: member in spec.planted for role, member in members.items()}
            result = await run_episode(
                EpisodeSpec(
                    run=spec.run,
                    task_dir=spec.task_dir,
                    sequence_id=spec.group_id,
                    episode=episode,
                    task_ids={role: spec.tasks[m][episode - 1] for role, m in members.items()},
                    memory={
                        role: memory_text_group(
                            entries_for(member, earlier),
                            enabled=spec.memory,
                            prompts=memory_prompts,
                        )
                        for role, member in members.items()
                    },
                    rules=spec.rules,
                    scripted={role: "proposer" for role in ROLES if planted[role]} or None,
                    message_limit=spec.message_limit,
                    task_prompt=spec.task_prompt,
                    query_cap=spec.query_cap,
                    group_size=spec.group_size,
                    board=spec.board,
                    board_text=board_text,
                ),
                lambda agent: client_for(agent, members[agent.name], planted[agent.name]),
            )
            record = result.record()
            record["group"] = {
                "group_id": spec.group_id,
                "episode": episode,
                "pair_index": pair_index,
                "agents": members,
                "planted": planted,
                "board_shown": shown,
            }
            record["memory_enabled"] = spec.memory
            record["memory_message_max"] = MEMORY_MESSAGE_MAX
            record["prompt_versions"].update(extra_versions)
            name = f"e{episode:02d}_p{pair_index}"
            _write(spec.out_dir / EPISODES_DIR / f"{name}.json", record)
            for role in ROLES:
                _write(
                    spec.out_dir / TRANSCRIPTS_DIR / f"{name}_{role}.json", result.transcript(role)
                )
            return record

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(p)) for p in todo]
        except BaseExceptionGroup as failed:
            raise first_error(failed) from None
        records.extend(task.result() for task in tasks)
        records.sort(key=lambda r: (r["episode"], r["group"]["pair_index"]))

    content = {
        "group_id": spec.group_id,
        "group_size": spec.group_size,
        "episodes": spec.episodes,
        "planted": list(spec.planted),
        "board": spec.board,
        "memory_enabled": spec.memory,
        "pairings": [[list(p) for p in episode] for episode in pairings],
        "tasks": spec.tasks,
        "pair_episodes": len(records),
    }
    _write(spec.out_dir / GROUP_FILE, content)
    return content


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2) + b"\n")
