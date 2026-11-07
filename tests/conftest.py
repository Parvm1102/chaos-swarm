"""Shared fixtures.

Every test runs offline, from an empty temp folder, with the swarm environment variables
cleared, so the real .env and runs/ are never read or written. Tests marked `live` keep the
real environment (and skip unless the shell exports SWARM_LIVE=1).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from swarm.config import CacheTTL, RunConfig
from swarm.llm.meter import CostMeter
from swarm.tasks.generate import generate_tasks

SWARM_ENV_VARS = ("ANTHROPIC_API_KEY", "SWARM_LIVE", "SWARM_DEFAULT_BUDGET_USD")


@pytest.fixture(scope="session")
def task_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small seed-0 task set (8 tasks, 2 per template), generated once per test session.

    Treat it as read-only: tests that need a modified database copy one first.
    """
    out = tmp_path_factory.mktemp("tasks")
    generate_tasks(base_seed=0, n=8, out_root=out)
    return out / "seed0"


@pytest.fixture(autouse=True)
def isolated_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if request.node.get_closest_marker("live") is not None:
        return
    for name in SWARM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def make_run() -> Callable[..., RunConfig]:
    """A RunConfig on Haiku with a $1 budget; keyword arguments override fields."""

    def make(**overrides: Any) -> RunConfig:
        data: dict[str, Any] = {
            "run_name": "test-run",
            "model": {"name": "claude-haiku-5-5"},
            "budget_usd": 1.0,
            "seed": 0,
        }
        data.update(overrides)
        return RunConfig.model_validate(data)

    return make


@pytest.fixture
def make_meter(tmp_path: Path) -> Callable[..., CostMeter]:
    """A CostMeter writing to <tmp>/run/usage.jsonl."""

    def make(budget_usd: float = 1.0, *, cache_ttl: CacheTTL = "5m") -> CostMeter:
        return CostMeter(tmp_path / "run", budget_usd, provider="anthropic", cache_ttl=cache_ttl)

    return make
