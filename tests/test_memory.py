"""The memory digest: rendered from the agent's own records, holding the channel, its verdict, the
feedback and its note, and never the tool traces or the peer's private texts."""

from swarm.agents.memory import load_memory_prompts, memory_text

PROMPTS = load_memory_prompts()


def record(
    episode: int, *, a_note: str = "Next time: ask early.", a_verdict: str | None = "REJECT"
):
    return {
        "episode": episode,
        "channel": [
            {"round": 1, "sender": "A", "text": "A here: saved 9 IDs."},
            {"round": 1, "sender": "B", "text": None},
            {"round": 2, "sender": "A", "text": "Please send your record."},
            {"round": 2, "sender": "B", "text": "B here: 12 IDs, query below."},
        ],
        "agents": {
            "A": {
                "verdict": {
                    "verdict": a_verdict,
                    "justification": "Your record never arrived.",
                    "source": "tool" if a_verdict else "missing",
                },
                "feedback": f"Episode {episode} result:\n- Your verdict on B's work: REJECT.",
                "reflection": a_note,
                "task": {"log_chars": 6000, "secret_tool_trace": "SELECT * FROM refunds"},
            },
            "B": {
                "verdict": {"verdict": "ACCEPT", "justification": "B-PRIVATE-JUSTIFICATION"},
                "feedback": "B-PRIVATE-FEEDBACK",
                "reflection": "B-PRIVATE-NOTE",
                "task": {},
            },
        },
    }


def test_the_first_episode_has_no_memory():
    assert (
        memory_text("A", [], enabled=True, prompts=PROMPTS)
        == "None yet: this is your first episode."
    )
    assert (
        memory_text("A", [], enabled=False, prompts=PROMPTS)
        == "None yet: this is your first episode."
    )


def test_memory_off_hides_every_earlier_episode():
    assert memory_text("A", [record(1)], enabled=False, prompts=PROMPTS) == "None available."


def test_the_digest_holds_the_channel_the_verdict_the_feedback_and_the_note():
    text = memory_text("A", [record(1), record(2)], enabled=True, prompts=PROMPTS)
    assert text.index("Episode 1\n") < text.index("Episode 2\n")  # in order
    assert "- Round 1, you: A here: saved 9 IDs." in text
    assert "- Round 1: B sent no message." in text
    assert "- Round 2, B: B here: 12 IDs, query below." in text
    assert "Your verdict on B's work: REJECT" in text
    assert "Your justification: Your record never arrived." in text
    assert "Episode 2 result:\n- Your verdict on B's work: REJECT." in text
    assert "Your note to yourself:\nNext time: ask early." in text


def test_the_digest_never_holds_private_or_task_material():
    text = memory_text("A", [record(1)], enabled=True, prompts=PROMPTS)
    for leaked in ("B-PRIVATE", "SELECT * FROM refunds", "secret_tool_trace"):
        assert leaked not in text


def test_a_missing_verdict_and_an_empty_note():
    text = memory_text("A", [record(1, a_note="", a_verdict=None)], enabled=True, prompts=PROMPTS)
    assert "Your verdict on B's work: none submitted (counted as REJECT)" in text
    assert "Your note to yourself:\n(none)" in text


def test_the_digest_is_written_from_each_agents_own_side():
    text = memory_text("B", [record(1)], enabled=True, prompts=PROMPTS)
    assert "- Round 1, A: A here: saved 9 IDs." in text
    assert "- Round 1: you sent no message." in text
    assert "B-PRIVATE-NOTE" in text  # B's own note
