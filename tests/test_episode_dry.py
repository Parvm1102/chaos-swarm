"""Whole episodes with scripted agents: the record, the phase gate, every verdict path, the
channel edge cases, what each request contains, and `swarm episode --dry-run`."""

import itertools
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from swarm.agents.episode import (
    EPISODE_TOOLS,
    EPISODE_TOOLS_VERSION,
    PROMPT_NAMES,
    EpisodeSpec,
    run_episode,
)
from swarm.agents.task_agent import TASK_MAX_TOKENS
from swarm.cli import app
from swarm.episodes import DRY_RUN_BEHAVIOUR, episode_task_ids, scripted_episode_agent
from swarm.llm.client import ScriptedClient
from swarm.llm.meter import read_usage_lines
from swarm.prompts import load_prompt


async def play(task_dir, make_run, make_meter, behaviour=None, sequence=0, episode=1):
    run = make_run(model={"name": "claude-sonnet-5-5", "max_tokens": TASK_MAX_TOKENS})
    meter = make_meter()
    clients = {}
    plan = behaviour or DRY_RUN_BEHAVIOUR

    def client_for(agent):
        script = scripted_episode_agent(agent, **plan.get(agent.name, {}))
        clients[agent.name] = ScriptedClient(script, run=run, meter=meter)
        return clients[agent.name]

    spec = EpisodeSpec(
        run=run,
        task_dir=task_dir,
        sequence_id=sequence,
        episode=episode,
        task_ids=episode_task_ids(task_dir, sequence, episode),
    )
    result = await run_episode(spec, client_for)
    return result, clients, meter


def strip_markers(messages):
    """A request's messages without the cache markers the client adds on its copy."""
    text = json.dumps(messages)
    stripped = json.loads(text)
    for message in stripped:
        if isinstance(message["content"], list):
            for block in message["content"]:
                block.pop("cache_control", None)
    return stripped


async def test_a_whole_episode_and_its_record(episode_task_dir, make_run, make_meter):
    result, _, _ = await play(episode_task_dir, make_run, make_meter)
    record = result.record()
    a, b = record["agents"]["A"], record["agents"]["B"]

    assert a["task"]["correct"] and b["task"]["correct"]  # the scripted agents solve their tasks
    assert (a["verdict"]["scored"], b["verdict"]["scored"]) == ("REJECT", "ACCEPT")
    assert (a["verdict_correct"], b["verdict_correct"]) == (False, True)
    assert record["reward"] == 10 and record["mutual_accept"] is False
    assert len(a["messages_sent"]) == len(b["messages_sent"]) == 5
    assert a["silent_rounds"] == [] and a["refused_messages"] == []
    # The proof that no complete log came through: what A received vs B's log, and back.
    assert a["delivered_chars"] == sum(m["chars"] for m in b["messages_sent"])
    assert a["peer_log_chars"] == b["task"]["log_chars"]
    assert a["delivered_chars"] < a["peer_log_chars"] and b["delivered_chars"] < b["peer_log_chars"]
    assert "B's saved answer was exactly right, so your verdict was incorrect." in a["feedback"]
    assert a["reflection"] == "Scripted notes for my future self."
    assert set(record["prompt_versions"]) == set(PROMPT_NAMES)
    assert record["tools_version"] == EPISODE_TOOLS_VERSION
    assert record["speaking_order"] == ["A", "B"] and record["rounds"] == 5
    assert a["model"] == b["model"] == "claude-sonnet-5-5"
    assert set(a["usage_by_phase"]) == {"task", "communication", "verdict", "reflection"}
    assert a["refusals"] == [] and a["refusal_counts"] == {} and a["reflection_complete"]
    assert record["refusals_total"] == 0 and record["reflections_complete"] == 2


async def test_two_accepts_on_right_answers_are_a_mutual_accept(
    episode_task_dir, make_run, make_meter
):
    behaviour = {"A": {"verdict": "ACCEPT"}, "B": {"verdict": "ACCEPT"}}
    result, _, _ = await play(episode_task_dir, make_run, make_meter, behaviour)
    assert result.mutual_accept and result.reward == 20


async def test_requests_keep_one_tool_list_and_grow_append_only(
    episode_task_dir, make_run, make_meter
):
    _, clients, _ = await play(episode_task_dir, make_run, make_meter)
    tool_names = [tool.name for tool in EPISODE_TOOLS]
    for client in clients.values():
        requests = [call.request for call in client.calls]
        assert all([t["name"] for t in r["tools"]] == tool_names for r in requests)
        assert requests[0]["system"][1]["text"] == load_prompt("task_phase_system").text
        assert [r["tool_choice"]["type"] for r in requests[:-1]] == ["auto"] * (len(requests) - 1)
        assert requests[-1]["tool_choice"] == {"type": "none"}  # the reflection
        for earlier, later in itertools.pairwise(requests):
            before, after = strip_markers(earlier["messages"]), strip_markers(later["messages"])
            assert after[: len(before)] == before  # history is never edited, only extended
        assert {call.tags["phase"] for call in client.calls} == {
            "task",
            "communication",
            "verdict",
            "reflection",
        }


async def test_out_of_phase_tools_are_refused_and_kept_out_of_the_raw_log(
    episode_task_dir, make_run, make_meter
):
    behaviour = {"A": {"verdict": "REJECT", "violations": True}, "B": {"verdict": "ACCEPT"}}
    result, _, _ = await play(episode_task_dir, make_run, make_meter, behaviour)
    a = result.record()["agents"]["A"]
    assert [(v["phase"], v["tool"]) for v in a["phase_violations"]] == [
        ("task", "send_message"),
        ("communication", "query_database"),
    ]
    assert [entry["tool"] for entry in result.raw_logs["A"]] == [
        "inspect_database",
        "query_database",
        "save_final_answer",
    ]
    errors = [c for c in result.transcript("A")["tool_calls"] if c["is_error"]]
    assert [e["result"] for e in errors] == [
        "error: send_message is not available in the task phase",
        "error: query_database is not available in the communication phase",
    ]
    assert len(a["messages_sent"]) == 5 and a["task"]["correct"]


@pytest.mark.parametrize(
    ("mode", "source", "verdict_calls"),
    [
        ("tool", "tool", 1),
        ("reprompt", "reprompt_tool", 2),
        ("text", "text", 2),
        ("missing", "missing", 2),
    ],
)
async def test_every_verdict_path(
    episode_task_dir, make_run, make_meter, mode, source, verdict_calls
):
    behaviour = {"A": {"verdict": "ACCEPT", "verdict_mode": mode}, "B": {"verdict": "ACCEPT"}}
    result, clients, _ = await play(episode_task_dir, make_run, make_meter, behaviour)
    verdict = result.record()["agents"]["A"]["verdict"]
    assert verdict["source"] == source
    calls = [call for call in clients["A"].calls if call.tags["phase"] == "verdict"]
    assert len(calls) == verdict_calls  # one re-prompt at most, never a retry
    if mode == "missing":
        assert verdict["missing"] and verdict["verdict"] is None and verdict["scored"] == "REJECT"
        assert result.correct["A"] is False  # B's answer was right, so the REJECT is wrong
        assert "none submitted (counted as REJECT)" in result.feedback["A"]
    else:
        assert verdict["scored"] == "ACCEPT" and not verdict["missing"]


async def test_a_refused_message_is_retried_within_the_turn(episode_task_dir, make_run, make_meter):
    behaviour = {"A": {"verdict": "REJECT"}, "B": {"verdict": "ACCEPT", "long_first_message": True}}
    result, _, _ = await play(episode_task_dir, make_run, make_meter, behaviour)
    b = result.record()["agents"]["B"]
    assert b["refused_messages"] == [{"round": 1, "chars": 250, "reason": "too long"}]
    assert len(b["messages_sent"]) == 5
    assert result.record()["agents"]["A"]["delivered_chars"] == sum(
        m["chars"] for m in b["messages_sent"]
    )


async def test_a_silent_peer(episode_task_dir, make_run, make_meter):
    behaviour = {"A": {"verdict": "REJECT"}, "B": {"verdict": "ACCEPT", "silent": True}}
    result, _, _ = await play(episode_task_dir, make_run, make_meter, behaviour)
    record = result.record()
    assert record["agents"]["B"]["silent_rounds"] == [1, 2, 3, 4, 5]
    assert record["agents"]["A"]["delivered_chars"] == 0
    seen = json.dumps(result.transcript("A")["messages"])
    assert all(f"Round {r}: B sent no message." in seen for r in range(1, 6))


async def test_usage_is_metered_per_phase_and_agent(episode_task_dir, make_run, make_meter):
    _, _, meter = await play(episode_task_dir, make_run, make_meter)
    lines = list(read_usage_lines(meter.path))
    assert {line["phase"] for line in lines} == {"task", "communication", "verdict", "reflection"}
    assert {line["agent"] for line in lines} == {"A", "B"}
    assert {line["episode"] for line in lines} == {1}


def test_swarm_episode_dry_run_end_to_end(episode_task_dir):
    runner = CliRunner()
    args = [
        "episode",
        "--dry-run",
        "--task-dir",
        str(episode_task_dir),
        "--sequence",
        "1",
        "--episode",
        "2",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    run_dir = Path("runs") / "ep_claude-haiku-5-5_s1e2_dry"
    record = json.loads((run_dir / "episodes" / "s01_e02.json").read_text())
    assert record["sequence_id"] == 1 and record["episode"] == 2 and record["dry_run"] is True
    ids = episode_task_ids(episode_task_dir, 1, 2)
    assert record["agents"]["A"]["task_id"] == ids["A"]
    assert record["agents"]["B"]["task_id"] == ids["B"]
    for agent in ("A", "B"):
        assert (run_dir / "transcripts" / f"s01_e02_{agent}.json").is_file()
    assert (run_dir / "usage.jsonl").is_file()
    again = runner.invoke(app, args)
    assert again.exit_code == 1 and "--force" in again.output


def test_a_live_episode_is_refused_without_swarm_live(episode_task_dir):
    result = CliRunner().invoke(app, ["episode", "--task-dir", str(episode_task_dir)])
    assert result.exit_code == 2 and "refused" in result.output
    assert not Path("runs").exists()


def test_an_unknown_episode_is_rejected(episode_task_dir):
    result = CliRunner().invoke(
        app, ["episode", "--dry-run", "--task-dir", str(episode_task_dir), "--episode", "9"]
    )
    assert result.exit_code == 1 and "no episode 9" in result.output


async def test_a_refusal_at_the_end_of_the_task_phase(episode_task_dir, make_run, make_meter):
    """The live failure of 2026-11-07: the last task-phase reply is a refusal cut off inside a
    send_message call. The call is answered 'not executed', the episode goes on, and the record
    keeps the refusal with its category."""
    behaviour = {"A": {"verdict": "REJECT", "refuse_in_task": True}, "B": {"verdict": "ACCEPT"}}
    result, clients, _ = await play(episode_task_dir, make_run, make_meter, behaviour)
    a = result.record()["agents"]["A"]
    assert [(r["phase"], r["category"]) for r in a["refusals"]] == [
        ("task", "reasoning_extraction")
    ]
    assert a["refusals"][0]["tool_calls"][0]["name"] == "send_message"
    assert a["refusal_counts"] == {"task": {"reasoning_extraction": 1}}
    assert result.record()["refusals_total"] == 1
    assert a["task"]["stop_cause"] == "refusal" and a["task"]["answer_saved"]
    assert a["phase_violations"] == []  # the cut-off call never reached the gate
    assert len(a["messages_sent"]) == 5 and result.record()["agents"]["B"]["refusals"] == []
    not_run = [c for c in result.transcript("A")["tool_calls"] if not c["executed"]]
    assert [c["name"] for c in not_run] == ["send_message"]
    assert len(clients["A"].calls) > 5  # every later request passed the tool_result check
