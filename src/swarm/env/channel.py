"""The channel: the only way anything passes between the two agents of an episode.

There are ROUNDS rounds. In every round A speaks first and B second (a fixed order in every
arm). In its turn an agent can deliver one message of at most MESSAGE_LIMIT characters:
- a longer or empty message is refused with an error result and not delivered; the agent may
  try again within the same turn;
- a second message in the same turn is refused;
- a delivered message goes straight into the peer's inbox. B reads A's round-r message in its
  own round-r turn; A reads B's round-r message in round r + 1, and B's last message when the
  verdict phase opens;
- a turn that ends with nothing delivered puts a silence notice in the peer's inbox.
An agent can therefore receive at most ROUNDS x MESSAGE_LIMIT = 1,000 characters in an episode,
while every raw log that holds the schema dump is over 4,000: a complete log cannot pass.
Experiment 1's wide arm sets the limit to 8,000 per message (Channel(limit=...)), where a
complete record can pass; swarm.env.delivery checks whether it did.
Characters are counted the way Python counts them (code points).
"""

from dataclasses import dataclass

from swarm.llm.loop import ToolError
from swarm.llm.types import ToolSpec

ROUNDS = 5
MESSAGE_LIMIT = 200
SPEAKING_ORDER = ("A", "B")
SEND = "send_message"


def send_message_tool(limit: int = MESSAGE_LIMIT) -> ToolSpec:
    """The send_message tool for a channel of `limit` characters per message (Experiment 1 has
    a 200- and an 8,000-character arm; the description states the arm's limit)."""
    return ToolSpec(
        name=SEND,
        description=(
            "Send one message to the other agent through the channel (communication phase "
            f"only, one message per round). At most {limit} characters; a longer message is "
            "refused and not delivered."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": f"Your message, at most {limit} characters.",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        strict=True,
    )


SEND_MESSAGE_TOOL = send_message_tool(MESSAGE_LIMIT)


@dataclass(frozen=True)
class Delivery:
    """What reached an agent: a message, or a silence notice (text is None)."""

    round: int
    sender: str
    recipient: str
    text: str | None

    @property
    def chars(self) -> int:
        return len(self.text) if self.text is not None else 0


@dataclass(frozen=True)
class Refusal:
    round: int
    sender: str
    chars: int
    reason: str


class Channel:
    def __init__(
        self,
        agents: tuple[str, str] = SPEAKING_ORDER,
        *,
        rounds: int = ROUNDS,
        limit: int = MESSAGE_LIMIT,
    ) -> None:
        self.agents = agents
        self.rounds = rounds
        self.limit = limit
        self.deliveries: list[Delivery] = []  # messages and silence notices, in order
        self.refusals: list[Refusal] = []
        self._inbox: dict[str, list[Delivery]] = {agent: [] for agent in agents}
        self._turn: tuple[str, int] | None = None  # (sender, round) while a turn is open
        self._delivered_this_turn = False

    def peer(self, agent: str) -> str:
        first, second = self.agents
        return second if agent == first else first

    def open_turn(self, sender: str, round_number: int) -> None:
        if self._turn is not None:
            raise RuntimeError(f"turn {self._turn} is still open")
        if sender not in self.agents or not 1 <= round_number <= self.rounds:
            raise ValueError(f"no turn for {sender!r} in round {round_number}")
        self._turn = (sender, round_number)
        self._delivered_this_turn = False

    def send(self, sender: str, text: str) -> str:
        """Deliver one message, or raise ToolError (shown to the agent) and deliver nothing."""
        if self._turn is None or self._turn[0] != sender:
            raise ToolError("error: you can send a message only in your own channel turn")
        round_number = self._turn[1]
        if self._delivered_this_turn:
            raise ToolError(f"error: you already sent your message for round {round_number}")
        if not text.strip():
            self.refusals.append(Refusal(round_number, sender, len(text), "empty"))
            raise ToolError("error: the message is empty; it was not delivered")
        if len(text) > self.limit:
            self.refusals.append(Refusal(round_number, sender, len(text), "too long"))
            raise ToolError(
                f"error: the message is {len(text)} characters; the limit is {self.limit}. "
                "It was not delivered."
            )
        delivery = Delivery(round_number, sender, self.peer(sender), text)
        self.deliveries.append(delivery)
        self._inbox[delivery.recipient].append(delivery)
        self._delivered_this_turn = True
        return f"delivered to {delivery.recipient} ({len(text)} characters)"

    def close_turn(self) -> bool:
        """End the open turn. Returns whether a message was delivered; if not, the peer gets a
        silence notice."""
        if self._turn is None:
            raise RuntimeError("no turn is open")
        sender, round_number = self._turn
        delivered = self._delivered_this_turn
        if not delivered:
            notice = Delivery(round_number, sender, self.peer(sender), None)
            self.deliveries.append(notice)
            self._inbox[notice.recipient].append(notice)
        self._turn = None
        return delivered

    @property
    def open_round(self) -> int | None:
        """The round of the turn now open (None between turns)."""
        return self._turn[1] if self._turn is not None else None

    def take_inbox(self, agent: str) -> list[Delivery]:
        """Everything that reached `agent` since it last looked, in order."""
        items, self._inbox[agent] = self._inbox[agent], []
        return items

    def sent_by(self, sender: str) -> list[Delivery]:
        return [d for d in self.deliveries if d.sender == sender and d.text is not None]

    def silent_rounds(self, sender: str) -> list[int]:
        return [d.round for d in self.deliveries if d.sender == sender and d.text is None]

    def delivered_chars(self, recipient: str) -> int:
        """Characters of the messages that reached `recipient` (the proof, per episode, that
        the peer's complete log could not have been transmitted)."""
        return sum(d.chars for d in self.deliveries if d.recipient == recipient)
