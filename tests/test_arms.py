"""Arm configs: YAML loading, the sequence ranges, and the checks that keep an arm runnable."""

from pathlib import Path

import pytest

from swarm.arms import ArmConfig, load_arm

ARMS = Path(__file__).resolve().parents[1] / "configs" / "arms"


def arm(**overrides) -> ArmConfig:
    data = {
        "arm": "test_arm",
        "model": {"name": "claude-haiku-5-5", "max_tokens": 4096},
        "budget_usd": 1.0,
        "task_dir": "data/tasks/seed0-hard",
        "sequences": "0-4",
    }
    data.update(overrides)
    return ArmConfig.model_validate(data)


def test_the_committed_arms_load():
    baseline = load_arm(ARMS / "haiku_baseline.yaml")
    assert baseline.sequences == list(range(10)) and baseline.memory and baseline.episodes == 10
    off = load_arm(ARMS / "haiku_memory_off.yaml")
    assert off.sequences == [10, 11, 12, 13, 14] and not off.memory
    spot = load_arm(ARMS / "sonnet_spot.yaml")
    assert (spot.model.name, spot.model.thinking, spot.model.effort) == (
        "claude-sonnet-5-5",
        "adaptive",
        "low",
    )


@pytest.mark.parametrize(
    ("value", "expected"), [("3-5", [3, 4, 5]), ("7", [7]), (2, [2]), ([4, 1], [1, 4])]
)
def test_sequence_ranges(value, expected):
    assert arm(sequences=value).sequences == expected


def test_a_dry_run_gets_its_own_folder_and_two_calls_per_trajectory():
    run = arm(concurrency=5).run_config(dry_run=True)
    assert run.run_name == "test_arm_dry" and run.dry_run and run.concurrency == 10


@pytest.mark.parametrize(
    "bad",
    [
        {"sequences": [1, 1]},
        {"sequences": []},
        {"model": {"name": "claude-haiku-5-5"}},  # max_tokens 1,024 is too small for episodes
        {"model": {"name": "claude-haiku-5-5", "max_tokens": 4096, "effort": "low"}},
        {"unknown_key": 1},
    ],
)
def test_bad_arms_are_refused(bad):
    with pytest.raises(ValueError):
        arm(**bad)
