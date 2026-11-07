"""One trajectory: a fixed pair playing its sequence of episodes, with memory between them.

run_trajectory runs the episodes in order. Before each one it renders each agent's memory from
the records of the earlier episodes (swarm.agents.memory), and after each one it writes the
episode record and both transcripts into the trajectory's folder:

    <out_dir>/episodes/e01.json ...        the episode records
    <out_dir>/transcripts/e01_A.json ...   what each agent saw and did
    <out_dir>/trajectory.json              the trajectory's metrics, once it is complete

Resuming is the default: episodes whose records already exist are not run again, and the next
episode's memory is rebuilt from those records (they hold everything memory needs). A record is
written only when its episode has finished, so an episode cut short is simply run again.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from swarm.agents.episode import EpisodeAgent, EpisodeSpec, run_episode
from swarm.agents.memory import MEMORY_MESSAGE_MAX, load_memory_prompts, memory_text
from swarm.analysis.metrics import trajectory_metrics
from swarm.config import RunConfig
from swarm.episodes import PROPOSER_PROMPTS
from swarm.llm.client import LLMClient
from swarm.prompts import load_prompt

EPISODES_DIR = "episodes"
TRANSCRIPTS_DIR = "transcripts"
TRAJECTORY_FILE = "trajectory.json"


@dataclass(frozen=True)
class TrajectorySpec:
    run: RunConfig
    task_dir: Path
    sequence_id: int
    tasks: list[dict[str, str]]  # episode i (1-based) is tasks[i - 1]: {"A": id, "B": id}
    memory: bool
    out_dir: Path
    rules: str = "explicit"
    scripted: dict[str, str] | None = None  # agent -> label of a harness-scripted agent
    message_limit: int = 200
    task_prompt: str = "task_phase_system"
    query_cap: int | None = None
    criterion: str = "tightened"  # the collusion criterion for trajectory.json


def load_records(out_dir: Path) -> list[dict[str, Any]]:
    """The episode records already written, in order (e01, e02, ... without gaps)."""
    records = []
    for episode in range(1, 1000):
        path = Path(out_dir) / EPISODES_DIR / f"e{episode:02d}.json"
        if not path.is_file():
            break
        records.append(orjson.loads(path.read_bytes()))
    return records


async def run_trajectory(
    spec: TrajectorySpec, client_for: Callable[[EpisodeAgent], LLMClient]
) -> dict[str, Any]:
    """Run (or finish) one trajectory and return its trajectory.json content."""
    prompts = load_memory_prompts()
    records = load_records(spec.out_dir)
    for episode in range(len(records) + 1, len(spec.tasks) + 1):
        memory = {
            agent: memory_text(agent, records, enabled=spec.memory, prompts=prompts)
            for agent in ("A", "B")
        }
        result = await run_episode(
            EpisodeSpec(
                run=spec.run,
                task_dir=spec.task_dir,
                sequence_id=spec.sequence_id,
                episode=episode,
                task_ids=spec.tasks[episode - 1],
                memory=memory,
                rules=spec.rules,
                scripted=spec.scripted,
                message_limit=spec.message_limit,
                task_prompt=spec.task_prompt,
                query_cap=spec.query_cap,
            ),
            client_for,
        )
        record = result.record()
        record["memory_enabled"] = spec.memory
        record["prompt_versions"].update({name: p.version for name, p in prompts.items()})
        record["memory_message_max"] = MEMORY_MESSAGE_MAX
        if spec.scripted:  # the scripted peer's messages are prompt text too (A reads them)
            record["prompt_versions"].update(
                {name: load_prompt(name).version for name in PROPOSER_PROMPTS}
            )
        _write(spec.out_dir / EPISODES_DIR / f"e{episode:02d}.json", record)
        for agent in ("A", "B"):
            _write(
                spec.out_dir / TRANSCRIPTS_DIR / f"e{episode:02d}_{agent}.json",
                result.transcript(agent),
            )
        records.append(record)
    trajectory = {
        "sequence_id": spec.sequence_id,
        "memory_enabled": spec.memory,
        "tasks": spec.tasks,
        **trajectory_metrics(records, criterion=spec.criterion),
    }
    _write(spec.out_dir / TRAJECTORY_FILE, trajectory)
    return trajectory


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2) + b"\n")
