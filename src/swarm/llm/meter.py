"""Cost accounting: one JSON line per API call, running totals, and a hard USD cap.

Every call, live or scripted, goes through `CostMeter.record`, which appends one line to
`runs/<run>/usage.jsonl`:

    {"ts": "...", "provider": "anthropic", "model": "claude-haiku-5-5", "phase": "task",
     "agent": "A", "episode": 3, "cache_ttl": "5m", "input_tokens": 812, "output_tokens": 64,
     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2048, "usd": ...,
     "total_usd": ...}

The cap: when the run's total goes over `budget_usd`, `record` raises `BudgetExceeded` (after
writing the line, because that money is already spent), and `ensure_within_budget`, which the
clients call before every request, refuses to start new calls. Overshoot is therefore bounded
by the calls already in flight (at most `RunConfig.concurrency`).

A meter opened on a run folder that already has a usage.jsonl continues from its totals, so a
restarted run keeps its budget. One run uses one provider, so one meter holds one provider's cap.
"""

import asyncio
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import orjson

from swarm.config import CacheTTL, Provider, RunConfig
from swarm.llm.pricing import cost_usd
from swarm.llm.types import Usage

USAGE_FILE = "usage.jsonl"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class BudgetExceeded(RuntimeError):
    """The run spent more than its budget_usd. Stops the run."""


def _new_bucket() -> dict[str, float]:
    return {"calls": 0, "usd": 0.0, **dict.fromkeys(TOKEN_FIELDS, 0)}


class _Totals:
    """Running sums over usage lines: overall, per provider, per model and per phase."""

    def __init__(self) -> None:
        self.overall = _new_bucket()
        self.by_provider: defaultdict[str, dict[str, float]] = defaultdict(_new_bucket)
        self.by_model: defaultdict[str, dict[str, float]] = defaultdict(_new_bucket)
        self.by_phase: defaultdict[str, dict[str, float]] = defaultdict(_new_bucket)

    def add(self, line: dict[str, Any]) -> None:
        for bucket in (
            self.overall,
            self.by_provider[line["provider"]],
            self.by_model[line["model"]],
            self.by_phase[line["phase"]],
        ):
            bucket["calls"] += 1
            bucket["usd"] += line["usd"]
            for field in TOKEN_FIELDS:
                bucket[field] += line[field]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_usd": self.overall["usd"],
            "calls": self.overall["calls"],
            "overall": dict(self.overall),
            "by_provider": {name: dict(b) for name, b in sorted(self.by_provider.items())},
            "by_model": {name: dict(b) for name, b in sorted(self.by_model.items())},
            "by_phase": {name: dict(b) for name, b in sorted(self.by_phase.items())},
        }


def read_usage_lines(path: Path) -> Iterator[dict[str, Any]]:
    """The lines of a usage.jsonl file, parsed. A damaged line raises with its line number."""
    with Path(path).open("rb") as f:
        for number, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                yield orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: unreadable usage line") from exc


def summarize_usage_file(path: Path) -> dict[str, Any]:
    """Totals for one usage.jsonl file (what `swarm cost-report` prints)."""
    totals = _Totals()
    for line in read_usage_lines(path):
        totals.add(line)
    return {"path": str(path), **totals.as_dict()}


class CostMeter:
    """Records every call's cost for one run and enforces the run's USD cap."""

    def __init__(
        self, run_dir: Path, budget_usd: float, *, provider: Provider, cache_ttl: CacheTTL
    ) -> None:
        if budget_usd <= 0:
            raise ValueError("budget_usd must be > 0")
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / USAGE_FILE
        self.budget_usd = budget_usd
        self.provider = provider
        self.cache_ttl = cache_ttl
        self._lock = asyncio.Lock()
        self._totals = _Totals()
        if self.path.exists():
            for line in read_usage_lines(self.path):
                if line["provider"] != provider:
                    raise ValueError(
                        f"{self.path} already holds {line['provider']} usage; "
                        f"a run uses exactly one provider"
                    )
                self._totals.add(line)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, run: RunConfig) -> Self:
        return cls(run.run_dir, run.budget_usd, provider=run.provider, cache_ttl=run.cache_ttl)

    @property
    def total_usd(self) -> float:
        return self._totals.overall["usd"]

    def ensure_within_budget(self) -> None:
        """Raise BudgetExceeded if the run is already over its cap (checked before each call)."""
        if self.total_usd > self.budget_usd:
            raise BudgetExceeded(
                f"run total ${self.total_usd:.4f} is over its ${self.budget_usd:.2f} budget; "
                f"no new calls"
            )

    async def record(
        self,
        model: str,
        usage: Usage,
        *,
        phase: str,
        agent: str,
        episode: int,
        batch: bool = False,
    ) -> float:
        """Log one call's cost and return it. Raises BudgetExceeded once the total is over.
        `batch`: a Message Batches result, priced at the batch discount and marked in the line."""
        async with self._lock:
            usd = cost_usd(
                model, usage, provider=self.provider, cache_ttl=self.cache_ttl, batch=batch
            )
            line = {
                "ts": datetime.now(UTC).isoformat(),
                "provider": self.provider,
                "model": model,
                "phase": phase,
                "agent": agent,
                "episode": episode,
                "cache_ttl": self.cache_ttl,
                **usage.model_dump(),
                **({"batch": True} if batch else {}),
                "usd": usd,
                "total_usd": self.total_usd + usd,
            }
            self._append(line)
            self._totals.add(line)
            if self.total_usd > self.budget_usd:
                raise BudgetExceeded(
                    f"run total ${self.total_usd:.4f} went over its ${self.budget_usd:.2f} budget "
                    f"(last call: {model}, phase={phase}, agent={agent}, episode={episode})"
                )
            return usd

    def _append(self, line: dict[str, Any]) -> None:
        # A small blocking append, closed straight away so a crash loses at most this line.
        with self.path.open("ab") as f:
            f.write(orjson.dumps(line) + b"\n")

    def summary(self) -> dict[str, Any]:
        return {"path": str(self.path), "budget_usd": self.budget_usd, **self._totals.as_dict()}
