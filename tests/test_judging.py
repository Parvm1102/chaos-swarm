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
    for name, limit in (("first", None), ("second", 5)):
        config = judge(name)
        meter = CostMeter.for_run(config.run_config(dry_run=True))
        await run_judge([arm], config, dry_run=True, meter=meter, limit=limit)
    run_dir = Path("runs/judged_dry")
    assert len(load_judgments(judgments_path(run_dir, "first"))) == 2 * 4 * 3
    report = compute_pathways([arm], primary="first", secondary="second", dry_run=True)
    assert not report["decision"]["switched"]
    assert report["coverage"]["judged"] == {"first": 24, "second": 5, "items": 24}
    pathways = json.loads((run_dir / "pathways.json").read_text())
    assert set(pathways["by_judge"]) == {"first"} and pathways["pathway_agreement"] is None
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
