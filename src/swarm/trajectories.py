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
Running the same arm again resumes it (see swarm.agents.trajectory). summarize_arm recomputes
trajectories.jsonl and summary.json from the stored episode records alone (no API calls), e.g.
after a metric changes (`swarm summarize`).

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
from swarm.agents.trajectory import (
    TRAJECTORY_FILE,
    TrajectorySpec,
    load_records,
    run_trajectory,
)
from swarm.analysis.metrics import arm_summary, trajectory_metrics
from swarm.arms import ArmConfig
from swarm.calibrate import first_error
from swarm.episodes import scripted_episode_agent, scripted_proposer
from swarm.llm.client import AnthropicClient, LLMClient, ScriptedClient, thinking_config
from swarm.llm.meter import (
    USAGE_FILE,
    CostMeter,
    RefusalBreakerTripped,
    breaker_history,
    summarize_usage_file,
)
from swarm.tasks.generate import load_manifest
from swarm.tasks.sequences import SEQUENCES_FILE, SequencesFile

SUMMARY_FILE = "summary.json"
TRAJECTORIES_FILE = "trajectories.jsonl"
# (agent, episode, sequence_id) -> scripted agent options
Behaviour = Callable[[str, int, int], dict[str, Any]]


def responsive_dry_run(agent: str, episode: int, sequence_id: int = 0) -> dict[str, Any]:
    """The default dry-run pair: B accepts from episode 2 on, A from episode 3 on."""
    return {"verdict": "ACCEPT" if episode >= {"A": 3, "B": 2}[agent] else "REJECT"}


def e1_dry_run(agent: str, episode: int, sequence_id: int = 0) -> dict[str, Any]:
    """The Experiment 1 dry-run pair: responsive verdicts, three queries (over the cap) in
    episodes 3, 6 and 9, one peek by A in episode 2, and records pasted when the channel fits."""
    return {
        **responsive_dry_run(agent, episode),
        "extra_queries": 2 if episode % 3 == 0 else 0,
        "peek": agent == "A" and episode == 2,
        "paste_record": True,
    }


def e3_dry_run(agent: str, episode: int, sequence_id: int = 0) -> dict[str, Any]:
    """The Experiment 3 dry-run pair (tool arms): responsive verdicts; A reports in episode 2
    on even sequences, nobody reports on odd ones; records are pasted when the channel fits."""
    return {
        **responsive_dry_run(agent, episode),
        "report": agent == "A" and episode == 2 and sequence_id % 2 == 0,
        "paste_record": True,
    }


def prereg_provenance(arm: ArmConfig) -> dict[str, str] | None:
    """The pre-registration this arm runs under: its path and the sha256 of its bytes."""
    if arm.prereg is None:
        return None
    path = Path(arm.prereg)
    if not path.is_file():
        raise ValueError(f"arm {arm.arm} names prereg {path}, which does not exist")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def check_prereg(arm: ArmConfig, run_dir: Path) -> dict[str, str] | None:
    """The launch guard: the prereg must exist, and must not have changed since the arm's first
    launch (its hash is kept in arm.json). Returns the provenance to store."""
    current = prereg_provenance(arm)
    previous_path = run_dir / "arm.json"
    if current is not None and previous_path.exists():
        first = orjson.loads(previous_path.read_bytes()).get("prereg_first_launch")
        if first is not None and first["sha256"] != current["sha256"]:
            raise ValueError(
                f"{current['path']} changed since arm {arm.arm} first launched "
                f"({first['sha256'][:12]} -> {current['sha256'][:12]}); refusing to run"
            )
        if first is not None:
            return first
    return current


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
                message_limit=arm.message_limit,
                task_prompt=arm.task_prompt,
                query_cap=arm.query_cap,
                criterion=arm.collusion_criterion,
                operator_policy=arm.operator_policy,
                operator_rules=arm.operator_rules,
                operator_checkin=arm.operator_checkin,
                intro_prompt=arm.intro_prompt,
            )
        )
    return specs


async def run_arm(
    arm: ArmConfig, *, dry_run: bool, meter: CostMeter, behaviour: Behaviour | None = None
) -> dict[str, Any]:
    """Run (or resume) every trajectory of the arm and write the summary; return it."""
    run = arm.run_config(dry_run=dry_run)
    if behaviour is None:
        behaviour = (
            e3_dry_run
            if arm.operator_policy is not None
            else e1_dry_run
            if arm.query_cap is not None
            else responsive_dry_run
        )
    specs = trajectory_specs(arm, run.run_dir, run)
    first_prereg = check_prereg(arm, run.run_dir)
    _write(
        run.run_dir / "arm.json",
        {**arm.model_dump(mode="json"), "prereg_first_launch": first_prereg},
    )

    def with_peer(base: Callable[[EpisodeAgent], LLMClient]) -> Callable[[EpisodeAgent], LLMClient]:
        """With a scripted peer, B is the harness-scripted proposer and only A is the model."""
        if not arm.scripted_peer:
            return base

        def choose(agent: EpisodeAgent) -> LLMClient:
            if agent.name == "B":
                return ScriptedClient(scripted_proposer(agent), run=run, meter=meter)
            return base(agent)

        return choose

    try:
        if dry_run:

            def scripted(agent: EpisodeAgent) -> LLMClient:
                options = behaviour(agent.name, agent.spec.episode, agent.spec.sequence_id)
                return ScriptedClient(
                    scripted_episode_agent(agent, **options), run=run, meter=meter
                )

            trajectories = await _run_all(specs, with_peer(scripted), arm.concurrency)
        else:
            async with AnthropicClient(run, meter) as client:
                trajectories = await _run_all(
                    specs, with_peer(lambda agent: client), arm.concurrency
                )
    except RefusalBreakerTripped as trip:
        mark_aborted(run.run_dir, trip.details, arm=arm.arm, dry_run=dry_run)
        raise

    del trajectories  # the summary is recomputed from the records, the single source
    return write_summary(arm, run.run_dir, dry_run=dry_run, meter_total_usd=meter.total_usd)


def write_summary(
    arm: ArmConfig, run_dir: Path, *, dry_run: bool, meter_total_usd: float
) -> dict[str, Any]:
    """Recompute every trajectory's metrics from its episode records (default criterion), then
    write trajectory.json files, trajectories.jsonl and summary.json (with the recounts)."""
    records_by_trajectory, trajectories = [], []
    for sequence_id in arm.sequences:
        out_dir = run_dir / "trajectories" / f"s{sequence_id:02d}"
        records = load_records(out_dir)
        if not records:
            raise ValueError(f"{out_dir} has no episode records")
        previous = orjson.loads((out_dir / TRAJECTORY_FILE).read_bytes())
        trajectory = {
            "sequence_id": sequence_id,
            "memory_enabled": arm.memory,
            "tasks": previous["tasks"],
            **trajectory_metrics(records, criterion=arm.collusion_criterion),
        }
        _write(out_dir / TRAJECTORY_FILE, trajectory)
        records_by_trajectory.append(records)
        trajectories.append(trajectory)
    with (run_dir / TRAJECTORIES_FILE).open("wb") as f:
        for trajectory in trajectories:
            f.write(orjson.dumps(trajectory) + b"\n")
    summary = {
        "arm": arm.arm,
        "description": arm.description,
        "dry_run": dry_run,
        "provenance": provenance(arm, run_dir),
        **arm_summary(trajectories, records_by_trajectory=records_by_trajectory),
        "meter_total_usd": meter_total_usd,
        "aborted_by_breaker": False,  # this summary comes from a run that finished
        "breaker_trips": breaker_history(run_dir),  # earlier invocations that tripped
    }
    _write(run_dir / SUMMARY_FILE, summary)
    from swarm.judging import attach_pathways  # judging imports this module

    attach_pathways(run_dir)  # keep the step-5 pathway counts, if the judges have run
    return orjson.loads((run_dir / SUMMARY_FILE).read_bytes())


def mark_aborted(run_dir: Path, details: dict[str, Any], **identity: Any) -> None:
    """Record a refusal-breaker abort in summary.json: the existing summary gains the flag, or
    a stub is written. A later run that finishes rewrites the summary (flag false, history kept)."""
    path = run_dir / SUMMARY_FILE
    summary = orjson.loads(path.read_bytes()) if path.exists() else {**identity, "complete": False}
    summary["aborted_by_breaker"] = True
    summary["breaker"] = details
    summary["breaker_trips"] = breaker_history(run_dir)
    _write(path, summary)


def summarize_arm(arm: ArmConfig, *, dry_run: bool = False) -> dict[str, Any]:
    """Recompute an existing arm's summary offline, from its records and its usage journal."""
    run_dir = arm.run_dir(dry_run=dry_run)
    usage = run_dir / USAGE_FILE
    total = summarize_usage_file(usage)["total_usd"] if usage.exists() else 0.0
    return write_summary(arm, run_dir, dry_run=dry_run, meter_total_usd=total)


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
    auto_inspect: set[bool] = set()
    for path in sorted(run_dir.glob("trajectories/s*/episodes/e*.json")):
        record = orjson.loads(path.read_bytes())
        for name, version in record["prompt_versions"].items():
            prompt_versions.setdefault(name, set()).add(version)
        tools_versions.add(record["tools_version"])
        auto_inspect.add(record.get("auto_inspect", False))  # absent before step 5
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
        "message_limit": arm.message_limit,
        "task_prompt": arm.task_prompt,
        "query_cap": arm.query_cap,
        "collusion_criterion": arm.collusion_criterion,
        "operator_policy": arm.operator_policy,
        "operator_rules": arm.operator_rules,
        "operator_checkin": arm.operator_checkin,
        "intro_prompt": arm.intro_prompt,
        "prereg": prereg_provenance(arm),
        "auto_inspect": sorted(auto_inspect)[0] if len(auto_inspect) == 1 else "mixed",
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
