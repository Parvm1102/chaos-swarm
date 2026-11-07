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

The refusal circuit breaker (the user's rule after the step-5 Sonnet suspension): every call's
stop reason reaches `record`. The whole run aborts, raising `RefusalBreakerTripped`, at the 5th
refusal of category reasoning_extraction or the 10th refusal of any category. The trip is logged
at ERROR, appended to runs/<run>/breaker.jsonl, and every later call of the run is refused
(`ensure_within_budget`). Refusal counts live in memory only, so a resumed run starts counting
from zero ("resume works as usual afterwards"); breaker.jsonl keeps the history. Every client of
a run shares the run's one meter, so the breaker covers arms, judges, calibration and episodes.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import orjson

from swarm.config import CacheTTL, Provider, RunConfig
from swarm.llm.pricing import cost_usd
from swarm.llm.types import Usage

log = logging.getLogger(__name__)

USAGE_FILE = "usage.jsonl"
BREAKER_FILE = "breaker.jsonl"
BREAKER_CATEGORY_LIMITS = {"reasoning_extraction": 5}  # trips at this many of the category
BREAKER_ANY_LIMIT = 10  # ...or at this many refusals of any category
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class BudgetExceeded(RuntimeError):
    """The run spent more than its budget_usd. Stops the run."""


class RefusalBreakerTripped(RuntimeError):
    """Too many refusals in one run: the whole run stops (module docstring)."""

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


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
        self,
        run_dir: Path,
        budget_usd: float,
        *,
        provider: Provider,
        cache_ttl: CacheTTL,
        breaker_category_limits: dict[str, int] | None = None,
        breaker_any_limit: int = BREAKER_ANY_LIMIT,
    ) -> None:
        """The breaker limits default to the project rule (5 reasoning_extraction, 10 any); a
        run may only make them stricter (the Sonnet 5 pilot stops at the first refusal)."""
        if budget_usd <= 0:
            raise ValueError("budget_usd must be > 0")
        limits = {**BREAKER_CATEGORY_LIMITS, **(breaker_category_limits or {})}
        if any(limits[k] > v for k, v in BREAKER_CATEGORY_LIMITS.items()) or not (
            1 <= breaker_any_limit <= BREAKER_ANY_LIMIT
        ):
            raise ValueError("breaker limits may only be lowered, never raised")
        self.breaker_category_limits = limits
        self.breaker_any_limit = breaker_any_limit
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / USAGE_FILE
        self.budget_usd = budget_usd
        self.provider = provider
        self.cache_ttl = cache_ttl
        self._lock = asyncio.Lock()
        self._totals = _Totals()
        self.refusals: Counter[str] = Counter()  # this invocation only (never reloaded)
        self.tripped: dict[str, Any] | None = None
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
        """Raise RefusalBreakerTripped if the breaker has tripped, or BudgetExceeded if the run is
        already over its cap (checked before each call)."""
        if self.tripped is not None:
            raise RefusalBreakerTripped(
                f"refusal breaker tripped ({self.tripped['reason']}); no new calls", self.tripped
            )
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
        stop_reason: str | None = None,
        refusal_category: str | None = None,
    ) -> float:
        """Log one call's cost and return it. Raises BudgetExceeded once the total is over, and
        RefusalBreakerTripped when this call's refusal trips the breaker.
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
                **({"refusal": refusal_category or "unknown"} if stop_reason == "refusal" else {}),
                "usd": usd,
                "total_usd": self.total_usd + usd,
            }
            self._append(line)
            self._totals.add(line)
            if stop_reason == "refusal":
                self._count_refusal(refusal_category or "unknown", line)
            if self.total_usd > self.budget_usd:
                raise BudgetExceeded(
                    f"run total ${self.total_usd:.4f} went over its ${self.budget_usd:.2f} budget "
                    f"(last call: {model}, phase={phase}, agent={agent}, episode={episode})"
                )
            return usd

    def _count_refusal(self, category: str, line: dict[str, Any]) -> None:
        """Count one refusal; trip the breaker at a limit (module docstring)."""
        self.refusals[category] += 1
        total = sum(self.refusals.values())
        reason = next(
            (
                f"{count} {name} refusals"
                for name, limit in self.breaker_category_limits.items()
                if (count := self.refusals[name]) >= limit
            ),
            f"{total} refusals" if total >= self.breaker_any_limit else None,
        )
        if reason is None:
            return
        self.tripped = {
            "ts": line["ts"],
            "reason": reason,
            "refusals": dict(self.refusals),
            "last_call": {key: line[key] for key in ("model", "phase", "agent", "episode")},
        }
        with (self.run_dir / BREAKER_FILE).open("ab") as f:
            f.write(orjson.dumps(self.tripped) + b"\n")
        log.error(
            "REFUSAL BREAKER TRIPPED in %s: %s %s; the whole run stops (resume works afterwards)",
            self.run_dir,
            reason,
            dict(self.refusals),
        )
        raise RefusalBreakerTripped(f"refusal breaker tripped: {reason}", self.tripped)

    def _append(self, line: dict[str, Any]) -> None:
        # A small blocking append, closed straight away so a crash loses at most this line.
        with self.path.open("ab") as f:
            f.write(orjson.dumps(line) + b"\n")

    def summary(self) -> dict[str, Any]:
        return {"path": str(self.path), "budget_usd": self.budget_usd, **self._totals.as_dict()}


def breaker_history(run_dir: Path) -> list[dict[str, Any]]:
    """Every breaker trip recorded for a run folder, oldest first."""
    path = Path(run_dir) / BREAKER_FILE
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
