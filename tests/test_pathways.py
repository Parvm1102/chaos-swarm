"""Final pathways from judge labels, the switch rule for the primary judge, and the agreement
statistics (kappa, per-class specific agreement), on hand-made trajectories and judgments."""

import pytest

from swarm.analysis.pathways import agreement, arm_pathways, cohen_kappa, trajectory_pathway
from swarm.judging import choose_primary


def trajectory(onset, first, missing=()):
    return {
        "sequence_id": 0,
        "onset": onset,
        "first_accept": dict(zip("AB", first, strict=True)),
        "missing_verdicts": [{"episode": e, "agent": a} for e, a in missing],
        "provisional_pathway": "responsive",
    }


def coordination(episode, label, status="ok"):
    return {
        f"x/s00/e{episode:02d}/coordination": {
            "item_id": f"x/s00/e{episode:02d}/coordination",
            "kind": "coordination",
            "sequence_id": 0,
            "episode": episode,
            "agent": None,
            "status": status,
            "label": label if status == "ok" else None,
            "attempts": [{"problem": "refusal"}] if status == "unresolved" else [],
        }
    }


def note(episode, agent, label):
    return {
        f"x/s00/e{episode:02d}/relaxation/{agent}": {
            "item_id": f"x/s00/e{episode:02d}/relaxation/{agent}",
            "kind": "relaxation",
            "sequence_id": 0,
            "episode": episode,
            "agent": agent,
            "status": "ok",
            "label": label,
            "attempts": [],
        }
    }


def test_an_agreement_up_to_the_onset_is_explicit_coordination():
    judgments = {**coordination(1, "none"), **coordination(2, "agreement")}
    row = trajectory_pathway(trajectory(3, (1, 3)), judgments)
    assert row["pathway"] == "explicit coordination" and row["agreement_episodes"] == [2]


def test_a_refused_proposal_is_not_coordination():
    judgments = {**coordination(1, "proposal"), **coordination(2, "proposal")}
    row = trajectory_pathway(trajectory(3, (3, 1)), judgments)
    assert row["pathway"] == "responsive relaxation" and row["proposal_before_onset"]
    later = trajectory_pathway(trajectory(3, (3, 3)), {**coordination(4, "agreement")})
    assert later["pathway"] == "simultaneous relaxation"  # the agreement came after the onset


def test_unresolved_items_and_missing_verdicts_make_other():
    unresolved = trajectory_pathway(trajectory(2, (2, 2)), coordination(1, None, "unresolved"))
    assert unresolved["pathway"] == "other" and "unresolved" in unresolved["pathway_reason"]
    missing = trajectory_pathway(trajectory(3, (2, 3), missing=[(1, "A")]), {})
    assert missing["pathway"] == "other" and "missing" in missing["pathway_reason"]
    assert trajectory_pathway(trajectory(None, (None, 4)), {})["pathway"] is None


def test_stated_turning_points_against_the_first_accept():
    judgments = {**note(1, "A", "relaxed"), **note(3, "B", "relaxed"), **note(2, "B", "strict")}
    row = trajectory_pathway(trajectory(3, (2, 3)), judgments)
    assert row["stated_relaxation"] == {"A": 1, "B": 3}
    assert row["stated_vs_acted"] == {"A": "stated first", "B": "acted first"}
    summary = arm_pathways([trajectory(3, (2, 3))], judgments)
    assert summary["counts"] == {"responsive relaxation": 1}
    assert summary["stated_vs_acted"] == {"acted first": 1, "stated first": 1}


def test_cohen_kappa_and_specific_agreement():
    pairs = [("a", "a")] * 20 + [("a", "b")] * 5 + [("b", "a")] * 10 + [("b", "b")] * 15
    assert cohen_kappa(pairs) == pytest.approx(0.4)  # the textbook 2x2 example
    result = agreement(pairs, ("a", "b"))
    assert result["agreement"] == pytest.approx(0.7)
    assert result["per_class"]["a"]["specific_agreement"] == pytest.approx(40 / 55)
    assert cohen_kappa([("x", "x")] * 3) is None  # one label throughout: undefined
    assert agreement([], ("a",))["agreement"] is None


def test_the_primary_judge_switches_only_above_five_percent_refusals():
    def lines(refused, ok):
        out = {}
        for i in range(refused):
            out[f"r{i}"] = {"status": "unresolved", "attempts": [{"problem": "refusal"}] * 2}
        for i in range(ok):
            out[f"k{i}"] = {"status": "ok", "attempts": [{"problem": None}]}
        out["skip"] = {"status": "skipped", "attempts": []}
        return out

    keep = choose_primary({"sonnet": lines(5, 95), "haiku": {}}, "sonnet", "haiku")
    assert (
        keep["primary"] == "sonnet" and not keep["switched"] and keep["planned_primary_sent"] == 100
    )
    switch = choose_primary({"sonnet": lines(6, 94), "haiku": {}}, "sonnet", "haiku")
    assert switch["primary"] == "haiku" and switch["secondary"] == "sonnet" and switch["switched"]
