"""Trajectories with scripted agents: memory threading, memory off, resume after a failure, the arm
summary with its provenance, and `swarm trajectories --dry-run`."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from swarm.agents.trajectory import TrajectorySpec, load_records, run_trajectory
from swarm.arms import ArmConfig
from swarm.cli import app
from swarm.episodes import scripted_episode_agent
from swarm.llm.client import ScriptedClient
from swarm.llm.meter import CostMeter
from swarm.prompts import load_prompt
from swarm.trajectories import run_arm


def make_arm(task_dir, **overrides) -> ArmConfig:
    data = {
        "arm": "traj_test",
        "model": {"name": "claude-haiku-5-5", "max_tokens": 4096},
        "budget_usd": 1.0,
        "task_dir": str(task_dir),
        "sequences": "0-1",
        "episodes": 4,
        "concurrency": 2,
    }
    data.update(overrides)
    return ArmConfig.model_validate(data)


def first_message(run_dir: Path, sequence: int, episode: int, agent: str = "A") -> str:
    path = (
        run_dir
        / "trajectories"
        / f"s{sequence:02d}"
        / "transcripts"
        / f"e{episode:02d}_{agent}.json"
    )
    return json.loads(path.read_text())["messages"][0]["content"]


async def test_an_arm_with_memory(episode_task_dir):
    arm = make_arm(episode_task_dir)
    run = arm.run_config(dry_run=True)
    summary = await run_arm(arm, dry_run=True, meter=CostMeter.for_run(run))

    assert summary["trajectories"] == 2 and summary["episodes"] == 8
    # B accepts from episode 2, A from 3: onset 3, a responsive start, converged.
    assert summary["onset"]["by_episode"] == {"3": 2}
    assert summary["provisional_pathway"] == {"responsive": 2}
    assert summary["tc"]["k"] == 2 and summary["cc"]["k"] == 2
    assert "pathway" not in summary
    prov = summary["provenance"]
    assert prov["thinking_sent"] == {"type": "disabled"} and prov["effort_sent"] is None
    assert len(prov["manifest_digest"]) == 64 and len(prov["sequences_digest"]) == 64
    assert {"episode_rules", "task_phase_system", "memory_episode"} <= set(prov["prompt_versions"])
    assert prov["prompt_versions_mixed"] == [] and isinstance(prov["tools_version"], str)

    assert first_message(run.run_dir, 0, 1).count("None yet: this is your first episode.") == 1
    second = first_message(run.run_dir, 0, 2)
    assert "Episode 1\nMessages:\n- Round 1, you:" in second
    assert "Your note to yourself:\nScripted notes for my future self." in second
    fourth = first_message(run.run_dir, 0, 4)
    assert all(f"Episode {e}\nMessages:" in fourth for e in (1, 2, 3))

    lines = (run.run_dir / "trajectories.jsonl").read_text().splitlines()
    assert [json.loads(line)["sequence_id"] for line in lines] == [0, 1]
    record = json.loads((run.run_dir / "trajectories/s00/episodes/e02.json").read_text())
    assert record["memory_enabled"] is True and record["channel"][0]["sender"] == "A"


async def test_memory_off_shows_no_earlier_episode(episode_task_dir):
    arm = make_arm(episode_task_dir, arm="traj_off", memory=False, sequences="1")
    run = arm.run_config(dry_run=True)
    await run_arm(arm, dry_run=True, meter=CostMeter.for_run(run))
    second = first_message(run.run_dir, 1, 2)
    assert "None available." in second and "Messages:" not in second


async def test_a_trajectory_resumes_after_a_failure(episode_task_dir, tmp_path):
    arm = make_arm(episode_task_dir)
    run = arm.run_config(dry_run=True)
    meter = CostMeter.for_run(run)
    spec = TrajectorySpec(
        run=run,
        task_dir=episode_task_dir,
        sequence_id=0,
        tasks=[
            {"A": "task_0000", "B": "task_0001"},
            {"A": "task_0002", "B": "task_0003"},
            {"A": "task_0004", "B": "task_0005"},
        ],
        memory=True,
        out_dir=tmp_path / "s00",
    )

    def failing_at(episode: int):
        def client_for(agent):
            if agent.spec.episode == episode:
                raise RuntimeError("simulated crash")
            return ScriptedClient(scripted_episode_agent(agent), run=run, meter=meter)

        return client_for

    with pytest.raises(RuntimeError, match="simulated crash"):
        await run_trajectory(spec, failing_at(3))
    assert [r["episode"] for r in load_records(spec.out_dir)] == [1, 2]
    assert not (spec.out_dir / "trajectory.json").exists()

    seen = []

    def resuming(agent):
        seen.append(agent.spec.episode)
        return ScriptedClient(scripted_episode_agent(agent), run=run, meter=meter)

    trajectory = await run_trajectory(spec, resuming)
    assert set(seen) == {3}  # episodes 1 and 2 were not run again
    assert trajectory["episodes"] == 3
    third = json.loads((spec.out_dir / "transcripts" / "e03_A.json").read_text())
    assert "Episode 2\nMessages:" in third["messages"][0]["content"]  # memory rebuilt from records


def test_swarm_trajectories_dry_run(episode_task_dir, tmp_path):
    arm_file = tmp_path / "arm.yaml"
    arm_file.write_text(
        "arm: cli_arm\nmodel: {name: claude-haiku-5-5, max_tokens: 4096}\nbudget_usd: 1\n"
        f"task_dir: {episode_task_dir}\nsequences: 0-1\nepisodes: 2\nconcurrency: 2\n"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["trajectories", "--arm", str(arm_file), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "TC (any mutual ACCEPT)" in result.output
    summary = json.loads(Path("runs/cli_arm_dry/summary.json").read_text())
    assert summary["dry_run"] is True and summary["trajectories"] == 2
    again = runner.invoke(app, ["trajectories", "--arm", str(arm_file), "--dry-run"])
    resumed = " ".join(again.output.split())  # rich wraps long lines
    assert again.exit_code == 0 and "2 trajectories done" in resumed  # nothing rerun


def test_a_live_arm_is_refused_without_swarm_live(episode_task_dir, tmp_path):
    arm_file = tmp_path / "arm.yaml"
    arm_file.write_text(
        "arm: live_arm\nmodel: {name: claude-haiku-5-5, max_tokens: 4096}\nbudget_usd: 1\n"
        f"task_dir: {episode_task_dir}\nsequences: 0\n"
    )
    result = CliRunner().invoke(app, ["trajectories", "--arm", str(arm_file)])
    assert result.exit_code == 2 and "refused" in result.output


async def test_a_scripted_proposer_peer(episode_task_dir):
    arm = make_arm(
        episode_task_dir,
        arm="traj_proposer",
        rules="paper",
        scripted_peer="proposer",
        sequences="0",
    )
    run = arm.run_config(dry_run=True)
    summary = await run_arm(arm, dry_run=True, meter=CostMeter.for_run(run))
    assert summary["provenance"]["rules"] == "paper"
    assert summary["provenance"]["scripted_peer"] == "proposer"
    assert {"peer_proposal", "peer_proposal_repeat"} <= set(
        summary["provenance"]["prompt_versions"]
    )
    assert summary["accept_rate"]["B"]["k"] == 4  # the proposer always accepts
    record = json.loads((run.run_dir / "trajectories/s00/episodes/e01.json").read_text())
    b = record["agents"]["B"]
    assert b["model"] == "scripted:proposer" and b["scripted"] == "proposer"
    assert record["agents"]["A"]["scripted"] is None
    assert [m["round"] for m in b["messages_sent"]] == [1, 3] and b["silent_rounds"] == [2, 4, 5]
    proposal = load_prompt("peer_proposal").text.strip()
    assert b["messages_sent"][0]["text"] == proposal
    assert proposal in first_message(run.run_dir, 0, 2)  # A remembers the proposal


def test_every_work_record_starts_with_the_automatic_inspect_and_summarize_reproduces(
    episode_task_dir, tmp_path
):
    arm_file = tmp_path / "arm.yaml"
    arm_file.write_text(
        "arm: auto_arm\nmodel: {name: claude-haiku-5-5, max_tokens: 4096}\nbudget_usd: 1\n"
        f"task_dir: {episode_task_dir}\nsequences: 0-1\nepisodes: 2\nconcurrency: 2\n"
    )
    runner = CliRunner()
    assert runner.invoke(app, ["trajectories", "--arm", str(arm_file), "--dry-run"]).exit_code == 0
    run_dir = Path("runs/auto_arm_dry")
    for path in run_dir.glob("trajectories/s*/transcripts/e*_*.json"):
        raw_log = json.loads(path.read_text())["raw_log"]
        assert raw_log[0]["automatic"] is True and raw_log[0]["tool"] == "inspect_database"
        assert len(json.dumps(raw_log)) > 4_000
    for path in run_dir.glob("trajectories/s*/episodes/e*.json"):
        record = json.loads(path.read_text())
        assert record["auto_inspect"] is True and "task_schema" in record["prompt_versions"]
        assert all(record["agents"][a]["peer_log_chars"] > 4_000 for a in "AB")
    assert "The database schema" in first_message(run_dir, 0, 1)
    before = json.loads((run_dir / "summary.json").read_text())
    assert before["provenance"]["auto_inspect"] is True and before["criterion"] == "tightened"
    result = runner.invoke(app, ["summarize", "--arm", str(arm_file), "--dry-run"])
    assert result.exit_code == 0, result.output
    after = json.loads((run_dir / "summary.json").read_text())
    assert {k: v for k, v in after.items() if k != "meter_total_usd"} == {
        k: v for k, v in before.items() if k != "meter_total_usd"
    }


async def test_the_refusal_breaker_aborts_an_arm_and_the_arm_resumes(episode_task_dir):
    from swarm.llm.meter import RefusalBreakerTripped

    arm = make_arm(episode_task_dir, arm="breaker_arm")
    run = arm.run_config(dry_run=True)
    with pytest.raises(RefusalBreakerTripped):
        # both agents refuse at the end of every task phase: the 5th refusal comes in episode 3
        await run_arm(
            arm,
            dry_run=True,
            meter=CostMeter.for_run(run),
            behaviour=lambda agent, episode, sequence_id: {"refuse_in_task": True},
        )
    summary = json.loads((run.run_dir / "summary.json").read_text())
    assert summary["aborted_by_breaker"] is True
    assert summary["breaker"]["refusals"] == {"reasoning_extraction": 5}
    done = [len(load_records(run.run_dir / "trajectories" / f"s{s:02d}")) for s in (0, 1)]
    assert sum(done) < 8  # stopped part-way
    resumed = await run_arm(arm, dry_run=True, meter=CostMeter.for_run(run))
    assert resumed["aborted_by_breaker"] is False and len(resumed["breaker_trips"]) == 1
    assert resumed["episodes"] == 8
