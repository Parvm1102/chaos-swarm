"""The channel: 200-character messages, one per turn, A before B, silence notices, and the
character count that proves a complete log cannot pass."""

import pytest

from swarm.env.channel import MESSAGE_LIMIT, ROUNDS, Channel
from swarm.llm.loop import ToolError


def test_a_message_is_delivered_to_the_peer():
    channel = Channel()
    channel.open_turn("A", 1)
    assert channel.send("A", "hello B") == "delivered to B (7 characters)"
    assert channel.close_turn() is True
    [delivery] = channel.take_inbox("B")
    assert (delivery.round, delivery.sender, delivery.text) == (1, "A", "hello B")
    assert channel.take_inbox("B") == []  # read once
    assert channel.take_inbox("A") == []


def test_a_message_over_the_limit_is_refused_and_a_retry_works():
    channel = Channel()
    channel.open_turn("A", 1)
    with pytest.raises(ToolError, match=f"{MESSAGE_LIMIT + 1} characters; the limit is 200"):
        channel.send("A", "x" * (MESSAGE_LIMIT + 1))
    assert channel.take_inbox("B") == []
    assert channel.send("A", "x" * MESSAGE_LIMIT).startswith("delivered")  # exactly 200 is fine
    channel.close_turn()
    assert [(r.chars, r.reason) for r in channel.refusals] == [(MESSAGE_LIMIT + 1, "too long")]
    assert channel.delivered_chars("B") == MESSAGE_LIMIT


def test_an_empty_message_is_refused():
    channel = Channel()
    channel.open_turn("B", 1)
    with pytest.raises(ToolError, match="empty"):
        channel.send("B", "   ")
    assert channel.refusals[0].reason == "empty"


def test_one_message_per_turn_and_only_in_your_own_turn():
    channel = Channel()
    channel.open_turn("A", 1)
    with pytest.raises(ToolError, match="your own channel turn"):
        channel.send("B", "not my turn")
    channel.send("A", "first")
    with pytest.raises(ToolError, match="already sent your message for round 1"):
        channel.send("A", "second")
    channel.close_turn()
    with pytest.raises(ToolError, match="your own channel turn"):
        channel.send("A", "no turn is open")


def test_a_turn_without_a_message_sends_a_silence_notice():
    channel = Channel()
    channel.open_turn("B", 3)
    assert channel.close_turn() is False
    [notice] = channel.take_inbox("A")
    assert (notice.round, notice.sender, notice.text, notice.chars) == (3, "B", None, 0)
    assert channel.silent_rounds("B") == [3]


def test_a_full_exchange_in_speaking_order():
    channel = Channel()
    seen_by_b = []
    for round_number in range(1, ROUNDS + 1):
        channel.open_turn("A", round_number)
        channel.send("A", f"A{round_number}")
        channel.close_turn()
        seen_by_b.append([d.text for d in channel.take_inbox("B")])
        channel.open_turn("B", round_number)
        channel.send("B", f"B{round_number}")
        channel.close_turn()
    # B reads A's message in the same round; A reads B's round-r message only later.
    assert seen_by_b == [[f"A{r}"] for r in range(1, ROUNDS + 1)]
    assert [d.text for d in channel.take_inbox("A")] == [f"B{r}" for r in range(1, ROUNDS + 1)]
    assert [d.text for d in channel.sent_by("A")] == [f"A{r}" for r in range(1, ROUNDS + 1)]


def test_an_agent_can_receive_at_most_a_thousand_characters():
    channel = Channel()
    for round_number in range(1, ROUNDS + 1):
        channel.open_turn("A", round_number)
        channel.send("A", "y" * MESSAGE_LIMIT)
        channel.close_turn()
    assert channel.delivered_chars("B") == ROUNDS * MESSAGE_LIMIT == 1_000
    assert channel.delivered_chars("A") == 0


def test_turns_are_checked():
    channel = Channel()
    with pytest.raises(ValueError):
        channel.open_turn("A", ROUNDS + 1)
    with pytest.raises(RuntimeError):
        channel.close_turn()
    channel.open_turn("A", 1)
    with pytest.raises(RuntimeError):
        channel.open_turn("B", 1)
