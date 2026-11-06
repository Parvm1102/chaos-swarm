"""The `swarm` command line: check-env, ping, cost-report, and the `tasks` sub-app."""

import asyncio
import logging
import platform
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, get_args

import anthropic
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress
from rich.table import Table

from swarm.config import RUNS_DIR, LiveCallRefused, ModelConfig, Provider, RunConfig, Settings
from swarm.llm.client import AnthropicClient
from swarm.llm.meter import USAGE_FILE, BudgetExceeded, CostMeter, summarize_usage_file
from swarm.llm.pricing import cost_usd
from swarm.llm.types import LLMResponse, text_block, user_message
from swarm.tasks.generate import (
    GroundTruthMismatch,
    TaskGenerationError,
    generate_tasks,
    load_manifest,
    load_task,
    summarize,
)
from swarm.tasks.sequences import SEQUENCES_FILE, make_sequences, write_sequences
from swarm.tasks.templates import TEMPLATES_BY_NAME

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Swarm collusion harness.")
tasks_app = typer.Typer(
    no_args_is_help=True, help="Synthetic SQLite tasks: generate, sequence and inspect."
)
app.add_typer(tasks_app, name="tasks")
console = Console()
DEFAULT_TASK_ROOT = Path("data/tasks")

# The --provider choices come straight from config.Provider, so the CLI cannot drift from it.
ProviderName = StrEnum("ProviderName", {name: name for name in get_args(Provider)})

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


# swarm tasks ...------------------------------------------------------------------------------


@tasks_app.command("generate")
def tasks_generate(
    seed: Annotated[int, typer.Option(help="Base seed; writes <out>/seed<seed>/.")] = 0,
    n: Annotated[int, typer.Option(help="Number of tasks (templates cycle T1..T4).")] = 100,
    out: Annotated[Path, typer.Option(help="Root folder for task sets.")] = DEFAULT_TASK_ROOT,
    force: Annotated[bool, typer.Option(help="Replace an existing task set.")] = False,
) -> None:
    """Generate a seeded task set: task_XXXX/{db.sqlite, task.json} plus manifest.json."""
    started = time.perf_counter()
    try:
        with Progress(console=console, transient=True) as progress:
            bar = progress.add_task(f"generating seed{seed}", total=n)
            manifest = generate_tasks(
                seed, n, out, force=force, on_task=lambda spec: progress.advance(bar)
            )
    except FileExistsError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    except (TaskGenerationError, GroundTruthMismatch) as error:
        console.print(f"[red]generation failed:[/red] {error}")
        raise typer.Exit(code=1) from None
    elapsed = time.perf_counter() - started
    console.print(
        f"generated {manifest.n_tasks} tasks in {elapsed:.1f}s -> {Path(out) / f'seed{seed}'} "
        f"(run `swarm tasks stats` to check them)"
    )


@tasks_app.command("sequences")
def tasks_sequences(
    seed: Annotated[int, typer.Option(help="Task set seed (also seeds the shuffles).")] = 0,
    n_seq: Annotated[int, typer.Option(help="Number of sequences.")] = 20,
    episodes: Annotated[int, typer.Option(help="Episodes per sequence.")] = 10,
    root: Annotated[Path, typer.Option(help="Root folder for task sets.")] = DEFAULT_TASK_ROOT,
) -> None:
    """Write <root>/seed<seed>/sequences.json: fixed (A task, B task) pairs for every episode."""
    task_dir = Path(root) / f"seed{seed}"
    manifest = load_manifest(task_dir)
    try:
        sequences = make_sequences(manifest, seed, n_sequences=n_seq, episodes=episodes)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    path = task_dir / SEQUENCES_FILE
    content = write_sequences(path, manifest, sequences, seed)
    console.print(
        f"wrote {content.n_sequences} sequences x {content.episodes} episodes -> {path} "
        f"(manifest {content.manifest_digest[:12]})"
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
