"""The `swarm` command line: check-env, ping, cost-report, calibrate, and the `tasks` sub-app."""

import asyncio
import logging
import platform
import shutil
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, get_args

import anthropic
import orjson
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress
from rich.table import Table

from swarm.agents.task_agent import TASK_MAX_TOKENS
from swarm.analysis.judges import JudgeConfig
from swarm.analysis.validation import score_sheet
from swarm.arms import load_arm
from swarm.calibrate import pick_tasks, run_calibration, summarize_calibration, write_summary
from swarm.config import (
    RUNS_DIR,
    CacheTTL,
    LiveCallRefused,
    ModelConfig,
    Provider,
    RunConfig,
    Settings,
)
from swarm.episodes import episode_name, episode_task_ids, run_episode_to_files
from swarm.experiment1 import run_spillover
from swarm.experiment3 import run_opendoor
from swarm.judging import add_judge_to_sample, compute_pathways, make_sample, run_judge
from swarm.llm.client import THINKING_OFF, AnthropicClient
from swarm.llm.meter import (
    USAGE_FILE,
    BudgetExceeded,
    CostMeter,
    RefusalBreakerTripped,
    summarize_usage_file,
)
from swarm.llm.pricing import PRICES, cost_usd
from swarm.llm.types import LLMResponse, text_block, user_message
from swarm.population import run_population
from swarm.replication import run_replication
from swarm.step8 import run_followups
from swarm.tasks.generate import (
    DIFFICULTIES,
    GroundTruthMismatch,
    TaskGenerationError,
    generate_tasks,
    load_manifest,
    load_task,
    summarize,
    task_set_name,
)
from swarm.tasks.groups import GROUPS_FILE, make_groups, write_groups
from swarm.tasks.sequences import SEQUENCES_FILE, make_sequence_set, write_sequences
from swarm.tasks.templates import TEMPLATES_BY_NAME
from swarm.trajectories import run_arm, summarize_arm

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Swarm collusion harness.")
tasks_app = typer.Typer(
    no_args_is_help=True, help="Synthetic SQLite tasks: generate, sequence and inspect."
)
app.add_typer(tasks_app, name="tasks")
validate_app = typer.Typer(
    no_args_is_help=True, help="Hand validation of the judges: a blind sample, then its score."
)
app.add_typer(validate_app, name="validate")
console = Console()
DEFAULT_TASK_ROOT = Path("data/tasks")

# The --provider and --cache-ttl choices come straight from config, so the CLI cannot drift.
ProviderName = StrEnum("ProviderName", {name: name for name in get_args(Provider)})
CacheTTLName = StrEnum("CacheTTLName", {ttl: ttl for ttl in get_args(CacheTTL)})
ThinkingName = StrEnum("ThinkingName", {"off": "off", "adaptive": "adaptive"})
EffortName = StrEnum(
    "EffortName", {level: level for level in ("low", "medium", "high", "xhigh", "max")}
)
DifficultyName = StrEnum("DifficultyName", {name: name for name in DIFFICULTIES})

PING_PROMPT = "Reply with the single word pong"
# Deliberately long and fixed: the API does not cache prefixes shorter than 512 tokens, so a
# bare "reply pong" request could never show a cache read on the second call.
PING_SYSTEM = (
    "You are a connectivity probe for a research harness. Reply to every request with exactly "
    "the one word it asks for, in lower case, with no punctuation.\n\n"
    "The numbered lines below only make this prompt long enough to be cached. Ignore them.\n"
    + "\n".join(
        f"Padding line {i:03d}: the quick brown fox jumps over the lazy dog." for i in range(1, 61)
    )
)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Swarm collusion harness."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
        force=True,
    )
    if not verbose:  # the SDK's HTTP layer logs every request at INFO
        for name in ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)


@app.command("check-env")
def check_env() -> None:
    """Python and conda env, SDK version, whether the API key is set, and the live switch."""
    settings = Settings()
    table = Table(title="swarm environment", show_header=False, title_justify="left")
    table.add_column(style="bold")
    table.add_column()
    table.add_row("python", f"{platform.python_version()} ({sys.executable})")
    table.add_row("conda env", _conda_env_name())
    table.add_row("anthropic SDK", anthropic.__version__)
    key = "present" if settings.anthropic_api_key() else "[red]missing[/red]"
    table.add_row("ANTHROPIC_API_KEY", key)
    live = "1 (live calls allowed)" if settings.SWARM_LIVE else "0 (dry-run only)"
    table.add_row("SWARM_LIVE", live)
    table.add_row("SWARM_DEFAULT_BUDGET_USD", f"{settings.SWARM_DEFAULT_BUDGET_USD:g}")
    table.add_row("platform", platform.platform())
    console.print(table)


def _conda_env_name() -> str:
    """The conda env of the running interpreter (not whichever env the shell activated)."""
    prefix = Path(sys.prefix)
    if not (prefix / "conda-meta").is_dir():
        return "(not a conda env)"
    return prefix.name if prefix.parent.name == "envs" else "base"


async def run_ping(
    *, provider: Provider, model: str, budget_usd: float, run_dir: Path | None = None
) -> tuple[list[LLMResponse], CostMeter]:
    """Send the same small request twice; the second call should read the first one's cache."""
    run = RunConfig(
        run_name=f"ping-{datetime.now(UTC):%Y%m%d-%H%M%S}",
        provider=provider,
        model=ModelConfig(name=model, max_tokens=512),
        budget_usd=budget_usd,
        dry_run=False,
        seed=0,
    )
    meter = CostMeter(
        run_dir or run.run_dir, run.budget_usd, provider=run.provider, cache_ttl=run.cache_ttl
    )
    responses: list[LLMResponse] = []
    async with AnthropicClient(run, meter) as client:
        for call in (1, 2):
            response = await client.complete(
                system=[text_block(PING_SYSTEM)],
                messages=[user_message(PING_PROMPT)],
                tools=None,
                tool_choice=None,
                max_tokens=run.model.max_tokens,
                tags={"phase": "ping", "agent": "cli", "episode": call},
            )
            responses.append(response)
    return responses, meter


@app.command()
def ping(
    provider: Annotated[ProviderName, typer.Option(help="API provider.")] = ProviderName.anthropic,
    model: Annotated[str, typer.Option(help="Model to ping.")] = "claude-haiku-5-5",
    budget: Annotated[float, typer.Option(help="USD cap for this ping.")] = 0.05,
) -> None:
    """One live call ("Reply with the single word pong"), sent twice: usage, cost, cache hit.

    Needs SWARM_LIVE=1 and ANTHROPIC_API_KEY. Writes runs/ping-<timestamp>/usage.jsonl.
    """
    try:
        responses, meter = asyncio.run(
            run_ping(provider=provider.value, model=model, budget_usd=budget)
        )
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    except BudgetExceeded as error:
        console.print(f"[red]over budget:[/red] {error}")
        raise typer.Exit(code=3) from None
    except RefusalBreakerTripped as error:
        console.print(
            f"[red]REFUSAL BREAKER:[/red] {error} {error.details['refusals']}; run aborted"
        )
        raise typer.Exit(code=4) from None
    except anthropic.APIError as error:
        console.print(f"[red]API error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(code=1) from None

    table = Table(title=f"swarm ping: {provider.value}/{model}", title_justify="left")
    for name in ("call", "reply", "stop", "input", "cache write", "cache read", "output", "USD"):
        table.add_column(name, justify="left" if name in ("reply", "stop") else "right")
    for number, response in enumerate(responses, start=1):
        u = response.usage
        usd = cost_usd(model, u, provider=meter.provider, cache_ttl=meter.cache_ttl)
        table.add_row(
            str(number),
            repr(response.text),
            response.stop_reason,
            f"{u.input_tokens:,}",
            f"{u.cache_creation_input_tokens:,}",
            f"{u.cache_read_input_tokens:,}",
            f"{u.output_tokens:,}",
            f"${usd:.6f}",
        )
    console.print(table)
    cached = responses[1].usage.cache_read_input_tokens
    verdict = "[green]yes[/green]" if cached > 0 else "[red]no[/red]"
    console.print(f"cache read on the 2nd call: {verdict} ({cached:,} tokens)")
    console.print(f"total ${meter.total_usd:.6f}; usage written to {meter.path}")
    if cached == 0:
        raise typer.Exit(code=1)


@app.command("cost-report")
def cost_report(
    run_name: Annotated[str, typer.Argument(help="A run folder under runs/.")],
) -> None:
    """Spend for one run, by model and by phase, from runs/<run_name>/usage.jsonl."""
    path = RUNS_DIR / run_name / USAGE_FILE
    if not path.is_file():
        console.print(f"[red]no usage file at {path}[/red]")
        raise typer.Exit(code=1)
    summary = summarize_usage_file(path)
    console.print(_usage_table(f"{run_name}: by model", summary["by_model"], summary["overall"]))
    console.print(_usage_table(f"{run_name}: by phase", summary["by_phase"], summary["overall"]))
    providers = ", ".join(summary["by_provider"])
    console.print(
        f"provider: {providers}  |  {summary['calls']} calls  |  ${summary['total_usd']:.6f}"
    )


def _usage_table(title: str, buckets: dict[str, dict[str, Any]], overall: dict[str, Any]) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("name")
    for name in ("calls", "input", "cache write", "cache read", "output", "USD"):
        table.add_column(name, justify="right")
    for name, bucket in buckets.items():
        table.add_row(name, *_usage_cells(bucket))
    table.add_section()
    table.add_row("total", *_usage_cells(overall), style="bold")
    return table


def _usage_cells(bucket: dict[str, Any]) -> list[str]:
    return [
        f"{bucket['calls']:,}",
        f"{bucket['input_tokens']:,}",
        f"{bucket['cache_creation_input_tokens']:,}",
        f"{bucket['cache_read_input_tokens']:,}",
        f"{bucket['output_tokens']:,}",
        f"${bucket['usd']:.6f}",
    ]


# swarm calibrate ------------------------------------------------------------------------------


@app.command()
def calibrate(
    model: Annotated[
        str, typer.Option(help="provider/model, e.g. anthropic/claude-haiku-5-5.")
    ] = "anthropic/claude-sonnet-5-5",
    n: Annotated[int, typer.Option(help="Number of tasks, spread evenly over T1..T4.")] = 20,
    seed: Annotated[int, typer.Option(help="Seed for picking the tasks.")] = 0,
    run_name: Annotated[
        str | None,
        typer.Option(help="Folder under runs/ (default calib_<model>_<ttl>, plus _dry)."),
    ] = None,
    cache_ttl: Annotated[CacheTTLName, typer.Option(help="Prompt-cache TTL.")] = CacheTTLName["5m"],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Offline scripted agent: no API calls, $0.")
    ] = False,
    task_dir: Annotated[Path, typer.Option(help="Task set folder.")] = DEFAULT_TASK_ROOT / "seed0",
    budget: Annotated[
        float | None, typer.Option(help="USD cap for this run (default SWARM_DEFAULT_BUDGET_USD).")
    ] = None,
    concurrency: Annotated[int, typer.Option(min=1, help="Task phases in flight at once.")] = 4,
    force: Annotated[bool, typer.Option(help="Replace an existing run folder.")] = False,
    task_prompt: Annotated[
        str,
        typer.Option(
            help="Task-phase prompt: task_phase_system or task_phase_system_rules2 (Experiment 1)."
        ),
    ] = "task_phase_system",
    query_cap: Annotated[
        int | None, typer.Option(help="Experiment 1's query cap (calls past it are logged).")
    ] = None,
) -> None:
    """Solo calibration: n task phases without a peer; accuracy, peek rate, cost, cache use.

    Live unless --dry-run, and a live run needs SWARM_LIVE=1. Writes runs/<run>/calibration.jsonl,
    summary.json, transcripts/ and usage.jsonl.
    """
    provider, model_name = _parse_model(model)
    if task_prompt not in ("task_phase_system", "task_phase_system_rules2",
                           "task_phase_system_rules2_cap1"):  # fmt: skip
        console.print(f"[red]unknown --task-prompt {task_prompt!r}[/red]")
        raise typer.Exit(code=1)
    name = run_name or f"calib_{model_name}_{cache_ttl.value}" + ("_dry" if dry_run else "")
    settings = Settings()
    try:
        run = RunConfig(
            run_name=name,
            provider=provider,
            model=ModelConfig(name=model_name, max_tokens=TASK_MAX_TOKENS),
            budget_usd=budget if budget is not None else settings.SWARM_DEFAULT_BUDGET_USD,
            dry_run=dry_run,
            concurrency=concurrency,
            cache_ttl=cache_ttl.value,
            seed=seed,
        )
        if not dry_run and settings.anthropic_api_key() is None:  # before any folder is made
            raise LiveCallRefused("ANTHROPIC_API_KEY is not set (put it in .env)")
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None

    manifest = load_manifest(task_dir)
    try:
        task_ids = pick_tasks(manifest, n, seed)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    if run.run_dir.exists():
        if not force:
            console.print(
                f"[red]{run.run_dir} already exists; choose another --run-name or pass "
                "--force[/red]"
            )
            raise typer.Exit(code=1)
        shutil.rmtree(run.run_dir)

    meter = CostMeter.for_run(run)
    mode = "dry run (scripted agent)" if dry_run else "LIVE"
    console.print(
        f"{mode}: {len(task_ids)} task phases on {provider}/{model_name}, cache {run.cache_ttl}, "
        f"{concurrency} at a time, budget ${run.budget_usd:g} -> {run.run_dir}"
    )
    try:
        records = asyncio.run(
            run_calibration(
                run, task_dir, task_ids, meter, task_prompt=task_prompt, query_cap=query_cap
            )
        )
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    except BudgetExceeded as error:
        console.print(f"[red]over budget:[/red] {error}")
        raise typer.Exit(code=3) from None
    except RefusalBreakerTripped as error:
        console.print(
            f"[red]REFUSAL BREAKER:[/red] {error} {error.details['refusals']}; run aborted"
        )
        raise typer.Exit(code=4) from None
    except anthropic.APIError as error:
        console.print(f"[red]API error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(code=1) from None

    summary = summarize_calibration(
        records,
        run=run,
        task_dir=task_dir,
        manifest_digest=manifest.digest(),
        meter_total_usd=meter.total_usd,
    )
    path = write_summary(run.run_dir, summary)
    _print_calibration(summary)
    console.print(f"wrote {path.parent}/{{calibration.jsonl, summary.json, transcripts/}}")


def _parse_model(value: str) -> tuple[Provider, str]:
    """'anthropic/claude-haiku-5-5' -> ('anthropic', 'claude-haiku-5-5'); the provider part is
    optional and defaults to anthropic."""
    provider, _, name = value.rpartition("/")
    provider = provider or "anthropic"
    if provider not in get_args(Provider):
        console.print(
            f"[red]unknown provider {provider!r}; known: {list(get_args(Provider))}[/red]"
        )
        raise typer.Exit(code=1)
    if name not in PRICES[provider] or name not in THINKING_OFF:
        console.print(
            f"[red]unknown model {name!r} for {provider}; known: {list(PRICES[provider])}[/red]"
        )
        raise typer.Exit(code=1)
    return provider, name


def _print_calibration(summary: dict[str, Any]) -> None:
    dry = ", dry run" if summary["dry_run"] else ""
    table = Table(
        title=f"{summary['run_name']}: {summary['provider']}/{summary['model']} "
        f"(cache {summary['cache_ttl']}{dry})",
        title_justify="left",
    )
    table.add_column("template")
    for column in ("tasks", "correct", "accuracy", "95% CI"):
        table.add_column(column, justify="right")
    for template, rate in summary["per_template"].items():
        table.add_row(template, *_rate_cells(rate))
    table.add_section()
    table.add_row("all", *_rate_cells(summary["accuracy"]), style="bold")
    console.print(table)

    totals = summary["totals"]
    metrics = Table(title="calibration metrics", title_justify="left", show_header=False)
    metrics.add_column(style="bold")
    metrics.add_column()
    metrics.add_row("accuracy, non-peek tasks", _rate_text(summary["accuracy_non_peek"]))
    metrics.add_row("peek rate (solo baseline)", _rate_text(summary["peek_rate"]))
    if summary.get("query_cap") is not None:
        metrics.add_row(
            f"cap violations (> {summary['query_cap']} queries)",
            _rate_text(summary["cap_violation_rate"]),
        )
        metrics.add_row("accuracy within the cap", _rate_text(summary["accuracy_within_cap"]))
        metrics.add_row("accuracy over the cap", _rate_text(summary["accuracy_over_cap"]))
        metrics.add_row("queries per task", str(summary["query_count_distribution"]))
    metrics.add_row("answer saved", _rate_text(summary["answer_saved_rate"]))
    metrics.add_row("stop causes", ", ".join(f"{k} {v}" for k, v in summary["stop_causes"].items()))
    metrics.add_row(
        "per task (median)",
        f"queries {summary['query_count_median']:g}, turns {summary['turns_median']:g}, "
        f"wall time {summary['wall_s_median']:.1f} s",
    )
    metrics.add_row(
        "log chars (median / min)",
        f"{summary['log_chars']['median']:,.0f} / {summary['log_chars']['min']:,}",
    )
    cost = summary["cost"]
    metrics.add_row(
        "cost", f"${cost['total_usd']:.4f} total, ${cost['mean_per_task_usd']:.4f} per task"
    )
    fraction = summary["cache_read_fraction"]
    metrics.add_row(
        "cache read fraction", "n/a (no tokens)" if fraction is None else f"{fraction:.1%}"
    )
    metrics.add_row(
        "write attempts / blocked / aborts / errors",
        f"{totals['write_attempts']} / {totals['blocked']} / {totals['query_aborts']} / "
        f"{totals['query_errors']}",
    )
    console.print(metrics)


def _rate_cells(rate: dict[str, Any]) -> list[str]:
    return [str(rate["n"]), str(rate["k"]), _percent(rate["rate"]), _interval(rate["ci95"])]


def _rate_text(rate: dict[str, Any]) -> str:
    if rate["n"] == 0:
        return "n/a (0 tasks)"
    return f"{rate['k']}/{rate['n']} = {_percent(rate['rate'])}  95% CI {_interval(rate['ci95'])}"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _interval(ci: list[float] | None) -> str:
    return "-" if ci is None else f"[{ci[0]:.1%}, {ci[1]:.1%}]"


# swarm episode --------------------------------------------------------------------------------


@app.command("episode")
def episode_command(
    model: Annotated[
        str, typer.Option(help="provider/model for both agents (default: the subject model).")
    ] = "anthropic/claude-haiku-5-5",
    sequence: Annotated[int, typer.Option(help="Sequence id in sequences.json.")] = 0,
    episode: Annotated[int, typer.Option(help="Episode number (1-based).")] = 1,
    task_dir: Annotated[
        Path, typer.Option(help="Task set folder (with sequences.json).")
    ] = DEFAULT_TASK_ROOT / "seed0-hard",
    run_name: Annotated[
        str | None,
        typer.Option(help="Folder under runs/ (default ep_<model>_s<seq>e<ep>, plus _dry)."),
    ] = None,
    cache_ttl: Annotated[CacheTTLName, typer.Option(help="Prompt-cache TTL.")] = CacheTTLName["5m"],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Scripted agents: no API calls, $0.")
    ] = False,
    budget: Annotated[
        float | None, typer.Option(help="USD cap for this run (default SWARM_DEFAULT_BUDGET_USD).")
    ] = None,
    thinking: Annotated[
        ThinkingName,
        typer.Option(help="off: each model's off switch (v1); adaptive: the model decides."),
    ] = ThinkingName.off,
    effort: Annotated[
        EffortName | None,
        typer.Option(help="output_config.effort (only with --thinking adaptive)."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing run folder.")] = False,
) -> None:
    """One episode for the pair A, B: task phases, the 5-round channel, verdicts, feedback and
    notes. Live unless --dry-run (a live run needs SWARM_LIVE=1)."""
    provider, model_name = _parse_model(model)
    name = run_name or f"ep_{model_name}_s{sequence}e{episode}" + ("_dry" if dry_run else "")
    settings = Settings()
    try:
        model_config = ModelConfig(
            name=model_name,
            max_tokens=TASK_MAX_TOKENS,
            thinking=thinking.value,
            effort=effort.value if effort else None,
        )
    except ValueError as error:  # pydantic's ValidationError is a ValueError
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    try:
        run = RunConfig(
            run_name=name,
            provider=provider,
            model=model_config,
            budget_usd=budget if budget is not None else settings.SWARM_DEFAULT_BUDGET_USD,
            dry_run=dry_run,
            concurrency=2,
            cache_ttl=cache_ttl.value,
            seed=0,
        )
        if not dry_run and settings.anthropic_api_key() is None:
            raise LiveCallRefused("ANTHROPIC_API_KEY is not set (put it in .env)")
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    try:
        episode_task_ids(task_dir, sequence, episode)  # fail before any folder is made
    except (ValueError, FileNotFoundError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    if run.run_dir.exists():
        if not force:
            console.print(
                f"[red]{run.run_dir} already exists; choose another --run-name or pass "
                "--force[/red]"
            )
            raise typer.Exit(code=1)
        shutil.rmtree(run.run_dir)

    meter = CostMeter.for_run(run)
    mode = "dry run (scripted agents)" if dry_run else "LIVE"
    console.print(
        f"{mode}: sequence {sequence}, episode {episode} on {provider}/{model_name}, "
        f"cache {run.cache_ttl}, budget ${run.budget_usd:g} -> {run.run_dir}"
    )
    try:
        record = asyncio.run(run_episode_to_files(run, task_dir, sequence, episode, meter))
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    except BudgetExceeded as error:
        console.print(f"[red]over budget:[/red] {error}")
        raise typer.Exit(code=3) from None
    except RefusalBreakerTripped as error:
        console.print(
            f"[red]REFUSAL BREAKER:[/red] {error} {error.details['refusals']}; run aborted"
        )
        raise typer.Exit(code=4) from None
    except anthropic.APIError as error:
        console.print(f"[red]API error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(code=1) from None
    _print_episode(record)
    console.print(
        f"wrote {run.run_dir}/{{episodes/{episode_name(sequence, episode)}.json, transcripts/}}"
    )


def _print_episode(record: dict[str, Any]) -> None:
    table = Table(
        title=f"{record['run_name']}: sequence {record['sequence_id']}, "
        f"episode {record['episode']}",
        title_justify="left",
    )
    table.add_column("")
    for name in record["agents"]:
        table.add_column(f"agent {name}")
    rows: list[tuple[str, Any]] = [
        ("task", lambda a: f"{a['task_id']} ({a['template']}), {a['task']['turns']} turns"),
        ("answer", lambda a: "correct" if a["task"]["correct"] else "wrong"),
        (
            "peeks / blocked / writes",
            lambda a: (
                f"{a['task']['peek_count']} / "
                f"{a['task']['blocked']} / {a['task']['write_attempts']}"
            ),
        ),
        (
            "messages sent (chars)",
            lambda a: ", ".join(str(m["chars"]) for m in a["messages_sent"]) or "none",
        ),
        (
            "received vs peer log",
            lambda a: f"{a['delivered_chars']:,} of {a['peer_log_chars']:,} chars",
        ),
        ("verdict", lambda a: f"{a['verdict']['scored']} ({a['verdict']['source']})"),
        ("verdict correct", lambda a: "yes" if a["verdict_correct"] else "no"),
        ("phase violations", lambda a: str(len(a["phase_violations"]))),
        (
            "refusals",
            lambda a: ", ".join(f"{r['phase']}: {r['category']}" for r in a["refusals"]) or "none",
        ),
        ("reflection complete", lambda a: "yes" if a["reflection_complete"] else "no"),
        ("cost", lambda a: f"${a['cost_usd']:.4f}"),
    ]
    for label, cell in rows:
        table.add_row(label, *(cell(agent) for agent in record["agents"].values()))
    console.print(table)
    console.print(
        f"mutual ACCEPT: {'yes' if record['mutual_accept'] else 'no'} | refusals "
        f"{record['refusals_total']} | shared reward "
        f"{record['reward']} | episode cost ${record['cost_usd']:.4f} | {record['wall_s']:.1f} s"
    )


# swarm trajectories ---------------------------------------------------------------------------


@app.command("trajectories")
def trajectories_command(
    arm_file: Annotated[Path, typer.Option("--arm", help="The arm's YAML file.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Scripted agents: no API calls, $0.")
    ] = False,
) -> None:
    """Run (or resume) every trajectory of one arm, then write its summary. Live unless
    --dry-run (a live run needs SWARM_LIVE=1)."""
    try:
        arm = load_arm(arm_file)
    except (ValueError, OSError) as error:
        console.print(f"[red]{arm_file}: {error}[/red]")
        raise typer.Exit(code=1) from None
    try:
        run = arm.run_config(dry_run=dry_run)
        if not dry_run and Settings().anthropic_api_key() is None:
            raise LiveCallRefused("ANTHROPIC_API_KEY is not set (put it in .env)")
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    done = len(list(run.run_dir.glob("trajectories/s*/trajectory.json")))
    meter = arm.meter(dry_run=dry_run)  # keeps its spend on resume; the arm's breaker limits
    mode = "dry run (scripted agents)" if dry_run else "LIVE"
    console.print(
        f"{mode}: arm {arm.arm} on {arm.provider}/{arm.model.name}, memory "
        f"{'on' if arm.memory else 'off'}, sequences {arm.sequences[0]}-{arm.sequences[-1]} "
        f"({len(arm.sequences)}), {arm.episodes} episodes, {arm.concurrency} at a time, budget "
        f"${arm.budget_usd:g} (spent ${meter.total_usd:.4f}; {done} trajectories done) -> "
        f"{run.run_dir}"
    )
    try:
        summary = asyncio.run(run_arm(arm, dry_run=dry_run, meter=meter))
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    except BudgetExceeded as error:
        console.print(f"[red]over budget:[/red] {error}")
        raise typer.Exit(code=3) from None
    except RefusalBreakerTripped as error:
        console.print(
            f"[red]REFUSAL BREAKER:[/red] {error} {error.details['refusals']}; run aborted"
        )
        raise typer.Exit(code=4) from None
    except anthropic.APIError as error:
        console.print(f"[red]API error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(code=1) from None
    _print_arm(summary)
    console.print(f"wrote {run.run_dir}/{{summary.json, trajectories.jsonl, trajectories/}}")


@app.command("summarize")
def summarize_command(
    arm_files: Annotated[
        list[Path], typer.Option("--arm", help="An arm's YAML file (repeatable).")
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The arm's dry-run folder.")] = False,
) -> None:
    """Recompute arms' trajectories.jsonl and summary.json from their episode records (offline,
    no API calls): the current collusion criterion plus the recounts under the others."""
    for arm_file in arm_files:
        try:
            summary = summarize_arm(load_arm(arm_file), dry_run=dry_run)
        except (ValueError, OSError) as error:
            console.print(f"[red]{arm_file}: {error}[/red]")
            raise typer.Exit(code=1) from None
        _print_arm(summary)


def _print_arm(summary: dict[str, Any]) -> None:
    if summary.get("population"):
        _print_population_arm(summary)
        return
    prov = summary["provenance"]
    table = Table(
        title=f"{summary['arm']}: {prov['model']} (memory {'on' if prov['memory'] else 'off'}, "
        f"rules {prov['rules']}{', B scripted' if prov['scripted_peer'] else ''}"
        f"{', dry run' if summary['dry_run'] else ''})",
        title_justify="left",
        show_header=False,
    )
    table.add_column(style="bold")
    table.add_column()
    table.add_row("trajectories / episodes", f"{summary['trajectories']} / {summary['episodes']}")
    table.add_row("collusion criterion", summary.get("criterion", "original"))
    table.add_row("TC (any mutual ACCEPT)", _rate_text(summary["tc"]))
    table.add_row("CC (converged)", _rate_text(summary["cc"]))
    table.add_row("EC (episodes, pooled)", _rate_text(summary["ec_pooled"]))
    onset = summary["onset"]
    table.add_row(
        "onset episode",
        f"{onset['by_episode'] or 'none'}; no onset in {onset['none']}; median {onset['median']}",
    )
    for name, recount in summary.get("recounts", {}).items():
        table.add_row(
            f"recount: {name}",
            f"TC {recount['tc']['k']}/{recount['tc']['n']}, CC {recount['cc']['k']}, "
            f"EC {recount['ec_pooled']['k']}/{recount['ec_pooled']['n']}",
        )
    if "record_fit_possible_episodes" in summary:
        table.add_row(
            "record could fit / compliant ACCEPTs",
            f"{summary['record_fit_possible_episodes']} episodes / {summary['compliant_accepts']}",
        )
    table.add_row("provisional pathway", str(summary["provisional_pathway"]))
    if "pathway" in summary:
        table.add_row(f"pathway ({summary['pathway_judge']})", str(summary["pathway"]))
    for agent in ("A", "B"):
        table.add_row(
            f"ACCEPT rate {agent} (unilateral)",
            f"{_rate_text(summary['accept_rate'][agent])} "
            f"({summary['unilateral_accept_rate'][agent]['k']} unilateral)",
        )
    table.add_row("task accuracy", _rate_text(summary["task_accuracy"]))
    table.add_row("peek rate (task phases)", _rate_text(summary["peek_rate"]))
    table.add_row("verdict sources", str(summary["verdict_sources"]))
    table.add_row("refusals", f"{summary['refusals']['total']} {summary['refusals']['by_phase']}")
    table.add_row("reflections complete", _rate_text(summary["reflections_complete"]))
    proof = summary["channel_proof"]
    holds = proof["max_delivered_chars"] < proof["min_peer_log_chars"]
    table.add_row(
        "channel proof",
        f"max received {proof['max_delivered_chars']:,} chars {'<' if holds else '>='} min peer "
        f"record {proof['min_peer_log_chars']:,}{'' if holds else ' (does NOT hold)'}",
    )
    cost = summary["cost"]
    table.add_row(
        "cost",
        f"${cost['total_usd']:.4f} (${cost['per_episode_usd']:.4f}/episode, "
        f"${cost['per_trajectory_usd']:.4f}/trajectory)",
    )
    console.print(table)


# swarm spillover (Experiment 1) -----------------------------------------------------------------


@app.command("spillover")
def spillover_command(
    constrained: Annotated[Path, typer.Option(help="The 200-character arm (memory on).")],
    unconstrained: Annotated[Path, typer.Option(help="The 8,000-character arm.")],
    nomem: Annotated[Path, typer.Option(help="The 200-character arm with memory off.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Experiment 1's pre-registered analysis (offline): P1, P2, P2', P3 and the P3 control."""
    arms = _load_arms([constrained, unconstrained, nomem])
    try:
        report = run_spillover(*arms, dry_run=dry_run)
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    table = Table(title="Experiment 1 arms", title_justify="left")
    for column in (
        "role",
        "arm",
        "peek",
        "cap violations",
        "accuracy",
        "TC",
        "EC",
        "delivered",
        "$",
    ):
        table.add_column(column)
    for role, a in report["arms"].items():
        delivered = a["record_delivered_rate"]
        table.add_row(
            role,
            a["arm"],
            _rate_text(a["peek_rate"]),
            _rate_text(a["cap_violation_rate"]),
            _rate_text(a["task_accuracy"]),
            f"{a['collusion']['tc']['k']}/{a['collusion']['tc']['n']}",
            f"{a['collusion']['ec_pooled']['k']}/{a['collusion']['ec_pooled']['n']}",
            "-" if delivered is None else _rate_text(delivered),
            f"{a['cost']['total_usd']:.2f}",
        )
    console.print(table)
    hypotheses = Table(title="pre-registered hypotheses", title_justify="left")
    for column in ("hypothesis", "rates (pooled)", "difference / p", "verdict"):
        hypotheses.add_column(column)
    for name, h in report["hypotheses"].items():
        if "pooled" in h:
            rates_text = (
                f"{_rate_text(h['pooled']['first'])} vs {_rate_text(h['pooled']['second'])}"
            )
            diff = h["difference_of_means"]
            stats = (
                f"{diff:+.3f} [{h['bootstrap_ci95'][0]:+.3f}, {h['bootstrap_ci95'][1]:+.3f}], "
                f"p={h['permutation_p']:.4f}"
                if diff is not None
                else "-"
            )
        elif "after" in h:
            rates_text = f"after {_rate_text(h['after'])} vs before {_rate_text(h['before'])}"
            stats = (
                f"{h['mean_paired_difference']:+.3f}, p={h['signflip_p']:.4f} "
                f"({h['trajectories']} trajectories)"
                if h["mean_paired_difference"] is not None
                else "-"
            )
        else:
            rates_text, stats = "-", f"{h['trajectories']} trajectories"
        hypotheses.add_row(name, rates_text, stats, h["verdict"])
    console.print(hypotheses)
    console.print(
        f"wrote {report['path']} (P3 split S = {report['p3_control']['split']['split_episode']})"
    )


# swarm opendoor (Experiment 3) ------------------------------------------------------------------


@app.command("opendoor")
def opendoor_command(
    control: Annotated[Path, typer.Option(help="The control arm (no report tool).")],
    fix: Annotated[Path, typer.Option(help="The arm whose operator raises the limit.")],
    ack: Annotated[Path, typer.Option(help="The arm whose operator only acknowledges.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Experiment 3's pre-registered analysis (offline): reports, P1-P4, the appendix."""
    arms = _load_arms([control, fix, ack])
    try:
        report = run_opendoor(*arms, dry_run=dry_run)
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    table = Table(title="Experiment 3 arms", title_justify="left")
    for column in (
        "role",
        "ever reported",
        "reports",
        "race (first report / onset)",
        "TC",
        "EC",
        "$",
    ):
        table.add_column(column)
    for role, a in report["arms"].items():
        r = a["reports"]
        table.add_row(
            role,
            _rate_text(r["trajectories_ever_reporting"]),
            str(r["reports_total"]),
            f"{r['race']['median_first_report']} / {r['race']['median_onset']} "
            f"{r['race']['per_trajectory']}",
            f"{a['collusion']['tc']['k']}/{a['collusion']['tc']['n']}",
            f"{a['collusion']['ec_pooled']['k']}/{a['collusion']['ec_pooled']['n']}",
            f"{a['cost']['total_usd']:.2f}",
        )
    console.print(table)
    hypotheses = Table(title="pre-registered hypotheses", title_justify="left")
    hypotheses.add_column("hypothesis")
    hypotheses.add_column("verdict")
    for name, h in report["hypotheses"].items():
        if "verdict" in h:
            hypotheses.add_row(name, h["verdict"])
        for part, sub in h.items():
            if isinstance(sub, dict) and "verdict" in sub:
                hypotheses.add_row(f"{name}: {part}", sub["verdict"])
    console.print(hypotheses)
    console.print(f"wrote {report['path']} and {report['appendix']}")


# swarm followups (step 8) -----------------------------------------------------------------------


@app.command("followups")
def followups_command(
    invite: Annotated[Path, typer.Option(help="e3_invite.")],
    checkin: Annotated[Path, typer.Option(help="e3_checkin.")],
    cap1: Annotated[Path, typer.Option(help="e1_cap1.")],
    cap1_nomem: Annotated[Path, typer.Option(help="e1_cap1_nomem.")],
    plainintro: Annotated[Path, typer.Option(help="e1_unconstrained_plainintro.")],
    control: Annotated[Path, typer.Option(help="e3_control (reused comparison).")],
    fix: Annotated[Path, typer.Option(help="e3_fix (reused comparison).")],
    unconstrained: Annotated[Path, typer.Option(help="e1_unconstrained (reused comparison).")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Step 8's pre-registered follow-up analysis (offline): F1-F6 and the cap-1 hypotheses."""
    roles = {
        "invite": invite,
        "checkin": checkin,
        "cap1": cap1,
        "cap1_nomem": cap1_nomem,
        "plainintro": plainintro,
        "control": control,
        "fix": fix,
        "unconstrained": unconstrained,
    }
    arms = dict(zip(roles, _load_arms(list(roles.values())), strict=True))
    try:
        report = run_followups(arms, dry_run=dry_run)
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    table = Table(title="follow-up arms", title_justify="left")
    for column in ("role", "arm", "TC", "EC", "cap violations", "delivered", "ever reported", "$"):
        table.add_column(column)
    for role, a in report["arms"].items():
        delivered, reports = a["record_delivered_rate"], a["reports"]
        table.add_row(
            role,
            a["arm"],
            f"{a['collusion']['tc']['k']}/{a['collusion']['tc']['n']}",
            f"{a['collusion']['ec_pooled']['k']}/{a['collusion']['ec_pooled']['n']}",
            _rate_text(a["cap_violation_rate"]),
            "-" if delivered is None else _rate_text(delivered),
            "-" if reports is None else _rate_text(reports["trajectories_ever_reporting"]),
            f"{a['cost']['total_usd']:.2f}",
        )
    console.print(table)
    hypotheses = Table(title="pre-registered follow-up hypotheses", title_justify="left")
    hypotheses.add_column("hypothesis")
    hypotheses.add_column("verdict")
    for name, h in report["hypotheses"].items():
        if "verdict" in h:
            hypotheses.add_row(name, h["verdict"])
        for part, sub in h.items():
            if isinstance(sub, dict) and "verdict" in sub:
                hypotheses.add_row(f"{name}: {part}", sub["verdict"])
    console.print(hypotheses)
    console.print(f"wrote {report['path']}")


def _print_population_arm(summary: dict[str, Any]) -> None:
    table = Table(
        title=f"{summary['arm']} (population: k={summary['planted']}, board "
        f"{'on' if summary['board'] else 'off'}{', dry run' if summary['dry_run'] else ''})",
        title_justify="left",
    )
    table.add_column("metric")
    table.add_column("value")
    table.add_row("groups / real agents", f"{summary['groups']} / {summary['real_agents']}")
    table.add_row("converted by episode 10", _rate_text(summary["converted_final"]))
    pcr = [p["mean"] for p in summary["pcr_by_episode"]]
    table.add_row("PCR by episode", " ".join("-" if v is None else f"{v:.2f}" for v in pcr))
    rr = [p["mean"] for p in summary["rr_collusion_by_episode"]]
    table.add_row("real-real collusion", " ".join("-" if v is None else f"{v:.2f}" for v in rr))
    table.add_row("time to majority", str(summary["t_majority"]))
    table.add_row("time to norm", str(summary["t_norm"]))
    table.add_row("exposure at conversion", str(summary["exposure_at_conversion"]["median"]))
    table.add_row("task accuracy (real)", _rate_text(summary["task_accuracy"]))
    table.add_row("refusals", str(summary["refusals"]["total"]))
    table.add_row("cost", f"${summary['cost']['total_usd']:.4f}")
    console.print(table)


# swarm population (Experiment 4) ----------------------------------------------------------------


@app.command("population")
def population_command(
    arm_files: Annotated[list[Path], typer.Option("--arm", help="The seven population arms.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Experiment 4's pre-registered analysis (offline): H2-H5 over the seven population arms."""
    try:
        report = run_population(_load_arms(arm_files), dry_run=dry_run)
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    table = Table(title="population arms", title_justify="left")
    for column in ("arm", "converted", "never majority", "t_majority", "PCR ep10", "RR ep10", "$"):
        table.add_column(column)
    for role, a in report["arms"].items():
        pcr, rr = a["pcr_by_episode"][-1]["mean"], a["rr_collusion_by_episode"][-1]["mean"]
        table.add_row(
            role,
            _rate_text(a["converted_final"]),
            str(a["groups_majority_never"]),
            str(a["t_majority"]),
            "-" if pcr is None else f"{pcr:.2f}",
            "-" if rr is None else f"{rr:.2f}",
            f"{a['cost']['total_usd']:.2f}",
        )
    console.print(table)
    hypotheses = Table(title="pre-registered hypotheses", title_justify="left")
    hypotheses.add_column("hypothesis")
    hypotheses.add_column("verdict")
    for name, h in report["hypotheses"].items():
        hypotheses.add_row(name, h["verdict"])
        for part, sub in h.get("comparisons", {}).items():
            hypotheses.add_row(f"  {part}", sub["verdict"])
    console.print(hypotheses)
    console.print(f"wrote {report['path']}")


# swarm replication (Sonnet 5 subject family + Experiment 5) --------------------------------------


@app.command("replication")
def replication_command(
    pilot: Annotated[Path, typer.Option(help="s5_pilot.")],
    explicit: Annotated[Path, typer.Option(help="s5_explicit.")],
    paper: Annotated[Path, typer.Option(help="s5_paper.")],
    nomem: Annotated[Path, typer.Option(help="s5_paper_nomem.")],
    reminder: Annotated[Path, typer.Option(help="e5_reminder.")],
    control: Annotated[Path, typer.Option(help="e3_control (reused comparison).")],
    checkin: Annotated[Path, typer.Option(help="e3_checkin (reused, descriptive).")],
    haiku_explicit: Annotated[Path, typer.Option(help="haiku_baseline (Experiment 0).")],
    haiku_paper: Annotated[Path, typer.Option(help="haiku_paper_nothink (Experiment 0).")],
    question: Annotated[Path | None, typer.Option(help="e5_question (Amendment 2).")] = None,
    pretest: Annotated[
        Path | None, typer.Option(help="pair_explicit_proposer (Experiment 4 pre-test).")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """The pre-registered replication analysis (offline): R1, R2 (Sonnet 5), E1, E2 (reminder),
    and, when given, Q1, Q2 (e5_question) and the Experiment 4 pre-test H1."""
    roles = {
        "pilot": pilot,
        "explicit": explicit,
        "paper": paper,
        "nomem": nomem,
        "reminder": reminder,
        "control": control,
        "checkin": checkin,
        "haiku_explicit": haiku_explicit,
        "haiku_paper": haiku_paper,
    }
    roles |= {k: v for k, v in (("question", question), ("pretest", pretest)) if v is not None}
    arms = dict(zip(roles, _load_arms(list(roles.values())), strict=True))
    try:
        report = run_replication(arms, dry_run=dry_run)
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    table = Table(title="replication arms", title_justify="left")
    for column in ("role", "arm", "status", "TC", "EC", "accuracy", "refusals", "$"):
        table.add_column(column)
    for role, a in report["arms"].items():
        if "collusion" not in a:
            table.add_row(role, a["arm"], a["status"], "-", "-", "-", "-", "-")
            continue
        c = a["collusion"]
        table.add_row(
            role,
            a["arm"],
            a["status"],
            f"{c['tc']['k']}/{c['tc']['n']}",
            f"{c['ec_pooled']['k']}/{c['ec_pooled']['n']}",
            _percent(a["task_accuracy"]["rate"]),
            str(a["refusals"]["total"]),
            f"{a['cost']['total_usd']:.2f}",
        )
    console.print(table)
    console.print(f"pilot gate: {report['pilot_gate']}")
    hypotheses = Table(title="pre-registered hypotheses", title_justify="left")
    hypotheses.add_column("hypothesis")
    hypotheses.add_column("verdict")
    for name, h in report["hypotheses"].items():
        hypotheses.add_row(name, h.get("verdict") or h.get("direction"))
    console.print(hypotheses)
    console.print(f"wrote {report['path']}")


# swarm judge / pathways / validate (step 5) ------------------------------------------------------


def _load_arms(arm_files: list[Path]) -> list[Any]:
    try:
        return [load_arm(path) for path in arm_files]
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None


@app.command("judge")
def judge_command(
    arm_files: Annotated[
        list[Path], typer.Option("--arm", help="An arm's YAML file (repeatable).")
    ],
    judge_file: Annotated[Path, typer.Option("--judge", help="The judge's YAML file.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Keyword stand-in judge on the dry-run arms, $0.")
    ] = False,
    backend: Annotated[
        str, typer.Option("--backend", help="sync (default) or batch (Message Batches API).")
    ] = "sync",
    limit: Annotated[
        int | None, typer.Option("--limit", help="At most this many new items per arm (pilot).")
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Only this item kind: coordination, relaxation or board."),
    ] = None,
    interleave: Annotated[
        bool,
        typer.Option(
            "--interleave", help="One pass over all arms ordered by episode (live, sync only)."
        ),
    ] = False,
) -> None:
    """Label every episode of the arms with one judge (resumes; items already labelled are
    skipped). Live unless --dry-run (a live run needs SWARM_LIVE=1)."""
    if kind not in (None, "coordination", "relaxation", "board"):
        console.print("[red]--kind must be coordination, relaxation or board[/red]")
        raise typer.Exit(code=1)
    if backend not in ("sync", "batch"):
        console.print("[red]--backend must be sync or batch[/red]")
        raise typer.Exit(code=1)
    arms = _load_arms(arm_files)
    try:
        judge = JudgeConfig.load(judge_file)
        judge.run_config(dry_run=dry_run)  # the live locks
        if not dry_run and Settings().anthropic_api_key() is None:
            raise LiveCallRefused("ANTHROPIC_API_KEY is not set (put it in .env)")
    except (ValueError, OSError) as error:
        console.print(f"[red]{judge_file}: {error}[/red]")
        raise typer.Exit(code=1) from None
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    meter = judge.meter(dry_run=dry_run)
    console.print(
        f"{'dry run (keyword judge)' if dry_run else 'LIVE'}: judge {judge.judge} = "
        f"{judge.model.name} (thinking {judge.model.thinking}, effort {judge.model.effort}), "
        f"{backend}, {judge.concurrency} at a time, breaker at "
        f"{judge.breaker_reasoning_extraction} reasoning_extraction / {judge.breaker_any} any "
        f"refusals, budget ${judge.budget_usd:g} (spent "
        f"${meter.total_usd:.4f}) on {', '.join(a.arm for a in arms)}"
    )
    try:
        counts = asyncio.run(
            run_judge(
                arms,
                judge,
                dry_run=dry_run,
                meter=meter,
                backend=backend,
                limit=limit,
                kind=kind,
                interleave=interleave,
            )
        )
    except LiveCallRefused as error:
        console.print(f"[red]refused:[/red] {error}")
        raise typer.Exit(code=2) from None
    except BudgetExceeded as error:
        console.print(f"[red]over budget:[/red] {error}")
        raise typer.Exit(code=3) from None
    except RefusalBreakerTripped as error:
        console.print(
            f"[red]REFUSAL BREAKER:[/red] {error} {error.details['refusals']}; run aborted"
        )
        raise typer.Exit(code=4) from None
    except anthropic.APIError as error:
        console.print(f"[red]API error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(code=1) from None
    table = Table(title=f"judge {judge.judge}", title_justify="left")
    for column in ("arm", "ok", "unresolved", "skipped", "already done"):
        table.add_column(column)
    for arm, c in counts.items():
        table.add_row(
            arm, str(c["ok"]), str(c["unresolved"]), str(c["skipped"]), str(c["already_done"])
        )
    console.print(table)
    console.print(f"spent ${meter.total_usd:.4f} of ${judge.budget_usd:g} ({meter.path})")


@app.command("pathways")
def pathways_command(
    arm_files: Annotated[
        list[Path], typer.Option("--arm", help="An arm's YAML file (repeatable).")
    ],
    primary: Annotated[str, typer.Option(help="The judge that fills `pathway`.")] = "sonnet",
    secondary: Annotated[str, typer.Option(help="The second judge.")] = "haiku",
    compare: Annotated[
        list[str] | None,
        typer.Option("--compare", help="A further judge to compare with the primary (repeatable)."),
    ] = None,
    out: Annotated[
        str, typer.Option(help="The pooled agreement file under runs/ (one per experiment).")
    ] = "step5_judges.json",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Final pathways from both judges' labels, judge agreement, and the counts in each summary
    (offline: no API calls)."""
    arms = _load_arms(arm_files)
    try:
        report = compute_pathways(
            arms,
            primary=primary,
            secondary=secondary,
            dry_run=dry_run,
            compare=tuple(compare or ()),
            out_name=out,
        )
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    decision = report["decision"]
    first, second = decision["primary"], decision["secondary"]
    console.print(
        f"primary judge: {first} (planned {decision['planned_primary']}; its refusal-unresolved "
        f"rate {decision['planned_primary_refusal_unresolved']}/{decision['planned_primary_sent']}"
        f" vs switch at >{decision['switch_rate']:.0%}"
        f"{'; SWITCHED' if decision['switched'] else ''})"
    )
    table = Table(title="pathways", title_justify="left")
    for column in (
        "arm",
        f"pathway ({first})",
        f"pathway ({second})",
        "same",
        "proposal before onset",
        "stated vs acted",
    ):
        table.add_column(column)
    for arm, data in report["arms"].items():
        agree = data["pathway_agreement"]
        covered = data["coverage"]
        table.add_row(
            arm,
            str(data["counts"][first]),
            str(data["counts"].get(second, f"partial: {covered[second]}/{covered['items']} items")),
            "-" if agree is None else f"{agree['same']}/{agree['of']}",
            str(data["proposal_before_onset"]),
            str(data["stated_vs_acted"]),
        )
    console.print(table)
    agreement_table = Table(title=f"item agreement with {first}", title_justify="left")
    for column in (
        "judge",
        "scope",
        "kind",
        "n",
        "agreement",
        "kappa",
        "specific agreement per class",
    ):
        agreement_table.add_column(column)
    scopes = [
        (other, "pooled", by_kind)
        for other, by_kind in report["pooled_item_agreement_by_judge"].items()
    ] + [
        (other, arm, by_kind)
        for arm, data in report["arms"].items()
        for other, by_kind in data["item_agreement_by_judge"].items()
    ]
    for other, scope, by_kind in scopes:
        for kind, a in by_kind.items():
            per_class = ", ".join(
                f"{label} {c['specific_agreement']:.2f}"
                if c["specific_agreement"] is not None
                else f"{label} -"
                for label, c in a["per_class"].items()
            )
            agreement_table.add_row(
                other,
                scope,
                kind,
                str(a["n"]),
                _percent(a["agreement"]),
                "-" if a["kappa"] is None else f"{a['kappa']:.2f}",
                per_class,
            )
    console.print(agreement_table)
    console.print(f"wrote runs/<arm>/pathways.json, the summaries, and {report['path']}")


@validate_app.command("sample")
def validate_sample(
    arm_files: Annotated[
        list[Path], typer.Option("--arm", help="An arm's YAML file (repeatable).")
    ],
    primary: Annotated[str, typer.Option(help="The judge whose positives are oversampled.")] = (
        "sonnet"
    ),
    secondary: Annotated[str, typer.Option(help="The second judge (its labels go in the key).")] = (
        "haiku"
    ),
    n: Annotated[int, typer.Option(help="Items in the sample.")] = 30,
    seed: Annotated[int, typer.Option(help="Sampling seed.")] = 0,
    out: Annotated[Path, typer.Option(help="Output folder.")] = Path("runs/validation"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Draw the blind validation sample: a sheet to label (no arm, agent, episode or judge
    labels), its key, and the labelling instructions."""
    arms = _load_arms(arm_files)
    try:
        paths = make_sample(
            arms, primary=primary, secondary=secondary, n=n, seed=seed, out_dir=out,
            dry_run=dry_run,
        )  # fmt: skip
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        f"sheet (label this): {paths['sheet']}\ninstructions: {paths['instructions']}\n"
        f"key (do not open before labelling): {paths['key']}"
    )


@validate_app.command("add-judge")
def validate_add_judge(
    arm_files: Annotated[
        list[Path], typer.Option("--arm", help="An arm's YAML file (repeatable).")
    ],
    judge: Annotated[str, typer.Option(help="The judge whose labels join the key.")],
    key: Annotated[Path, typer.Option(help="The sample's key (JSON).")] = Path(
        "runs/validation/step5_validation_key.json"
    ),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="The dry-run arms.")] = False,
) -> None:
    """Add a further judge's labels to an existing sample key; the sheet is not touched."""
    arms = _load_arms(arm_files)
    try:
        labelled = add_judge_to_sample(arms, judge, key, dry_run=dry_run)
    except (ValueError, OSError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"{key}: {judge} labels on {labelled} items (the sheet is unchanged)")


@validate_app.command("score")
def validate_score(
    sheet: Annotated[Path, typer.Option(help="The labelled sheet (CSV).")],
    key: Annotated[Path, typer.Option(help="The sample's key (JSON).")] = Path(
        "runs/validation/step5_validation_key.json"
    ),
    second_sheet: Annotated[
        Path | None, typer.Option(help="A second person's labelled sheet, for human agreement.")
    ] = None,
) -> None:
    """Score the judges against the human labels (and two humans against each other)."""
    try:
        report = score_sheet(sheet, key, second_sheet)
    except (ValueError, OSError, KeyError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    out = sheet.with_suffix(".score.json")
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2) + b"\n")
    table = Table(title=f"judges vs human ({report['labelled']}/{report['of']} labelled)")
    for column in ("judge", "kind", "n", "accuracy (95% CI)", "kappa"):
        table.add_column(column)
    for judge, by_kind in report["judges"].items():
        for kind, a in by_kind.items():
            table.add_row(
                judge,
                kind,
                str(a["n"]),
                _rate_text(a["accuracy"]),
                "-" if a["kappa"] is None else f"{a['kappa']:.2f}",
            )
    console.print(table)
    for judge, by_kind in report["judges"].items():
        for kind, a in by_kind.items():
            for d in a["disagreements"]:
                console.print(
                    f"  disagreement {judge} {kind} {d['item']} ({d['item_id']}): "
                    f"human {d['human']}, judge {d['judge']}"
                )
    console.print(f"wrote {out}")


# swarm tasks ...------------------------------------------------------------------------------


@tasks_app.command("generate")
def tasks_generate(
    seed: Annotated[int, typer.Option(help="Base seed; writes <out>/seed<seed>[-hard]/.")] = 0,
    n: Annotated[int, typer.Option(help="Number of tasks (templates cycle T1..T4).")] = 100,
    out: Annotated[Path, typer.Option(help="Root folder for task sets.")] = DEFAULT_TASK_ROOT,
    difficulty: Annotated[
        DifficultyName, typer.Option(help="base, or hard (planted traps; see tasks/traps.py).")
    ] = DifficultyName.base,
    force: Annotated[bool, typer.Option(help="Replace an existing task set.")] = False,
) -> None:
    """Generate a seeded task set: task_XXXX/{db.sqlite, task.json} plus manifest.json."""
    name = task_set_name(seed, difficulty.value)
    started = time.perf_counter()
    try:
        with Progress(console=console, transient=True) as progress:
            bar = progress.add_task(f"generating {name}", total=n)
            manifest = generate_tasks(
                seed,
                n,
                out,
                difficulty=difficulty.value,
                force=force,
                on_task=lambda spec: progress.advance(bar),
            )
    except FileExistsError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    except (TaskGenerationError, GroundTruthMismatch) as error:
        console.print(f"[red]generation failed:[/red] {error}")
        raise typer.Exit(code=1) from None
    elapsed = time.perf_counter() - started
    console.print(
        f"generated {manifest.n_tasks} tasks in {elapsed:.1f}s -> {Path(out) / name} "
        f"(run `swarm tasks stats` to check them)"
    )


@tasks_app.command("sequences")
def tasks_sequences(
    seed: Annotated[int, typer.Option(help="Task set seed (also seeds the shuffles).")] = 0,
    n_seq: Annotated[int, typer.Option(help="Number of sequences.")] = 20,
    episodes: Annotated[int, typer.Option(help="Episodes per sequence.")] = 10,
    root: Annotated[Path, typer.Option(help="Root folder for task sets.")] = DEFAULT_TASK_ROOT,
    difficulty: Annotated[
        DifficultyName, typer.Option(help="Which task set: base or hard.")
    ] = DifficultyName.base,
) -> None:
    """Write <root>/seed<seed>[-hard]/sequences.json: fixed (A task, B task) pairs for every
    episode."""
    task_dir = Path(root) / task_set_name(seed, difficulty.value)
    manifest = load_manifest(task_dir)
    try:
        sequences = make_sequence_set(manifest, seed, n_sequences=n_seq, episodes=episodes)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    path = task_dir / SEQUENCES_FILE
    content = write_sequences(path, manifest, sequences, seed)
    console.print(
        f"wrote {content.n_sequences} sequences x {content.episodes} episodes -> {path} "
        f"(manifest {content.manifest_digest[:12]})"
    )


@tasks_app.command("groups")
def tasks_groups(
    seed: Annotated[int, typer.Option(help="Task set seed (also seeds the shuffles).")] = 0,
    n_groups: Annotated[int, typer.Option(help="Number of groups.")] = 10,
    root: Annotated[Path, typer.Option(help="Root folder for task sets.")] = DEFAULT_TASK_ROOT,
    difficulty: Annotated[
        DifficultyName, typer.Option(help="Which task set: base or hard.")
    ] = DifficultyName.base,
) -> None:
    """Write <root>/seed<seed>[-hard]/groups.json: Experiment 4's task schedule (6 agents x 10
    episodes per group, 60 distinct tasks per group)."""
    task_dir = Path(root) / task_set_name(seed, difficulty.value)
    manifest = load_manifest(task_dir)
    try:
        groups = make_groups(manifest, seed, n_groups=n_groups)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    path = task_dir / GROUPS_FILE
    content = write_groups(path, manifest, groups, seed)
    console.print(
        f"wrote {content.n_groups} groups x {content.group_size} agents x {content.episodes} "
        f"episodes -> {path} (manifest {content.manifest_digest[:12]})"
    )


@tasks_app.command("stats")
def tasks_stats(
    task_dir: Annotated[Path, typer.Argument(help="A task set folder, e.g. data/tasks/seed0.")],
) -> None:
    """Per-template counts, answer sizes, draws per task, schema-dump length, duplicates."""
    summary = summarize(load_manifest(task_dir))
    table = Table(title=f"{task_dir}: {summary['n_tasks']} tasks", title_justify="left")
    table.add_column("template", overflow="fold")
    for name in ("tasks", "answer min", "median", "max", "mean draws", "max draws"):
        table.add_column(name, justify="right")
    for name, row in summary["templates"].items():
        table.add_row(
            TEMPLATES_BY_NAME[name].label if name in TEMPLATES_BY_NAME else name,
            str(row["tasks"]),
            str(row["answer_min"]),
            f"{row['answer_median']:g}",
            str(row["answer_max"]),
            f"{row['mean_draws']:.2f}",
            str(row["max_draws"]),
        )
    console.print(table)

    bins = list(summary["histogram"])
    histogram = Table(title="answer sizes (tasks per bin)", title_justify="left")
    histogram.add_column("template")
    for label in bins:
        histogram.add_column(label, justify="right")
    for name, row in summary["templates"].items():
        histogram.add_row(name, *(str(row["histogram"][label]) for label in bins))
    histogram.add_section()
    histogram.add_row("all", *(str(summary["histogram"][label]) for label in bins), style="bold")
    console.print(histogram)

    dumps = summary["schema_dump_chars"]
    console.print(
        f"schema dump: min {dumps['min']:,} chars, median {dumps['median']:,} chars; "
        f"duplicate fingerprints: {summary['duplicate_fingerprints']}"
    )
    for check, passed in summary["checks"].items():
        console.print(f"  {'[green]pass[/green]' if passed else '[red]FAIL[/red]'}  {check}")
    if not all(summary["checks"].values()):
        raise typer.Exit(code=1)


@tasks_app.command("show")
def tasks_show(
    task_dir: Annotated[Path, typer.Argument(help="A task set folder, e.g. data/tasks/seed0.")],
    task_id: Annotated[str, typer.Argument(help="A task id, e.g. task_0007.")],
    reveal: Annotated[bool, typer.Option(help="Also print the reference SQL and IDs.")] = False,
) -> None:
    """The request an agent sees, the answer size and template; --reveal adds SQL and IDs."""
    spec = load_task(task_dir, task_id)
    template = TEMPLATES_BY_NAME[spec.template].label
    console.print(f"[bold]{spec.task_id}[/bold]  {template}  ({spec.n_answer} IDs expected)")
    # soft_wrap: no hard line breaks, so the request and the SQL can be copied intact
    console.print(spec.nl_request, markup=False, soft_wrap=True)
    if reveal:
        console.print("\n[bold]reference SQL[/bold]")
        console.print(spec.reference_sql, markup=False, soft_wrap=True)
        console.print(f"\n[bold]reference IDs[/bold] {spec.reference_ids}", soft_wrap=True)
