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
from swarm.analysis.validation import sample_items, write_sample
from swarm.arms import ArmConfig
from swarm.config import RUNS_DIR
from swarm.llm.client import AnthropicClient, ScriptedClient
from swarm.llm.meter import CostMeter
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
) -> dict[str, dict[str, int]]:
    """Label every arm's items with one judge; counts by status, per arm."""
    run = judge.run_config(dry_run=dry_run)
    counts: dict[str, dict[str, int]] = {}
    if dry_run:
        clients = {
            kind: ScriptedClient(dry_run_script(kind), run=run, meter=meter) for kind in KINDS
        }
        for arm in arms:
            run_dir = arm.run_dir(dry_run=True)
            counts[arm.arm] = await judge_items(
                arm_items(arm.arm, run_dir),
                clients.__getitem__,
                judge,
                judgments_path(run_dir, judge.judge),
                limit=limit,
            )
        return counts
    async with AnthropicClient(run, meter) as client:
        for arm in arms:
            run_dir = arm.run_dir(dry_run=False)
            items = arm_items(arm.arm, run_dir)
            out = judgments_path(run_dir, judge.judge)
            if backend == "batch":
                counts[arm.arm] = await judge_items_batch(items, client, judge, out)
            else:
                counts[arm.arm] = await judge_items(
                    items, lambda kind: client, judge, out, limit=limit
                )
    return counts


def _trajectories(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / TRAJECTORIES_FILE).read_bytes().splitlines()
    trajectories = [orjson.loads(line) for line in lines if line.strip()]
    stale = [t["sequence_id"] for t in trajectories if t.get("criterion") != "tightened"]
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
    arms: list[ArmConfig], *, primary: str, secondary: str, dry_run: bool
) -> dict[str, Any]:
    """Pathways per arm under each judge that labelled every item of that arm, item agreement
    per arm and pooled (on items both judges resolved), files written.

    The judge that ends up primary (after the switch rule) must cover every item of every arm.
    The other judge may be partial (step 5: Sonnet 5.5 was suspended mid-run): its pathways are
    computed only for arms it covered completely, and its coverage is reported."""
    per_arm: dict[str, dict[str, dict[str, Any]]] = {}
    item_ids: dict[str, set[str]] = {}
    pooled: dict[str, dict[str, dict[str, Any]]] = {primary: {}, secondary: {}}
    for arm in arms:
        run_dir = arm.run_dir(dry_run=dry_run)
        item_ids[arm.arm] = {item.item_id for item in arm_items(arm.arm, run_dir)}
        per_arm[arm.arm] = {}
        for judge in (primary, secondary):
            lines = load_judgments(judgments_path(run_dir, judge))
            per_arm[arm.arm][judge] = lines
            pooled[judge].update(lines)
    decision = choose_primary(pooled, primary, secondary)
    first, second = decision["primary"], decision["secondary"]
    coverage = {
        arm: {judge: len(item_ids[arm] & set(per_arm[arm][judge])) for judge in (first, second)}
        | {"items": len(item_ids[arm])}
        for arm in item_ids
    }
    incomplete = [arm for arm, c in coverage.items() if c[first] < c["items"]]
    if incomplete:
        raise ValueError(f"primary judge {first} has not labelled every item of {incomplete}")
    report: dict[str, Any] = {"decision": decision, "coverage": coverage, "arms": {}}
    for arm in arms:
        run_dir = arm.run_dir(dry_run=dry_run)
        trajectories = _trajectories(run_dir)
        complete = [j for j in (first, second) if coverage[arm.arm][j] == len(item_ids[arm.arm])]
        by_judge = {
            judge: arm_pathways(trajectories, per_arm[arm.arm][judge]) for judge in complete
        }
        pathway_agreement = None
        if second in by_judge:
            same = [
                a["pathway"] == b["pathway"]
                for a, b in zip(
                    by_judge[first]["trajectories"], by_judge[second]["trajectories"], strict=True
                )
            ]
            pathway_agreement = {"same": sum(same), "of": len(same)}
        result = {
            "arm": arm.arm,
            "primary": first,
            "secondary": second,
            "decision": decision,
            "coverage": coverage[arm.arm],
            "by_judge": by_judge,
            "pathway_agreement": pathway_agreement,
            "item_agreement": judge_agreement(
                per_arm[arm.arm][first], per_arm[arm.arm][second], LABELS
            ),
        }
        _write(run_dir / PATHWAYS_FILE, result)
        attach_pathways(run_dir)
        report["arms"][arm.arm] = {
            "pathway_agreement": pathway_agreement,
            "item_agreement": result["item_agreement"],
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
                for judge in (first, second)
            },
            "labels": {judge: data["labels"] for judge, data in by_judge.items()},
        }
    report["pooled_item_agreement"] = judge_agreement(pooled[first], pooled[second], LABELS)
    out = RUNS_DIR / (POOLED_FILE if not dry_run else POOLED_FILE.replace(".json", "_dry.json"))
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
