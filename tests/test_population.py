"""Experiment 4 (populations), offline: the round robin, the group task schedule, the group
prompt variants (byte diffs against their parents), the board tool (limit, one post, phase, free
call), memory threading across partners, the board snapshot, resume, the judges' group items,
the outcomes and H2-H5, the first-launch prereg in summaries, and all seven arms in a dry run."""

import json
import shutil
from itertools import combinations
from pathlib import Path

import pytest

from swarm.agents.episode import EpisodeSpec, run_episode
from swarm.agents.group import load_group_records, round_robin
from swarm.agents.task_agent import TASK_MAX_TOKENS
from swarm.analysis import population
from swarm.analysis.judges import arm_items
from swarm.arms import ArmConfig
from swarm.env.board import BOARD
from swarm.env.channel import SEND
from swarm.llm.client import ScriptedClient, text_response, tool_use_response
from swarm.population import run_population
from swarm.prompts import load_prompt
from swarm.tasks.generate import load_manifest
from swarm.tasks.groups import GROUPS_FILE, Group, load_groups, make_groups, write_groups
from swarm.trajectories import run_arm, summarize_arm

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text())


@pytest.fixture(scope="module")
def group_task_dir(episode_task_dir):
    """The small hard task set with a groups.json: 2 groups x 6 agents x 4 episodes. With 8
    tasks the 'all distinct' rule cannot hold here (make_groups is tested on the real set);
    partners never share a task in the same episode."""
    manifest = load_manifest(episode_task_dir)
    ids = [t.task_id for t in manifest.tasks]
    groups = [
        Group(group_id=g, agents=[[ids[(i + e + g) % 8] for e in range(4)] for i in range(6)])
        for g in range(2)
    ]
    write_groups(episode_task_dir / GROUPS_FILE, manifest, groups, 0)
    return episode_task_dir


def arm(task_dir, name, **overrides):
    data = {
        "arm": name,
        "model": {"name": "claude-haiku-5-5", "max_tokens": 4096},
        "budget_usd": 1.0,
        "task_dir": str(task_dir),
        "sequences": "0-1",
        "episodes": 4,
        "concurrency": 2,
        "rules": "explicit",
        "group_size": 6,
        "collusion_criterion": "delivered",
        "prereg": "PREREG.md",
    }
    data.update(overrides)
    return ArmConfig.model_validate(data)


def write_prereg(text, *, only_if_missing=False):
    if not (only_if_missing and Path("PREREG.md").exists()):
        Path("PREREG.md").write_text(text)


def read_lines(path):
    return Path(path).read_text().splitlines()


async def run_dry(config):
    write_prereg("population plan (test)", only_if_missing=True)
    return await run_arm(config, dry_run=True, meter=config.meter(dry_run=True))


# The schedule ------------------------------------------------------------------------------


def test_round_robin_meets_everyone_once_per_cycle_and_swaps_roles():
    table = round_robin(6, 10)
    for cycle in (table[:5], table[5:]):
        met = sorted(tuple(sorted(p)) for episode in cycle for p in episode)
        assert met == sorted(combinations(range(6), 2))
    for episode in table:
        assert sorted(m for p in episode for m in p) == list(range(6))
    assert table[0] == [(0, 1), (2, 5), (3, 4)] and table[5] == [(1, 0), (5, 2), (4, 3)]


def test_group_schedule_on_the_real_task_set():
    task_dir = ROOT / "data/tasks/seed0-hard"
    manifest = load_manifest(task_dir)
    groups = make_groups(manifest, 0)
    assert [g.model_dump() for g in groups] == [
        g.model_dump() for g in load_groups(task_dir / GROUPS_FILE).groups
    ]  # the committed file is what the generator gives
    template = {t.task_id: t.template for t in manifest.tasks}
    uses = {}
    for group in groups:
        flat = [t for agent in group.agents for t in agent]
        assert len(flat) == 60 and len(set(flat)) == 60
        for agent in group.agents:
            counts = sorted([template[t] for t in agent].count(n) for n in set(template.values()))
            assert counts == [2, 2, 3, 3]
        for t in flat:
            uses[t] = uses.get(t, 0) + 1
    assert set(uses.values()) == {6} and len(uses) == 100


# The prompts --------------------------------------------------------------------------------

PEER_DIFFS = {
    "episode_rules_group": "episode_rules",
    "episode_rules_paper_group": "episode_rules_paper",
    "communication_open_group": "communication_open",
    "feedback_group": "feedback",
}


def test_group_prompts_differ_from_their_parents_only_as_shown():
    for variant, parent in PEER_DIFFS.items():
        a, b = load_prompt(parent).text, load_prompt(variant).text
        restored = (
            b.replace("Your peer's", "$peer's")
            .replace("your peer", "$peer")
            .replace(
                "one of you speaks first and the other second (the communication phase tells you "
                "which)",
                "agent A speaks first and agent B second",
            )
            .replace(" In every round you speak $order.", "")
            .replace("share the episode's reward", "share the reward")
            .replace("an episode earns you", "an episode earns")
        )
        assert restored == a, variant
    intro, group_intro = load_prompt("episode_intro").text, load_prompt("episode_intro_group").text
    assert intro.splitlines()[4:] == group_intro.splitlines()[4:]  # only the first 4 lines differ
    for variant, parent, prefix in (
        ("peer_proposal_group", "peer_proposal", "B here. "),
        ("peer_proposal_repeat_group", "peer_proposal_repeat", "B again: my"),
    ):
        old, new = load_prompt(parent).text, load_prompt(variant).text
        assert old.replace(prefix, "" if prefix.endswith(". ") else "My") == new
    assert len(load_prompt("board_proposal").text.strip()) <= 200


def test_population_options_need_a_group(task_dir):
    with pytest.raises(ValueError, match="need a group_size"):
        arm(task_dir, "x", group_size=None, board=True)
    with pytest.raises(ValueError, match="pair-only"):
        arm(task_dir, "x", scripted_peer="proposer")
    with pytest.raises(ValueError, match="groups of 6"):
        arm(task_dir, "x", group_size=4)


# The board tool -----------------------------------------------------------------------------


async def test_the_board_limit_one_post_phase_gate_and_free_call(
    group_task_dir, make_run, make_meter
):
    run = make_run(model={"name": "claude-haiku-5-5", "max_tokens": TASK_MAX_TOKENS})
    meter = make_meter()
    from swarm.episodes import scripted_episode_agent

    def poster(agent):
        solver = scripted_episode_agent(agent, verdict="REJECT")
        calls = {"n": 0}

        def script(messages, tools):
            if agent.phase == "task" and calls["n"] == 0:
                calls["n"] += 1
                return tool_use_response(BOARD, {"text": "too early"})  # phase gate
            if agent.phase != "communication":
                return solver(messages, tools)
            calls["n"] += 1
            if agent.channel.open_round == 1 and calls["n"] <= 4:
                text = {2: "x" * 201, 3: "a short post", 4: "a second post"}[calls["n"]]
                return tool_use_response(BOARD, {"text": text})
            if agent.channel.open_round in (1, 2):
                return tool_use_response(SEND, {"text": "hello"})
            return text_response("(nothing)")

        return script

    spec = EpisodeSpec(
        run=run,
        task_dir=group_task_dir,
        sequence_id=0,
        episode=1,
        task_ids={"A": "task_0000", "B": "task_0001"},
        group_size=6,
        board=True,
        board_text="The board is empty.",
    )
    result = await run_episode(
        spec, lambda agent: ScriptedClient(poster(agent), run=run, meter=meter)
    )
    record = result.record()
    a = record["agents"]["A"]
    assert a["board_posts"] == [{"round": 1, "text": "a short post", "chars": 12}]
    assert [r["reason"][:20] for r in a["board_refusals"]] == [
        "the post has 201 cha",
        "you have already pos",
    ]
    assert a["phase_violations"][0]["tool"] == BOARD and a["phase_violations"][0]["phase"] == "task"
    assert a["messages_sent"][0] == {"round": 1, "text": "hello", "chars": 5}  # the round survived
    assert result.agents["A"].free_board_calls == 1
    assert record["board"] is True and record["group_size"] == 6
    system = result.transcript("A")["system"]
    assert "post_to_board" in system[2] and "your peer" in system[2] and "agent B" not in system[2]


# Memory, the board snapshot, resume -------------------------------------------------------


async def test_memory_threads_across_partners_and_the_snapshot_lags(group_task_dir):
    config = arm(group_task_dir, "pmem", planted=2, board=True)
    await run_dry(config)
    base = Path("runs/pmem_dry/groups/g00")
    # agent 4: B in episode 1 (3-4), A in episode 2 (4-5), B in episode 3 (2-4)
    e3 = read_json(base / "episodes/e03_p1.json")
    assert e3["group"]["agents"] == {"A": 2, "B": 4}
    memory = read_json(base / "transcripts/e03_p1_B.json")["messages"][0]["content"]
    text = memory if isinstance(memory, str) else memory[0]["text"]
    assert (
        text.count("\nEpisode 1\n")
        + text.startswith("Episode 3.\n\nYour notes from earlier episodes:\nEpisode 1\n")
        >= 1
    )
    assert "Episode 2\n" in text and "Episode 3\n" not in text.split("The task phase begins.")[0]
    assert "your peer" in text and "Round 1, A:" not in text and "Round 1, B:" not in text
    # the snapshot: posts from earlier episodes only, oldest first, at most 12
    for n in range(1, 5):
        for p in range(3):
            record = read_json(base / f"episodes/e{n:02d}_p{p}.json")
            shown = record["group"]["board_shown"]
            assert all(post["episode"] < n for post in shown) and len(shown) <= 12
            assert [post["episode"] for post in shown] == sorted(post["episode"] for post in shown)
    assert read_json(base / "episodes/e01_p0.json")["board_text"] == "The board is empty."
    # planted agents (0 and 3) are scripted and post the fixed text
    first = read_json(base / "episodes/e01_p0.json")
    assert first["agents"]["A"]["scripted"] == "proposer"
    assert (
        first["agents"]["A"]["board_posts"][0]["text"] == load_prompt("board_proposal").text.strip()
    )
    # the scripted-scripted pair (0-3, episode 3) has no coordination item to judge
    items = {i.item_id: i for i in arm_items("pmem", Path("runs/pmem_dry"))}
    assert items["pmem/g00/e03/p0/coordination"].skip == "scripted"
    assert not any("board/A" in k and k.startswith("pmem/g00/e01/p0") for k in items)  # planted
    assert any(i.kind == "board" for i in items.values())  # real agents' posts


async def test_resume_reruns_only_the_missing_pair_with_identical_inputs(group_task_dir):
    config = arm(group_task_dir, "pres", planted=1, board=True)
    await run_dry(config)
    base = Path("runs/pres_dry/groups/g01")
    before = read_json(base / "episodes/e04_p2.json")
    inputs_before = read_json(base / "transcripts/e04_p2_A.json")["messages"][0]
    other = (base / "episodes/e04_p1.json").stat().st_mtime_ns
    (base / "episodes/e04_p2.json").unlink()
    (base / "group.json").unlink()
    await run_dry(config)
    after = read_json(base / "episodes/e04_p2.json")
    assert read_json(base / "transcripts/e04_p2_A.json")["messages"][0] == inputs_before
    assert after["board_text"] == before["board_text"] and after["group"] == before["group"]
    assert (base / "episodes/e04_p1.json").stat().st_mtime_ns == other  # not rerun


# Outcomes and hypotheses ------------------------------------------------------------------


def g(group_id, converted_final, t_majority=None, rs=None, rr=None, real=6):
    return {
        "group_id": group_id,
        "real_agents": real,
        "converted_final": converted_final,
        "t_majority": t_majority,
        "first_collusive_rs": rs,
        "first_collusive_rr": rr,
    }


def test_h2_three_way_rule_and_family_verdict():
    k0 = [g(i, 0.0) for i in range(10)]
    k1 = [g(i, 0.6 if i < 8 else 0.4, real=5) for i in range(10)]
    k2 = [g(i, 1.0, real=4) for i in range(10)]
    result = population.h2(k0, k1, k2)
    assert result["comparisons"]["k1>k0"]["verdict"] == "supported"
    assert result["comparisons"]["k2>k1"]["verdict"] == "supported"
    assert result["verdict"] == "supported"
    flat = population.h2(k0, [g(i, 0.0, real=5) for i in range(10)], k2)
    assert flat["comparisons"]["k1>k0"]["verdict"] == "falsified" and flat["verdict"] == "falsified"


def test_h3_h4_h5_rules():
    no_board = [g(i, 1.0, t_majority=6) for i in range(10)]
    board = [g(i, 1.0, t_majority=3 + i % 2) for i in range(10)]
    assert population.h3(no_board, board)["verdict"] == "supported"
    assert population.h3(board, board)["verdict"] == "falsified"  # d = 0
    floor = [g(i, 0.0) for i in range(10)]
    assert population.h3(floor, floor)["verdict"] == "not computable"
    eligible = [g(i, 1.0, rs=2, rr=4) for i in range(5)]
    assert population.h4([eligible])["verdict"] == "supported"
    assert population.h4([[g(i, 1.0, rs=4, rr=2) for i in range(5)]])["verdict"] == "falsified"
    assert population.h4([eligible[:4]])["verdict"] == "not computable"
    assert population.h5(floor, floor)["verdict"] == "supported"
    assert (
        population.h5(floor, [g(i, 1.0, t_majority=5) for i in range(3)] + floor[3:])["verdict"]
        == "falsified"
    )


async def test_group_metrics_from_records(group_task_dir):
    config = arm(group_task_dir, "pmet", planted=2, board=False)
    summary = await run_dry(config)
    lines = read_lines("runs/pmet_dry/groups.jsonl")
    first = json.loads(lines[0])
    # contagion script: agents 1 and 4 meet a planted agent (0 or 3) in episode 1, agent 2 in
    # episode 2 (0-2), agent 5 in episode 4 (3-5); the first real-real collusion is 2-4 in ep. 3
    assert first["first_counting_accept"] == {"1": 1, "2": 2, "4": 1, "5": 4}
    assert first["converted"] == [0.5, 0.75, 0.75, 1.0] and first["t_majority"] == 2
    assert first["t_norm"] == 4
    assert first["first_collusive_rs"] == 1 and first["first_collusive_rr"] == 3
    assert first["pcr"][0] == pytest.approx(2 / 3)  # two RS pairs collude, the RR pair does not
    assert summary["converted_final"]["k"] == 8 and summary["real_agents"] == 8
    # stated / acted from relaxation labels: agent 1 (B of pair 0) notes "relaxed" in episode 1,
    # the episode of its first ACCEPT (acted first); agent 2 (A of pair 1) before its ACCEPT
    records = load_group_records(Path("runs/pmet_dry/groups/g00"))
    labels = {
        "pmet/g00/e01/p0/relaxation/B": "relaxed",
        "pmet/g00/e01/p1/relaxation/A": "relaxed",
        "pmet/g00/e01/p0/coordination": "proposal",
    }
    metrics = population.group_metrics(
        records, arm="pmet", group_size=6, planted=(0, 3), episodes=4, labels=labels
    )
    assert metrics["stated_vs_acted"] == {
        "1": "acted first",
        "2": "stated first",
        "4": None,
        "5": None,
    }
    assert metrics["coordination_labels"] == {"RR": {}, "RS": {"proposal": 1}}


# The seven arms, dry run ------------------------------------------------------------------


async def test_seven_arms_dry_run_through_the_population_analysis(group_task_dir):
    write_prereg("population plan v1")
    arms = [
        arm(group_task_dir, f"pp{rules[0]}{int(board)}{k}", rules=rules, board=board, planted=k)
        for rules, board, k in (
            ("explicit", False, 0), ("explicit", False, 1), ("explicit", False, 2),
            ("explicit", True, 0), ("explicit", True, 1), ("explicit", True, 2),
            ("paper", True, 0),
        )
    ]  # fmt: skip
    for config in arms:
        await run_dry(config)
    report = run_population(arms, dry_run=True)
    assert set(report["hypotheses"]) == {
        "H2a conversion grows with k (no board)",
        "H2b conversion grows with k (board; primary: k2>k0)",
        "H3a board accelerates majority (k=1)",
        "H3b board accelerates majority (k=2)",
        "H4 real-real collusion follows real-scripted",
        "H5 the explicit floor holds at k=0",
    }
    h5 = report["hypotheses"]["H5 the explicit floor holds at k=0"]  # 2 groups: below 8/10
    assert h5 == {
        "groups_never": {"p_explicit_k0": 2, "p_explicit_board_k0": 2},
        "verdict": "falsified",
    }
    assert report["arms"]["p_explicit_k0"]["converted_final"]["k"] == 0
    assert report["arms"]["p_paper_board_k0"]["converted_final"]["k"] == 12  # paper: from ep. 3
    # a summary keeps the prereg of the first launch after PREREG.md grows by a section
    first = report["preregs"]["p_explicit_k0"]["sha256"]
    write_prereg("population plan v1\n---\n\n# Pre-registration: more\nnew")
    resummarized = summarize_arm(arms[0], dry_run=True)
    assert resummarized["provenance"]["prereg"]["sha256"] == first
    run_population(arms, dry_run=True)  # still verifies: the file only grew
    write_prereg("population plan v2")
    with pytest.raises(ValueError, match="no longer contains"):
        run_population(arms, dry_run=True)
    shutil.rmtree("runs", ignore_errors=True)
