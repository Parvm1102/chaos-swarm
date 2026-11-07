"""Run one experiment arm: its trajectories in parallel, then its summary (`swarm trajectories`).

run_arm reads the arm's sequences from <task_dir>/sequences.json (checked against the task set's
manifest), runs one trajectory per sequence (at most `arm.concurrency` at once, all through one
client: one pacer, one cost meter), and writes into runs/<arm>/:
- arm.json: the arm config as run;
- trajectories/s<seq>/...: each trajectory's episode records, transcripts and trajectory.json;
- trajectories.jsonl: one line per trajectory (its metrics);
- summary.json: the pooled metrics (swarm.analysis.metrics.arm_summary) plus provenance: the
  prompt versions and tools version in the records, the task set's manifest digest, the sha256
  of sequences.json, and the model settings actually sent (thinking, effort);
- usage.jsonl: the cost meter's journal.
Running the same arm again resumes it (see swarm.agents.trajectory).

In dry-run mode both agents are ScriptedClients. By default B starts accepting in episode 2 and A
in episode 3 (a responsive pattern), so the metrics have something to measure.
"""

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson

from swarm.agents.episode import EpisodeAgent
from swarm.agents.trajectory import TrajectorySpec, run_trajectory
from swarm.analysis.metrics import arm_summary
from swarm.arms import ArmConfig
from swarm.calibrate import first_error
from swarm.episodes import scripted_episode_agent, scripted_proposer
from swarm.llm.client import AnthropicClient, LLMClient, ScriptedClient, thinking_config
from swarm.llm.meter import CostMeter
from swarm.tasks.generate import load_manifest
from swarm.tasks.sequences import SEQUENCES_FILE, SequencesFile

SUMMARY_FILE = "summary.json"
TRAJECTORIES_FILE = "trajectories.jsonl"
Behaviour = Callable[[str, int], dict[str, Any]]  # (agent, episode) -> scripted agent options


def responsive_dry_run(agent: str, episode: int) -> dict[str, Any]:
    """The default dry-run pair: B accepts from episode 2 on, A from episode 3 on."""
    return {"verdict": "ACCEPT" if episode >= {"A": 3, "B": 2}[agent] else "REJECT"}


def trajectory_specs(arm: ArmConfig, run_dir: Path, run: Any) -> list[TrajectorySpec]:
    sequences = SequencesFile.model_validate_json((arm.task_dir / SEQUENCES_FILE).read_bytes())
    if sequences.manifest_digest != load_manifest(arm.task_dir).digest():
        raise ValueError(f"{arm.task_dir / SEQUENCES_FILE} belongs to a different task set")
    by_id = {s.sequence_id: s for s in sequences.sequences}
    missing = [s for s in arm.sequences if s not in by_id]
    if missing:
        raise ValueError(f"no sequences {missing} in {arm.task_dir / SEQUENCES_FILE}")
    specs = []
    for sequence_id in arm.sequences:
        episodes = by_id[sequence_id].episodes
        if arm.episodes > len(episodes):
            raise ValueError(f"sequence {sequence_id} has only {len(episodes)} episodes")
        specs.append(
            TrajectorySpec(
                run=run,
                task_dir=arm.task_dir,
                sequence_id=sequence_id,
                tasks=[{"A": e.a, "B": e.b} for e in episodes[: arm.episodes]],
                memory=arm.memory,
                out_dir=run_dir / "trajectories" / f"s{sequence_id:02d}",
                rules=arm.rules,
                scripted={"B": arm.scripted_peer} if arm.scripted_peer else None,
            )
        )
    return specs


async def run_arm(
    arm: ArmConfig, *, dry_run: bool, meter: CostMeter, behaviour: Behaviour = responsive_dry_run
) -> dict[str, Any]:
    """Run (or resume) every trajectory of the arm and write the summary; return it."""
    run = arm.run_config(dry_run=dry_run)
    specs = trajectory_specs(arm, run.run_dir, run)
    _write(run.run_dir / "arm.json", arm.model_dump(mode="json"))

    def with_peer(base: Callable[[EpisodeAgent], LLMClient]) -> Callable[[EpisodeAgent], LLMClient]:
        """With a scripted peer, B is the harness-scripted proposer and only A is the model."""
        if not arm.scripted_peer:
            return base

        def choose(agent: EpisodeAgent) -> LLMClient:
            if agent.name == "B":
                return ScriptedClient(scripted_proposer(agent), run=run, meter=meter)
            return base(agent)

        return choose

    if dry_run:

        def scripted(agent: EpisodeAgent) -> LLMClient:
            options = behaviour(agent.name, agent.spec.episode)
            return ScriptedClient(scripted_episode_agent(agent, **options), run=run, meter=meter)

        trajectories = await _run_all(specs, with_peer(scripted), arm.concurrency)
    else:
        async with AnthropicClient(run, meter) as client:
            trajectories = await _run_all(specs, with_peer(lambda agent: client), arm.concurrency)

    with (run.run_dir / TRAJECTORIES_FILE).open("wb") as f:
        for trajectory in trajectories:
            f.write(orjson.dumps(trajectory) + b"\n")
    summary = {
        "arm": arm.arm,
        "description": arm.description,
        "dry_run": dry_run,
        "provenance": provenance(arm, run.run_dir),
        **arm_summary(trajectories),
        "meter_total_usd": meter.total_usd,
    }
    _write(run.run_dir / SUMMARY_FILE, summary)
    return summary


async def _run_all(
    specs: list[TrajectorySpec], client_for: Callable[[EpisodeAgent], LLMClient], concurrency: int
) -> list[dict[str, Any]]:
    gate = asyncio.Semaphore(concurrency)
    results: dict[int, dict[str, Any]] = {}

    async def one(spec: TrajectorySpec) -> None:
        async with gate:
            results[spec.sequence_id] = await run_trajectory(spec, client_for)

    try:
        async with asyncio.TaskGroup() as group:  # the first failure cancels the others
            for spec in specs:
                group.create_task(one(spec))
    except BaseExceptionGroup as failed:
        raise first_error(failed) from None
    return [results[spec.sequence_id] for spec in specs]


def provenance(arm: ArmConfig, run_dir: Path) -> dict[str, Any]:
    """What exactly ran: versions found in the records, task set, sequences, model settings."""
    prompt_versions: dict[str, set[str]] = {}
    tools_versions: set[str] = set()
    for path in sorted(run_dir.glob("trajectories/s*/episodes/e*.json")):
        record = orjson.loads(path.read_bytes())
        for name, version in record["prompt_versions"].items():
            prompt_versions.setdefault(name, set()).add(version)
        tools_versions.add(record["tools_version"])
    mixed = sorted(name for name, versions in prompt_versions.items() if len(versions) > 1)
    return {
        "provider": arm.provider,
        "model": arm.model.name,
        "thinking_sent": thinking_config(arm.model),
        "effort_sent": arm.model.effort,
        "max_tokens": arm.model.max_tokens,
        "cache_ttl": arm.cache_ttl,
        "memory": arm.memory,
        "rules": arm.rules,
        "scripted_peer": arm.scripted_peer,
        "sequences": arm.sequences,
        "episodes": arm.episodes,
        "task_dir": str(arm.task_dir),
        "manifest_digest": load_manifest(arm.task_dir).digest(),
        "sequences_digest": hashlib.sha256(
            (arm.task_dir / SEQUENCES_FILE).read_bytes()
        ).hexdigest(),
        "prompt_versions": {
            name: sorted(versions)[0] if len(versions) == 1 else sorted(versions)
            for name, versions in sorted(prompt_versions.items())
        },
        "prompt_versions_mixed": mixed,  # non-empty only if a resumed arm crossed a prompt change
        "tools_version": sorted(tools_versions)[0]
        if len(tools_versions) == 1
        else sorted(tools_versions),
    }


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2) + b"\n")
