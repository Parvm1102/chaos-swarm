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

SWARM_ENV_VARS = ("ANTHROPIC_API_KEY", "SWARM_LIVE", "SWARM_DEFAULT_BUDGET_USD")


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
