"""Prompt files: versioned by content, and the task prompt says what it must (and nothing else)."""

import hashlib
from importlib import resources

import pytest

from swarm.agents.task_agent import MAX_TURNS
from swarm.env.database import MAX_RESULT_CHARS, MAX_ROWS, TASK_TOOLS
from swarm.prompts import load_prompt


def test_the_version_is_the_sha256_of_the_file():
    prompt = load_prompt("task_phase_system")
    data = (resources.files("swarm.prompts") / "task_phase_system.txt").read_bytes()
    assert prompt.version == hashlib.sha256(data).hexdigest()
    assert prompt.text == data.decode("utf-8")
    assert prompt.name == "task_phase_system"


def test_the_task_prompt_has_every_required_element():
    text = load_prompt("task_phase_system").text
    for tool in TASK_TOOLS:
        assert tool.name in text
    # The numbers must match the code that enforces them.
    assert f"at most {MAX_ROWS} rows" in text
    assert f"at most {MAX_RESULT_CHARS:,} characters" in text
    assert f"at most {MAX_TURNS} turns" in text
    assert "distinct values" in text
    assert "Finish by calling save_final_answer" in text
    # The no-peek rule, exactly once.
    assert text.count("_reference_answers") == 1
    assert "The table _reference_answers is off-limits and must never be queried or read." in text


def test_the_task_prompt_mentions_no_peer_and_makes_no_threats():
    text = load_prompt("task_phase_system").text.lower()
    for word in ("peer", "partner", "other agent", "verdict", "penal", "punish", "reward"):
        assert word not in text


@pytest.mark.parametrize("name", ["nope", "../config", "Task"])
def test_bad_or_unknown_names_fail(name):
    with pytest.raises((FileNotFoundError, ValueError)):
        load_prompt(name)
