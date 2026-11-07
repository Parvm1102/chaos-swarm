"""Step 5 end to end, offline: a dry-run arm, two dry-run judges (one stopped early, as Sonnet
was by its suspension), pathways with a partial second judge, and the validation sample."""

import json
from pathlib import Path

import pytest

from swarm.analysis.judges import JudgeConfig, judgments_path, load_judgments
from swarm.arms import ArmConfig
from swarm.judging import compute_pathways, make_sample, run_judge
from swarm.llm.meter import CostMeter
from swarm.trajectories import run_arm


def read_json(path: str) -> dict:
    return json.loads(Path(path).read_text())


def judge(name: str) -> JudgeConfig:
    return JudgeConfig.model_validate(
        {
            "judge": name,
            "model": {"name": "claude-haiku-5-5", "max_tokens": 4096, "thinking": "adaptive",
                      "effort": "low"},
            "budget_usd": 1.0,
        }
    )  # fmt: skip


async def test_pathways_with_a_partial_second_judge(episode_task_dir):
    arm = ArmConfig.model_validate(
        {
            "arm": "judged",
            "model": {"name": "claude-haiku-5-5", "max_tokens": 4096},
            "budget_usd": 1.0,
            "task_dir": str(episode_task_dir),
            "sequences": "0-1",
            "episodes": 4,
            "concurrency": 2,
            "rules": "paper",
            "scripted_peer": "proposer",
        }
    )
    run = arm.run_config(dry_run=True)
    await run_arm(arm, dry_run=True, meter=CostMeter.for_run(run))
    for name, limit in (("first", None), ("second", 5), ("third", 7)):
        config = judge(name)
        meter = CostMeter.for_run(config.run_config(dry_run=True))
        await run_judge([arm], config, dry_run=True, meter=meter, limit=limit)
    run_dir = Path("runs/judged_dry")
    assert len(load_judgments(judgments_path(run_dir, "first"))) == 2 * 4 * 3
    report = compute_pathways([arm], primary="first", secondary="second", dry_run=True)
    assert not report["decision"]["switched"]
    assert report["coverage"]["judged"] == {"first": 24, "second": 5, "items": 24}
    compared = compute_pathways(
        [arm], primary="first", secondary="second", dry_run=True, compare=("third",)
    )
    assert compared["coverage"]["judged"]["third"] == 7
    coord = judge("coord")
    await run_judge(
        [arm], coord, dry_run=True, meter=CostMeter.for_run(coord.run_config(dry_run=True)),
        kind="coordination",
    )  # fmt: skip
    with_coord = compute_pathways(
        [arm], primary="first", secondary="second", dry_run=True, compare=("coord",)
    )
    assert with_coord["arms"]["judged"]["pathway_agreement_by_judge"]["coord"] == {
        "same": 2,
        "of": 2,
    }
    pathways_file = read_json("runs/judged_dry/pathways.json")
    assert pathways_file["by_judge"]["coord"]["notes_complete"] is False
    assert set(compared["pooled_item_agreement_by_judge"]) == {"second", "third"}
    assert compared["pooled_item_agreement"] == compared["pooled_item_agreement_by_judge"]["second"]
    compute_pathways([arm], primary="first", secondary="second", dry_run=True)  # plain again
    pathways = json.loads((run_dir / "pathways.json").read_text())
    # "second" labelled only 5 items (not every coordination item): no pathways from it
    assert set(pathways["by_judge"]) == {"first"} and pathways["pathway_agreement"] is None
    assert pathways["by_judge"]["first"]["notes_complete"] is True
    rows = pathways["by_judge"]["first"]["trajectories"]
    # the planted proposer: a proposal before the onset, but no agreement, so no coordination
    assert all(r["proposal_before_onset"] for r in rows)
    assert all(r["pathway"] == "responsive relaxation" for r in rows)
    summary = json.loads((run_dir / "summary.json").read_text())
    assert (
        summary["pathway"] == {"responsive relaxation": 2} and summary["pathway_judge"] == "first"
    )
    with pytest.raises(ValueError, match="has not labelled every item"):
        compute_pathways([arm], primary="second", secondary="first", dry_run=True)
    paths = make_sample(
        [arm], primary="first", secondary="second", n=6, seed=0, out_dir=Path("val"),
        dry_run=True,
    )  # fmt: skip
    key = json.loads(paths["key"].read_text())
    assert len(key) == 6 and all(set(v["labels"]) == {"first", "second"} for v in key.values())


async def test_a_breaker_trip_is_flagged_in_the_judge_summary(episode_task_dir, monkeypatch):
    from swarm.llm.client import text_response
    from swarm.llm.meter import RefusalBreakerTripped

    arm = ArmConfig.model_validate(
        {
            "arm": "judged2",
            "model": {"name": "claude-haiku-5-5", "max_tokens": 4096},
            "budget_usd": 1.0,
            "task_dir": str(episode_task_dir),
            "sequences": "0",
            "episodes": 4,
        }
    )
    await run_arm(arm, dry_run=True, meter=CostMeter.for_run(arm.run_config(dry_run=True)))
    refusal = text_response("", stop_reason="refusal").model_copy(
        update={"raw": {"stop_details": {"category": "reasoning_extraction"}}}
    )
    monkeypatch.setattr("swarm.judging.dry_run_script", lambda kind: lambda m, t: refusal)
    config = judge("refuser")
    meter = CostMeter.for_run(config.run_config(dry_run=True))
    with pytest.raises(RefusalBreakerTripped):
        await run_judge([arm], config, dry_run=True, meter=meter, kind="coordination")
    summary = read_json("runs/judges_refuser_dry/summary.json")
    assert (
        summary["aborted_by_breaker"] is True and summary["last_invocation_kind"] == "coordination"
    )
    assert summary["refusals_this_invocation"] == {"reasoning_extraction": 5}
    assert summary["arms"]["judged2"]["relaxation"] == {}  # --kind: no note was judged
