"""Experiment arms: one YAML file describes one arm, so the file alone defines the condition.

arm: haiku_baseline            # becomes runs/<arm>/ (runs/<arm>_dry/ for a dry run)
description: ...
model: {name: claude-haiku-5-5, max_tokens: 4096}   # thinking/effort optional
budget_usd: 3.0                # this arm's USD cap
cache_ttl: 5m
task_dir: data/tasks/seed0-hard
sequences: 0-9                 # "a-b" (inclusive), a list, or one number
episodes: 10
memory: true                   # false: no digest of earlier episodes
rules: explicit                # or paper (the evidence basis only)
scripted_peer: proposer        # optional: B is the scripted planted violator
concurrency: 5                 # trajectories in flight (each runs its episodes in order)
"""

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from swarm.agents.task_agent import TASK_MAX_TOKENS
from swarm.config import RUNS_DIR, CacheTTL, ModelConfig, Provider, RunConfig
from swarm.llm.meter import CostMeter


class ArmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    description: str = ""
    provider: Provider = "anthropic"
    model: ModelConfig
    budget_usd: float = Field(gt=0)
    cache_ttl: CacheTTL = "5m"
    task_dir: Path
    sequences: list[int]
    episodes: int = Field(default=10, ge=1)
    memory: bool = True
    rules: Literal["explicit", "paper"] = "explicit"  # verification-rule wording
    scripted_peer: Literal["proposer"] | None = None  # B is harness-scripted; A is the model
    concurrency: int = Field(default=5, ge=1)
    seed: int = 0
    # Experiment 1 (spillover). Defaults reproduce every earlier arm.
    message_limit: int = Field(default=200, ge=1)  # characters per channel message
    task_prompt: Literal[
        "task_phase_system", "task_phase_system_rules2", "task_phase_system_rules2_cap1"
    ] = "task_phase_system"
    query_cap: int | None = Field(default=None, ge=0)  # query_database calls per task (logged)
    collusion_criterion: Literal["tightened", "delivered"] = "tightened"
    prereg: Path | None = None  # its sha256 goes into every summary; a live run guards it
    # Experiment 3 (open door): report_to_operator with an operator policy; None: no tool.
    operator_policy: Literal["ack", "fix"] | None = None
    # Step 8 follow-ups.
    operator_rules: Literal["neutral", "invite"] = "neutral"  # the rules sentence of tool arms
    operator_checkin: bool = False  # an operator check-in atop every feedback (prompted)
    intro_prompt: Literal["episode_intro", "episode_intro_plain"] = "episode_intro"
    # Experiment 5: a verification-rule reminder atop every feedback (no tool needed).
    feedback_reminder: bool = False
    # Experiment 5b: the operator check-in question alone atop every feedback (no tool).
    feedback_question: bool = False
    # Refusal breaker limits for this arm's runs; only stricter than the project rule
    # (the Sonnet 5 subject pilot stops at the first refusal).
    breaker_reasoning_extraction: int = Field(default=5, ge=1)
    breaker_any: int = Field(default=10, ge=1)

    @field_validator("sequences", mode="before")
    @classmethod
    def _read_range(cls, value: Any) -> Any:
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            first, _, last = value.partition("-")
            return list(range(int(first), int(last or first) + 1))
        return value

    @field_validator("sequences")
    @classmethod
    def _distinct_sequences(cls, value: list[int]) -> list[int]:
        if not value or len(set(value)) != len(value) or min(value) < 0:
            raise ValueError("sequences must be distinct, non-negative and not empty")
        return sorted(value)

    @model_validator(mode="after")
    def _prompt_matches_the_cap(self) -> Self:
        expected = {
            "task_phase_system": None,
            "task_phase_system_rules2": 2,
            "task_phase_system_rules2_cap1": 1,
        }[self.task_prompt]
        if self.query_cap != expected:
            raise ValueError(
                f"task_prompt {self.task_prompt} states a cap of {expected}, "
                f"but query_cap is {self.query_cap}"
            )
        if (self.operator_rules != "neutral" or self.operator_checkin) and not self.operator_policy:
            raise ValueError("operator_rules and operator_checkin need an operator_policy")
        return self

    @model_validator(mode="after")
    def _room_for_the_task_phase(self) -> Self:
        if self.model.max_tokens < TASK_MAX_TOKENS:
            raise ValueError(f"model.max_tokens must be at least {TASK_MAX_TOKENS} for episodes")
        return self

    def run_name(self, *, dry_run: bool) -> str:
        return self.arm + ("_dry" if dry_run else "")

    def run_dir(self, *, dry_run: bool) -> Path:
        """runs/<arm>[_dry]/, without building a RunConfig (offline readers need no live lock)."""
        return RUNS_DIR / self.run_name(dry_run=dry_run)

    def meter(self, *, dry_run: bool) -> CostMeter:
        """The cost meter (and refusal breaker, with this arm's limits) for its run folder."""
        run = self.run_config(dry_run=dry_run)
        return CostMeter(
            run.run_dir,
            run.budget_usd,
            provider=run.provider,
            cache_ttl=run.cache_ttl,
            breaker_category_limits={"reasoning_extraction": self.breaker_reasoning_extraction},
            breaker_any_limit=self.breaker_any,
        )

    def run_config(self, *, dry_run: bool) -> RunConfig:
        return RunConfig(
            run_name=self.run_name(dry_run=dry_run),
            provider=self.provider,
            model=self.model,
            budget_usd=self.budget_usd,
            dry_run=dry_run,
            concurrency=2 * self.concurrency,  # each trajectory has at most two calls in flight
            cache_ttl=self.cache_ttl,
            seed=self.seed,
        )


def load_arm(path: Path) -> ArmConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return ArmConfig.model_validate(data)
