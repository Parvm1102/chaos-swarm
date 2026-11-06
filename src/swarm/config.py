"""Run configuration and environment settings.

Two locks guard every live API call:

1. the run config says ``dry_run: false`` (the default is ``true``), and
2. ``SWARM_LIVE`` is true in the environment (a shell variable, or the ``.env`` file).

A ``RunConfig`` that asks for a live run while ``SWARM_LIVE`` is off cannot be constructed,
and ``AnthropicClient`` checks both locks again before it exists.
"""

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RUNS_DIR = Path("runs")

# One provider per run: CLAUDE.md forbids mixing providers inside an arm or a pair.
# AgentRouter joins this Literal in its own step.
Provider = Literal["anthropic"]
CacheTTL = Literal["5m", "1h"]


class LiveCallRefused(RuntimeError):
    """Something asked for a live API call while a live switch was off."""


class Settings(BaseSettings):
    """Values from the process environment, then from ``.env`` in the current directory.

    Shell variables win over ``.env``. Empty values count as unset.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    ANTHROPIC_API_KEY: SecretStr | None = None
    SWARM_LIVE: bool = False
    SWARM_DEFAULT_BUDGET_USD: float = Field(default=5.0, gt=0)

    def anthropic_api_key(self) -> str | None:
        """The key as plain text, or None when it is missing or blank."""
        if self.ANTHROPIC_API_KEY is None:
            return None
        return self.ANTHROPIC_API_KEY.get_secret_value().strip() or None


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    max_tokens: int = Field(default=1024, gt=0)
    # Recorded for provenance only. The SDK (2.x) and API version 2026-10-21 reject
    # `temperature` at any value, so it is never sent: every call samples at the default, 1.0.
    temperature: float = 1.0
    # v1 runs with extended thinking off. A budget could not be sent anyway: `budget_tokens`
    # is a 400 on all three 5.5 models (a later "thinking on" would be adaptive + effort).
    thinking_budget: int | None = None

    @field_validator("temperature")
    @classmethod
    def _temperature_is_the_api_default(cls, value: float) -> float:
        if value != 1.0:
            raise ValueError("temperature cannot be changed (the API rejects it); leave it at 1.0")
        return value

    @field_validator("thinking_budget")
    @classmethod
    def _no_extended_thinking_in_v1(cls, value: int | None) -> int | None:
        if value is not None:
            raise ValueError("thinking_budget must stay None in v1 (no extended thinking)")
        return value


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # becomes runs/<run_name>/
    provider: Provider = "anthropic"
    model: ModelConfig
    budget_usd: float = Field(gt=0)  # this run's USD cap, for its one provider
    dry_run: bool = True
    concurrency: int = Field(default=8, ge=1)  # max API calls in flight at once
    # "5m" by default: every cache hit refreshes the TTL, and turns inside an episode are
    # seconds apart. Use "1h" only for runs that may pause between calls (writes cost 2x input).
    cache_ttl: CacheTTL = "5m"
    seed: int

    @model_validator(mode="after")
    def _live_run_needs_swarm_live(self) -> Self:
        if not self.dry_run and not Settings().SWARM_LIVE:
            raise LiveCallRefused(
                f"run {self.run_name!r} has dry_run=false but SWARM_LIVE is off; refusing"
            )
        return self

    @property
    def run_dir(self) -> Path:
        return RUNS_DIR / self.run_name


def load_run_config(path: Path) -> RunConfig:
    """Load a run config from a YAML file.

    The one environment override: if the YAML has no ``budget_usd``,
    ``SWARM_DEFAULT_BUDGET_USD`` fills it in. Nothing else is read from the environment,
    so the YAML file alone describes an experiment arm.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    if "budget_usd" not in data:
        data["budget_usd"] = Settings().SWARM_DEFAULT_BUDGET_USD
    return RunConfig.model_validate(data)
