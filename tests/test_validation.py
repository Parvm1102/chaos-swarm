"""The blind validation sample: stratification, what the sheet hides, and the scoring."""

import csv

import pytest

from swarm.analysis.judges import Item
from swarm.analysis.validation import read_sheet, sample_items, score_sheet, write_sample


def population():
    """Two arms; coordination positives only in arm b; relaxed notes in both."""
    items, primary = [], {}
    for arm in ("a", "b"):
        for e in range(1, 21):
            cid = f"{arm}/s00/e{e:02d}/coordination"
            items.append(Item(cid, "coordination", arm, 0, e, None, f"Round 1, A: text {arm}{e}"))
            label = "proposal" if arm == "b" and e <= 6 else "none"
            primary[cid] = {"kind": "coordination", "status": "ok", "label": label}
            nid = f"{arm}/s00/e{e:02d}/relaxation/A"
            items.append(Item(nid, "relaxation", arm, 0, e, "A", f"note {arm}{e}"))
            primary[nid] = {
                "kind": "relaxation",
                "status": "ok" if e != 20 else "unresolved",
                "label": ("relaxed" if e % 2 else "strict") if e != 20 else None,
            }
    return items, primary


def test_the_sample_is_stratified_and_deterministic():
    items, primary = population()
    picked = sample_items(items, primary, n=30, seed=0)
    assert picked == sample_items(items, primary, n=30, seed=0)
    assert len(picked) == 30 and len({i.item_id for i in picked}) == 30
    kinds = [i.kind for i in picked]
    assert kinds.count("coordination") == 15 and kinds.count("relaxation") == 15
    coordination_positives = [
        i for i in picked if i.kind == "coordination" and primary[i.item_id]["label"] == "proposal"
    ]
    assert len(coordination_positives) == 6  # all six available (quota//2 = 7)
    relaxed = [i for i in picked if primary[i.item_id]["label"] == "relaxed"]
    assert len(relaxed) == 7
    assert {i.arm for i in picked if i.kind == "relaxation"} == {"a", "b"}
    assert all(primary[i.item_id]["status"] == "ok" for i in picked)


def test_the_sheet_hides_the_arm_the_episode_and_the_labels(tmp_path):
    items, primary = population()
    picked = sample_items(items, primary, n=10, seed=1)
    second = {k: {**v, "label": "none" if v["kind"] == "coordination" else "unclear"}
              for k, v in primary.items()}  # fmt: skip
    paths = write_sample(picked, {"sonnet": primary, "haiku": second}, tmp_path)
    with paths["sheet"].open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["item"] for r in rows] == [f"V{n:02d}" for n in range(1, 11)]
    assert set(rows[0]) == {
        "item",
        "kind",
        "allowed_labels",
        "text",
        "your_label",
        "your_evidence",
        "comment",
    }
    sheet_text = paths["sheet"].read_text()
    assert (
        "s00/" not in sheet_text
        and "sonnet" not in sheet_text
        and "proposal" not in "".join(r["text"] for r in rows)
    )
    assert "judge_coordination" not in paths["instructions"].read_text()
    assert "You label short message exchanges" in paths["instructions"].read_text()


def test_scoring_a_labelled_sheet(tmp_path):
    items, primary = population()
    picked = sample_items(items, primary, n=10, seed=2)
    paths = write_sample(picked, {"sonnet": primary}, tmp_path)
    with paths["sheet"].open(newline="") as f:
        rows = list(csv.DictReader(f))
    import orjson

    key = orjson.loads(paths["key"].read_bytes())
    for row in rows:  # the human agrees with the judge everywhere except V01
        row["your_label"] = key[row["item"]]["labels"]["sonnet"]
    rows[0]["your_label"] = "none" if key["V01"]["kind"] == "coordination" else "unclear"
    labelled = tmp_path / "labelled.csv"
    with labelled.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert len(read_sheet(labelled)) == 10
    report = score_sheet(labelled, paths["key"], second_sheet=labelled)
    correct = sum(r["accuracy"]["k"] for r in report["judges"]["sonnet"].values())
    assert report["labelled"] == 10 and correct == 9
    assert all(r["agreement"] == 1.0 for r in report["humans"].values() if r["n"])
    rows[1]["your_label"] = "banana"
    with labelled.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="outside the allowed set"):
        score_sheet(labelled, paths["key"])
