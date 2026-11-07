"""`swarm calibrate --dry-run` end to end, task picking, and the summary numbers."""

from collections import Counter
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from swarm.calibrate import ids_from_result, pick_tasks, wilson
from swarm.cli import app
from swarm.tasks.generate import load_manifest

SUMMARY_KEYS = {
    "run_name",
    "provider",
    "model",
    "cache_ttl",
    "thinking",
    "dry_run",
    "seed",
    "task_dir",
    "manifest_digest",
    "prompt_version",
    "tools_version",
    "max_turns",
    "max_tokens",
    "n_tasks",
    "task_ids",
    "accuracy",
    "accuracy_non_peek",
    "peek_rate",
    "answer_saved_rate",
    "per_template",
    "log_chars",
    "query_count_median",
    "turns_median",
    "wall_s_median",
    "cost",
    "tokens",
    "cache_read_fraction",
    "stop_causes",
    "totals",
}


def read_jsonl(path: Path) -> list[dict]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def test_dry_run_calibration_end_to_end(task_dir):
    runner = CliRunner()
    args = ["calibrate", "--dry-run", "--n", "8", "--task-dir", str(task_dir)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output

    run_dir = Path("runs") / "calib_claude-sonnet-5-5_5m_dry"  # the default model and TTL
    records = read_jsonl(run_dir / "calibration.jsonl")
    assert len(records) == 8
    assert Counter(record["template"] for record in records) == {"T1": 2, "T2": 2, "T3": 2, "T4": 2}
    assert all(record["outcome"]["correct"] for record in records)
    assert {record["model"] for record in records} == {"claude-sonnet-5-5"}

    summary = orjson.loads((run_dir / "summary.json").read_bytes())
    assert set(summary) >= SUMMARY_KEYS
    assert summary["dry_run"] is True
    assert summary["accuracy"] == {
        "k": 8,
        "n": 8,
        "rate": 1.0,
        "ci95": pytest.approx([0.676, 1.0], abs=1e-3),
    }
    assert summary["peek_rate"]["k"] == 0
    assert summary["stop_causes"] == {"end_turn": 8}
    assert summary["cost"]["total_usd"] == 0.0
    assert summary["cache_read_fraction"] is None  # scripted calls report no tokens
    assert summary["task_ids"] == [record["task_id"] for record in records]

    assert len(read_jsonl(run_dir / "usage.jsonl")) == 8 * 4  # every scripted call was metered
    assert len(list((run_dir / "transcripts").glob("task_*.json"))) == 8

    # The same folder is refused without --force, and replaced with it.
    again = runner.invoke(app, args)
    assert again.exit_code == 1 and "--force" in again.output
    forced = runner.invoke(app, [*args, "--force"])
    assert forced.exit_code == 0, forced.output
    assert len(read_jsonl(run_dir / "calibration.jsonl")) == 8


def test_a_live_run_is_refused_without_swarm_live(task_dir):
    result = CliRunner().invoke(
        app,
        [
            "calibrate",
            "--model",
            "anthropic/claude-haiku-5-5",
            "--n",
            "4",
            "--task-dir",
            str(task_dir),
        ],
    )
    assert result.exit_code == 2 and "refused" in result.output
    assert not Path("runs").exists()


def test_an_unknown_model_is_rejected(task_dir):
    result = CliRunner().invoke(
        app,
        ["calibrate", "--dry-run", "--model", "anthropic/claude-nope", "--task-dir", str(task_dir)],
    )
    assert result.exit_code == 1 and "unknown model" in result.output


def test_task_picking_is_stratified_and_deterministic(task_dir):
    manifest = load_manifest(task_dir)
    template = {task.task_id: task.template for task in manifest.tasks}
    picked = pick_tasks(manifest, 6, seed=0)
    assert picked == pick_tasks(manifest, 6, seed=0)
    assert Counter(template[task_id] for task_id in picked) == {"T1": 2, "T2": 2, "T3": 1, "T4": 1}
    assert len(set(picked)) == 6
    with pytest.raises(ValueError):
        pick_tasks(manifest, 9, seed=0)


def test_wilson_intervals():
    assert wilson(0, 0) == {"k": 0, "n": 0, "rate": None, "ci95": None}
    interval = wilson(18, 20)
    assert interval["rate"] == 0.9
    assert interval["ci95"] == pytest.approx([0.699, 0.972], abs=1e-3)


def test_ids_are_read_back_from_a_result():
    assert ids_from_result("id\n4\n17\n(2 rows)") == [4, 17]
    assert ids_from_result("id\n(0 rows)") == []
