"""Experiment 1 (spillover) orchestration: `swarm spillover` (offline).

run_spillover reads the three arms' summaries and trajectories and checks that every arm has
finished (all sequences, all episodes, not aborted by the refusal breaker) and that all three ran
under the same PREREG.md as the file now on disk. It then fixes the P3 split (the constrained
arm's median onset, rounded down; written once to runs/e1_p3_split.json, reused afterwards) and
computes the pre-registered hypotheses (swarm.analysis.spillover), writing runs/e1_spillover.json.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from swarm.analysis.spillover import arm_report, between_arms, p3, p3_control, p3_split
from swarm.arms import ArmConfig
from swarm.config import RUNS_DIR
from swarm.trajectories import SUMMARY_FILE, TRAJECTORIES_FILE

SPLIT_FILE = "e1_p3_split.json"
REPORT_FILE = "e1_spillover.json"


def _load(arm: ArmConfig, dry_run: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run_dir = arm.run_dir(dry_run=dry_run)
    summary = orjson.loads((run_dir / SUMMARY_FILE).read_bytes())
    lines = (run_dir / TRAJECTORIES_FILE).read_bytes().splitlines()
    trajectories = [orjson.loads(line) for line in lines if line.strip()]
    if summary.get("aborted_by_breaker"):
        raise ValueError(f"{arm.arm} was aborted by the refusal breaker; resume it first")
    short = [t["sequence_id"] for t in trajectories if t["episodes"] != arm.episodes]
    if len(trajectories) != len(arm.sequences) or short:
        raise ValueError(f"{arm.arm} has not finished ({len(trajectories)} trajectories, {short})")
    return summary, trajectories


def run_spillover(
    constrained: ArmConfig, unconstrained: ArmConfig, nomem: ArmConfig, *, dry_run: bool
) -> dict[str, Any]:
    arms = {"constrained": constrained, "unconstrained": unconstrained, "nomem": nomem}
    loaded = {role: _load(arm, dry_run) for role, arm in arms.items()}
    preregs = {role: summary["provenance"].get("prereg") for role, (summary, _) in loaded.items()}
    hashes = {p["sha256"] if p else None for p in preregs.values()}
    if len(hashes) != 1 or None in hashes:
        raise ValueError(f"the arms ran under different (or no) pre-registrations: {preregs}")
    prereg_path = Path(constrained.prereg) if constrained.prereg else None
    if prereg_path is None or hashlib.sha256(prereg_path.read_bytes()).hexdigest() not in hashes:
        raise ValueError(f"{prereg_path} on disk differs from the one the arms ran under")
    trajectories = {role: data[1] for role, data in loaded.items()}

    suffix = "_dry" if dry_run else ""
    split_path = RUNS_DIR / SPLIT_FILE.replace(".json", f"{suffix}.json")
    computed = p3_split(trajectories["constrained"])
    if split_path.exists():
        fixed = orjson.loads(split_path.read_bytes())
        if fixed["split_episode"] != computed:
            raise ValueError(f"{split_path} says S={fixed['split_episode']}, data say {computed}")
    else:
        fixed = {
            "split_episode": computed,
            "rule": "median onset of the constrained arm, rounded down",
            "onsets": [t["onset"] for t in trajectories["constrained"]],
            "fixed_at": datetime.now(UTC).isoformat(),
        }
        split_path.write_bytes(orjson.dumps(fixed, option=orjson.OPT_INDENT_2) + b"\n")
    split = fixed["split_episode"]

    report = {
        "prereg": next(iter(preregs.values())),
        "arms": {role: {"arm": arms[role].arm, **arm_report(*loaded[role])} for role in arms},
        "hypotheses": {
            "P1 peek: constrained > unconstrained": between_arms(
                trajectories["constrained"], trajectories["unconstrained"], "peek"
            ),
            "P2 cap: constrained > unconstrained": between_arms(
                trajectories["constrained"], trajectories["unconstrained"], "cap"
            ),
            "P2' cap: constrained > nomem": between_arms(
                trajectories["constrained"], trajectories["nomem"], "cap"
            ),
            "P3 cap, constrained: after onset > before": p3(trajectories["constrained"]),
        },
        "p3_control": {
            "split": fixed,
            "unconstrained": p3_control(trajectories["unconstrained"], split) if split else None,
            "nomem": p3_control(trajectories["nomem"], split) if split else None,
        },
    }
    out = RUNS_DIR / REPORT_FILE.replace(".json", f"{suffix}.json")
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2) + b"\n")
    report["path"] = str(out)
    return report
