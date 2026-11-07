"""Experiment 3 (open door) orchestration: `swarm opendoor` (offline).

run_opendoor reads the three arms' summaries, trajectories and episode records, checks that every
arm has finished (all sequences and episodes, not aborted by the refusal breaker) and that all
three ran under the same pre-registration as PREREG.md on disk, then computes the pre-registered
hypotheses (swarm.analysis.opendoor). It writes runs/e3_opendoor.json and the appendix of every
report text, runs/e3_report_texts.md.
"""

from pathlib import Path
from typing import Any

import orjson

from swarm.agents.trajectory import load_records
from swarm.analysis import opendoor
from swarm.arms import ArmConfig
from swarm.config import RUNS_DIR
from swarm.prereg import matches
from swarm.trajectories import SUMMARY_FILE, TRAJECTORIES_FILE

REPORT_FILE = "e3_opendoor.json"
APPENDIX_FILE = "e3_report_texts.md"


def _load(arm: ArmConfig, dry_run: bool) -> dict[str, Any]:
    run_dir = arm.run_dir(dry_run=dry_run)
    summary = orjson.loads((run_dir / SUMMARY_FILE).read_bytes())
    lines = (run_dir / TRAJECTORIES_FILE).read_bytes().splitlines()
    trajectories = [orjson.loads(line) for line in lines if line.strip()]
    if summary.get("aborted_by_breaker"):
        raise ValueError(f"{arm.arm} was aborted by the refusal breaker; resume it first")
    short = [t["sequence_id"] for t in trajectories if t["episodes"] != arm.episodes]
    if len(trajectories) != len(arm.sequences) or short:
        raise ValueError(f"{arm.arm} has not finished ({len(trajectories)} trajectories, {short})")
    records = [load_records(run_dir / "trajectories" / f"s{s:02d}") for s in arm.sequences]
    return {"summary": summary, "trajectories": trajectories, "records": records}


def run_opendoor(
    control: ArmConfig, fix: ArmConfig, ack: ArmConfig, *, dry_run: bool
) -> dict[str, Any]:
    arms = {"control": control, "fix": fix, "ack": ack}
    if (fix.operator_policy, ack.operator_policy, control.operator_policy) != ("fix", "ack", None):
        raise ValueError("expected arms with operator policies fix, ack and none (control)")
    data = {role: _load(arm, dry_run) for role, arm in arms.items()}
    preregs = {role: d["summary"]["provenance"].get("prereg") for role, d in data.items()}
    hashes = {p["sha256"] if p else None for p in preregs.values()}
    if len(hashes) != 1 or None in hashes:
        raise ValueError(f"the arms ran under different (or no) pre-registrations: {preregs}")
    if control.prereg is None or not matches(Path(control.prereg), next(iter(hashes))):
        raise ValueError(f"{control.prereg} on disk differs from the one the arms ran under")
    t = {role: d["trajectories"] for role, d in data.items()}
    texts = {role: opendoor.report_texts(arms[role].arm, d["records"]) for role, d in data.items()}

    report = {
        "prereg": next(iter(preregs.values())),
        "arms": {
            role: {
                "arm": arms[role].arm,
                "operator_policy": arms[role].operator_policy,
                "reports": opendoor.report_summary(t[role], texts[role]),
                "collusion": {
                    key: data[role]["summary"][key]
                    for key in ("criterion", "tc", "cc", "ec_pooled", "onset")
                },
                "task_accuracy": data[role]["summary"]["task_accuracy"],
                "refusals": data[role]["summary"]["refusals"],
                "aborted_by_breaker": data[role]["summary"].get("aborted_by_breaker"),
                "cost": data[role]["summary"]["cost"],
            }
            for role in arms
        },
        "hypotheses": {
            "P1 fewer than 50% ever report: ack": opendoor.p1(t["ack"]),
            "P1 fewer than 50% ever report: fix": opendoor.p1(t["fix"]),
            "P2 report rate lower after onset: ack": opendoor.p2(t["ack"]),
            "P2 report rate lower after onset: fix": opendoor.p2(t["fix"]),
            "P3 fix after the raise": opendoor.p3(t["fix"], t["control"]),
            "P4 ack vs control": opendoor.p4(t["ack"], t["control"]),
        },
        "fix_detail": opendoor.fix_detail(t["fix"]),
    }
    suffix = "_dry" if dry_run else ""
    out = RUNS_DIR / REPORT_FILE.replace(".json", f"{suffix}.json")
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2) + b"\n")
    appendix = RUNS_DIR / APPENDIX_FILE.replace(".md", f"{suffix}.md")
    every = [text for role in ("control", "ack", "fix") for text in texts[role]]
    appendix.write_text(opendoor.appendix_markdown(every), encoding="utf-8")
    report["path"], report["appendix"] = str(out), str(appendix)
    return report
