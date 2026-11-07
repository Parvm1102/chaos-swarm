"""Hand validation of the judges: a blind sample to label, and the score once it is labelled.

sample_items draws `n` items (default 30): half coordination items (channel messages), half
relaxation items (notes). Within each kind, up to half are the primary judge's positives
("agreement"/"proposal"; "relaxed"), because positives are rare in some arms; the rest are its
other labels. Inside each stratum items are taken round-robin across arms, so every arm appears.
Seeded and deterministic.

write_sample writes two files:
- the sheet (CSV) for the human: item (V01..), kind ("messages" or "note"), the allowed labels,
  the judged text exactly as the judge read it, and empty columns your_label, your_evidence and
  comment. It hides the arm, the agent, the episode and every judge label.
- the key (JSON), kept apart: what each item is, and each judge's label.
plus the labelling instructions (the judges' own definitions, so humans and judges use one
rubric).

score_sheet joins a labelled sheet to the key: for each judge, agreement with the human (Wilson
95% interval), Cohen's kappa and per-class agreement; with a second labelled sheet, the agreement
between the two humans.
"""

import csv
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

import orjson

from swarm.analysis.judges import CLAIMS, KINDS, LABELS, PROMPTS, Item
from swarm.analysis.pathways import agreement
from swarm.calibrate import wilson
from swarm.prompts import load_prompt

SHEET_FILE = "step5_validation_sheet.csv"
KEY_FILE = "step5_validation_key.json"
INSTRUCTIONS_FILE = "step5_validation_instructions.md"
SHEET_COLUMNS = ("item", "kind", "allowed_labels", "text", "your_label", "your_evidence", "comment")
KIND_NAMES = {"coordination": "messages", "relaxation": "note"}
POSITIVE = {"coordination": CLAIMS["coordination"], "relaxation": frozenset({"relaxed"})}


def sample_items(
    items: list[Item],
    primary: dict[str, dict[str, Any]],
    *,
    n: int = 30,
    seed: int = 0,
) -> list[Item]:
    """Draw the validation sample (module docstring). Only items the primary judge resolved."""
    rng = random.Random(seed)
    by_id = {item.item_id: item for item in items}
    picked: list[Item] = []
    for index, kind in enumerate(KINDS):
        quota = n // len(KINDS) + (1 if index < n % len(KINDS) else 0)
        resolved = [
            by_id[item_id]
            for item_id, line in sorted(primary.items())
            if line["kind"] == kind and line["status"] == "ok" and item_id in by_id
        ]
        positives = [i for i in resolved if primary[i.item_id]["label"] in POSITIVE[kind]]
        others = [i for i in resolved if primary[i.item_id]["label"] not in POSITIVE[kind]]
        take_positive = min(len(positives), quota // 2)
        chosen = _round_robin(positives, take_positive, rng)
        chosen += _round_robin(others, quota - len(chosen), rng)
        if len(chosen) < quota:  # too few others: fill from the remaining positives
            rest = [i for i in positives if i not in chosen]
            chosen += _round_robin(rest, quota - len(chosen), rng)
        picked.extend(chosen)
    rng.shuffle(picked)
    return picked


def _round_robin(pool: list[Item], k: int, rng: random.Random) -> list[Item]:
    """k items from the pool, taking arms in turn (each arm's items shuffled)."""
    by_arm: dict[str, list[Item]] = defaultdict(list)
    for item in pool:
        by_arm[item.arm].append(item)
    queues = []
    for arm in sorted(by_arm):
        queue = list(by_arm[arm])
        rng.shuffle(queue)
        queues.append(queue)
    rng.shuffle(queues)
    out: list[Item] = []
    while len(out) < k and any(queues):
        for queue in queues:
            if queue and len(out) < k:
                out.append(queue.pop())
    return out


def write_sample(
    picked: list[Item], judges: dict[str, dict[str, dict[str, Any]]], out_dir: Path
) -> dict[str, Path]:
    """Write the blind sheet, the key and the instructions; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet, key_path, instructions = (
        out_dir / SHEET_FILE,
        out_dir / KEY_FILE,
        out_dir / INSTRUCTIONS_FILE,
    )
    key: dict[str, Any] = {}
    with sheet.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        for number, item in enumerate(picked, start=1):
            name = f"V{number:02d}"
            writer.writerow(
                {
                    "item": name,
                    "kind": KIND_NAMES[item.kind],
                    "allowed_labels": " / ".join(LABELS[item.kind]),
                    "text": item.text,
                    "your_label": "",
                    "your_evidence": "",
                    "comment": "",
                }
            )
            key[name] = {
                "item_id": item.item_id,
                "kind": item.kind,
                "arm": item.arm,
                "sequence_id": item.sequence_id,
                "episode": item.episode,
                "agent": item.agent,
                "labels": {
                    judge: lines.get(item.item_id, {}).get("label")
                    for judge, lines in judges.items()
                },
            }
    key_path.write_bytes(orjson.dumps(key, option=orjson.OPT_INDENT_2) + b"\n")
    instructions.write_text(_instructions(), encoding="utf-8")
    return {"sheet": sheet, "key": key_path, "instructions": instructions}


def _instructions() -> str:
    parts = [
        "# Step 5 validation: how to label\n",
        "Open the sheet, read each row's `text`, and fill `your_label` with one of its "
        "`allowed_labels`. Copy the words that decided it into `your_evidence` (optional for "
        "`none` and `unclear`). Do not open the key file until you are done. Rows of kind "
        "`messages` use the first rubric, rows of kind `note` the second. These are the exact "
        "instructions the judge models received.\n",
    ]
    for kind in KINDS:
        parts.append(f"\n## Kind `{KIND_NAMES[kind]}`\n\n{load_prompt(PROMPTS[kind]).text}")
    return "".join(parts)


def read_sheet(path: Path) -> dict[str, str]:
    """item -> the human's label (blank rows are left out)."""
    with Path(path).open(newline="", encoding="utf-8") as f:
        return {
            row["item"]: row["your_label"].strip().lower()
            for row in csv.DictReader(f)
            if row["your_label"].strip()
        }


def score_sheet(sheet: Path, key_path: Path, second_sheet: Path | None = None) -> dict[str, Any]:
    """Each judge against the human, per kind; and human against human with a second sheet."""
    key = orjson.loads(Path(key_path).read_bytes())
    human = read_sheet(sheet)
    bad = {i: label for i, label in human.items() if label not in LABELS[key[i]["kind"]]}
    if bad:
        raise ValueError(f"labels outside the allowed set: {bad}")
    judges = sorted({judge for entry in key.values() for judge in entry["labels"]})
    report: dict[str, Any] = {"labelled": len(human), "of": len(key), "judges": {}}
    for judge in judges:
        report["judges"][judge] = {}
        for kind in KINDS:
            pairs = [
                (label, key[i]["labels"][judge])
                for i, label in sorted(human.items())
                if key[i]["kind"] == kind and key[i]["labels"].get(judge) is not None
            ]
            result = agreement(pairs, LABELS[kind])  # first = human, second = judge
            result["accuracy"] = wilson(sum(a == b for a, b in pairs), len(pairs))
            report["judges"][judge][kind] = result
    if second_sheet is not None:
        other = read_sheet(second_sheet)
        report["humans"] = {
            kind: agreement(
                [
                    (label, other[i])
                    for i, label in sorted(human.items())
                    if key[i]["kind"] == kind and i in other
                ],
                LABELS[kind],
            )
            for kind in KINDS
        }
    return report
