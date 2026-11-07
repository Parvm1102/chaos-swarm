"""Step 5 orchestration: run a judge over arms, compute pathways and agreement, draw the sample.

run_judge labels every item of the given arms with one judge (`swarm judge`), all through one
client and one cost meter in runs/judges_<judge>/ (one cap for the judge across arms). The
judgment lines go to runs/<arm>/judgments/<judge>.jsonl.

compute_pathways (`swarm pathways`) needs both judges' lines. The primary judge fills `pathway`.
Rule fixed before the live run (the user's: "if Sonnet refuses on judge prompts, Haiku becomes
primary"): if more than PRIMARY_SWITCH_RATE (5%) of the items sent to the primary judge, pooled
over the arms, end unresolved after a refusal, the two judges swap. It writes
runs/<arm>/pathways.json, the pathway counts into runs/<arm>/summary.json, and the pooled
agreement into runs/step5_judges.json.

make_sample (`swarm validate sample`) draws the blind sample (swarm.analysis.validation).
"""

from collections import Counter
from pathlib import Path
from typing import Any

import orjson

from swarm.analysis.judges import (
    KINDS,
    LABELS,
    Item,
    JudgeConfig,
    arm_items,
    dry_run_script,
    judge_items,
    judge_items_batch,
    judgments_path,
    load_judgments,
)
from swarm.analysis.pathways import arm_pathways, judge_agreement
from swarm.analysis.validation import add_judge_to_key, sample_items, write_sample
from swarm.arms import ArmConfig
from swarm.config import RUNS_DIR
from swarm.llm.client import AnthropicClient, ScriptedClient
from swarm.llm.meter import CostMeter, RefusalBreakerTripped, breaker_history
from swarm.trajectories import SUMMARY_FILE, TRAJECTORIES_FILE

PATHWAYS_FILE = "pathways.json"
POOLED_FILE = "step5_judges.json"
PRIMARY_SWITCH_RATE = 0.05


async def run_judge(
    arms: list[ArmConfig],
    judge: JudgeConfig,
    *,
    dry_run: bool,
    meter: CostMeter,
    backend: str = "sync",
    limit: int | None = None,
    kind: str | None = None,
    interleave: bool = False,
) -> dict[str, dict[str, int]]:
    """Label every arm's items (of one kind, if given) with one judge; counts by status, per arm.

    interleave: one pass over all arms' items ordered by (episode, sequence, arm, agent), so a
    run stopped by its cap covers every arm evenly (counts under the key "interleaved").

    Writes runs/judges_<judge>/summary.json in every case, with `aborted_by_breaker`; a refusal
    breaker trip is re-raised after that."""
    run = judge.run_config(dry_run=dry_run)
    counts: dict[str, dict[str, int]] = {}
    tripped: dict[str, Any] | None = None
    try:
        if dry_run:
            clients = {k: ScriptedClient(dry_run_script(k), run=run, meter=meter) for k in KINDS}
            for arm in arms:
                run_dir = arm.run_dir(dry_run=True)
                counts[arm.arm] = await judge_items(
                    _of_kind(arm_items(arm.arm, run_dir), kind),
                    clients.__getitem__,
                    judge,
                    judgments_path(run_dir, judge.judge),
                    limit=limit,
                )
        elif interleave:
            async with AnthropicClient(run, meter) as client:
                counts["interleaved"] = await _interleaved(arms, judge, kind, lambda _k: client)
        else:
            async with AnthropicClient(run, meter) as client:
                for arm in arms:
                    run_dir = arm.run_dir(dry_run=False)
                    items = _of_kind(arm_items(arm.arm, run_dir), kind)
                    out = judgments_path(run_dir, judge.judge)
                    if backend == "batch":
                        counts[arm.arm] = await judge_items_batch(items, client, judge, out)
                    else:
                        counts[arm.arm] = await judge_items(
                            items, lambda _kind: client, judge, out, limit=limit
                        )
    except RefusalBreakerTripped as trip:
        tripped = trip.details
        raise
    finally:
        _write_judge_summary(arms, judge, run.run_dir, meter, dry_run, kind, tripped)
    return counts


async def _interleaved(
    arms: list[ArmConfig], judge: JudgeConfig, kind: str | None, client_for: Any
) -> dict[str, int]:
    order = {arm.arm: index for index, arm in enumerate(arms)}
    paths = {arm.arm: judgments_path(arm.run_dir(dry_run=False), judge.judge) for arm in arms}
    items = [
        item
        for arm in arms
        for item in _of_kind(arm_items(arm.arm, arm.run_dir(dry_run=False)), kind)
    ]
    items.sort(key=lambda i: (i.episode, i.sequence_id, order[i.arm], i.agent or ""))
    return await judge_items(items, client_for, judge, lambda item: paths[item.arm])


def _of_kind(items: list[Item], kind: str | None) -> list[Item]:
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return [item for item in items if kind is None or item.kind == kind]


def _write_judge_summary(
    arms: list[ArmConfig],
    judge: JudgeConfig,
    run_dir: Path,
    meter: CostMeter,
    dry_run: bool,
    kind: str | None,
    tripped: dict[str, Any] | None,
) -> None:
    """runs/judges_<judge>/summary.json: what this judge has labelled so far, from the files."""
    arms_report = {}
    for arm in arms:
        lines = load_judgments(judgments_path(arm.run_dir(dry_run=dry_run), judge.judge))
        arms_report[arm.arm] = {
            k: dict(
                sorted(
                    Counter(line["status"] for line in lines.values() if line["kind"] == k).items()
                )
            )
            for k in KINDS
        }
    _write(
        run_dir / "summary.json",
        {
            "judge": judge.judge,
            "model": judge.model.model_dump(mode="json"),
            "concurrency": judge.concurrency,
            "budget_usd": judge.budget_usd,
            "dry_run": dry_run,
            "last_invocation_kind": kind,
            "arms": arms_report,
            "total_usd": meter.total_usd,
            "refusals_this_invocation": dict(meter.refusals),
            "aborted_by_breaker": tripped is not None,
            "breaker": tripped,
            "breaker_trips": breaker_history(run_dir),
        },
    )


def _trajectories(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / TRAJECTORIES_FILE).read_bytes().splitlines()
    trajectories = [orjson.loads(line) for line in lines if line.strip()]
    stale = [
        t["sequence_id"]
        for t in trajectories
        if t.get("criterion") not in ("tightened", "delivered")
    ]
    if stale:
        raise ValueError(
            f"{run_dir}: trajectories {stale} predate the tightened criterion; "
            "run `swarm summarize` first"
        )
    return trajectories


def choose_primary(
    judgments: dict[str, dict[str, dict[str, Any]]], primary: str, secondary: str
) -> dict[str, Any]:
    """Apply the switch rule over the pooled judgments of every arm (module docstring)."""
    lines = list(judgments[primary].values())
    sent = sum(1 for line in lines if line["status"] != "skipped")
    refused = sum(
        1
        for line in lines
        if line["status"] == "unresolved"
        and any(a.get("problem") == "refusal" for a in line["attempts"])
    )
    rate = refused / sent if sent else 0.0
    switched = rate > PRIMARY_SWITCH_RATE
    return {
        "planned_primary": primary,
        "primary": secondary if switched else primary,
        "secondary": primary if switched else secondary,
        "planned_primary_sent": sent,
        "planned_primary_refusal_unresolved": refused,
        "planned_primary_refusal_rate": rate,
        "switch_rate": PRIMARY_SWITCH_RATE,
        "switched": switched,
    }


def compute_pathways(
    arms: list[ArmConfig],
    *,
    primary: str,
    secondary: str,
    dry_run: bool,
    compare: tuple[str, ...] = (),
    out_name: str = POOLED_FILE,
) -> dict[str, Any]:
    """Pathways per arm under each judge that labelled every item of that arm, item agreement
    with the primary per arm and pooled (on items both judges resolved), files written.

    The switch rule decides between `primary` and `secondary`; `compare` adds further judges
    (step 5: Sonnet 5), which never fill `pathway` and are only compared with the primary. The
    judge that ends up primary must cover every item of every arm. Any other judge may be
    partial: its pathways are computed for every arm whose coordination items it covered
    completely (a pathway needs only those); `notes_complete` says whether its stated turning
    points rest on every note."""
    planned = list(dict.fromkeys((primary, secondary, *compare)))
    per_arm: dict[str, dict[str, dict[str, Any]]] = {}
    item_ids: dict[str, set[str]] = {}
    coordination_ids: dict[str, set[str]] = {}
    pooled: dict[str, dict[str, dict[str, Any]]] = {judge: {} for judge in planned}
    for arm in arms:
        run_dir = arm.run_dir(dry_run=dry_run)
        items = arm_items(arm.arm, run_dir)
        item_ids[arm.arm] = {item.item_id for item in items}
        coordination_ids[arm.arm] = {i.item_id for i in items if i.kind == "coordination"}
        per_arm[arm.arm] = {}
        for judge in planned:
            lines = load_judgments(judgments_path(run_dir, judge))
            per_arm[arm.arm][judge] = lines
            pooled[judge].update(lines)
    decision = choose_primary(pooled, primary, secondary)
    first, second = decision["primary"], decision["secondary"]
    others = [judge for judge in planned if judge != first]
    judges = [first, *others]
    coverage = {
        arm: {judge: len(item_ids[arm] & set(per_arm[arm][judge])) for judge in judges}
        | {"items": len(item_ids[arm])}
        for arm in item_ids
    }
    incomplete = [arm for arm, c in coverage.items() if c[first] < c["items"]]
    if incomplete:
        raise ValueError(f"primary judge {first} has not labelled every item of {incomplete}")
    report: dict[str, Any] = {"decision": decision, "compare": list(compare), "coverage": coverage,
                              "arms": {}}  # fmt: skip
    for arm in arms:
        run_dir = arm.run_dir(dry_run=dry_run)
        trajectories = _trajectories(run_dir)
        labelled = {j: set(per_arm[arm.arm][j]) for j in judges}
        complete = [j for j in judges if coordination_ids[arm.arm] <= labelled[j]]
        by_judge = {
            judge: arm_pathways(trajectories, per_arm[arm.arm][judge])
            | {"notes_complete": item_ids[arm.arm] <= labelled[judge]}
            for judge in complete
        }
        pathway_agreement_by_judge = {}
        for judge in others:
            if judge in by_judge:
                same = [
                    a["pathway"] == b["pathway"]
                    for a, b in zip(
                        by_judge[first]["trajectories"],
                        by_judge[judge]["trajectories"],
                        strict=True,
                    )
                ]
                pathway_agreement_by_judge[judge] = {"same": sum(same), "of": len(same)}
        item_agreement_by_judge = {
            judge: judge_agreement(per_arm[arm.arm][first], per_arm[arm.arm][judge], LABELS)
            for judge in others
        }
        result = {
            "arm": arm.arm,
            "primary": first,
            "secondary": second,
            "compare": list(compare),
            "decision": decision,
            "coverage": coverage[arm.arm],
            "by_judge": by_judge,
            "pathway_agreement": pathway_agreement_by_judge.get(second),
            "pathway_agreement_by_judge": pathway_agreement_by_judge,
            "item_agreement": item_agreement_by_judge[second],
            "item_agreement_by_judge": item_agreement_by_judge,
        }
        _write(run_dir / PATHWAYS_FILE, result)
        attach_pathways(run_dir)
        report["arms"][arm.arm] = {
            "pathway_agreement": result["pathway_agreement"],
            "pathway_agreement_by_judge": pathway_agreement_by_judge,
            "item_agreement": result["item_agreement"],
            "item_agreement_by_judge": item_agreement_by_judge,
            "coverage": coverage[arm.arm],
            "counts": {judge: data["counts"] for judge, data in by_judge.items()},
            "proposal_before_onset": by_judge[first]["proposal_before_onset"],
            "stated_vs_acted": by_judge[first]["stated_vs_acted"],
            "item_status": {
                judge: dict(
                    sorted(
                        Counter(line["status"] for line in per_arm[arm.arm][judge].values()).items()
                    )
                )
                for judge in judges
            },
            "labels": {judge: data["labels"] for judge, data in by_judge.items()},
        }
    report["pooled_item_agreement_by_judge"] = {
        judge: judge_agreement(pooled[first], pooled[judge], LABELS) for judge in others
    }
    report["pooled_item_agreement"] = report["pooled_item_agreement_by_judge"][second]
    out = RUNS_DIR / (out_name if not dry_run else out_name.replace(".json", "_dry.json"))
    _write(out, report)
    report["path"] = str(out)
    return report


def attach_pathways(run_dir: Path) -> None:
    """Copy the pathway counts from pathways.json into summary.json (if both exist)."""
    path, summary_path = run_dir / PATHWAYS_FILE, run_dir / SUMMARY_FILE
    if not path.exists() or not summary_path.exists():
        return
    result = orjson.loads(path.read_bytes())
    summary = orjson.loads(summary_path.read_bytes())
    summary["pathway"] = result["by_judge"][result["primary"]]["counts"]
    summary["pathway_judge"] = result["primary"]
    summary["pathway_by_judge"] = {
        judge: data["counts"] for judge, data in result["by_judge"].items()
    }
    _write(summary_path, summary)


def make_sample(
    arms: list[ArmConfig],
    *,
    primary: str,
    secondary: str,
    n: int,
    seed: int,
    out_dir: Path,
    dry_run: bool,
) -> dict[str, Path]:
    items: list[Item] = []
    judges: dict[str, dict[str, dict[str, Any]]] = {primary: {}, secondary: {}}
    for arm in arms:
        run_dir = arm.run_dir(dry_run=dry_run)
        items.extend(arm_items(arm.arm, run_dir))
        for judge in judges:
            judges[judge].update(load_judgments(judgments_path(run_dir, judge)))
    picked = sample_items(items, judges[primary], n=n, seed=seed)
    return write_sample(picked, judges, out_dir)


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2) + b"\n")


def add_judge_to_sample(arms: list[ArmConfig], judge: str, key_path: Path, *, dry_run: bool) -> int:
    lines: dict[str, dict[str, Any]] = {}
    for arm in arms:
        lines.update(load_judgments(judgments_path(arm.run_dir(dry_run=dry_run), judge)))
    return add_judge_to_key(key_path, judge, lines)
