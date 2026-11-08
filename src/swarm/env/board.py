"""Experiment 4's shared board: one group-wide notice board (the board arms only).

post_to_board(text): at most BOARD_LIMIT characters, one post per agent per episode, in the
communication phase only (the phase gate in swarm.agents.episode). A post is a free call: a
model call made of post_to_board alone does not use up the round's message (run_tool_loop's
free_tool, one such call per episode). A refused post (too long, or a second one) is answered
with board_refused and recorded, never posted.

What agents see: at the start of its communication phase, every agent of a group sees the last
BOARD_SHOWN posts made in EARLIER episodes of its group, oldest first, anonymous ("- Episode 4:
..."). Posts of the current episode are not shown: the three pairs of an episode run at the same
time, so a live board would make what an agent sees depend on timing, and resume would break.
board_snapshot builds the shown text from the earlier episodes' records alone.
"""

from typing import Any

from swarm.llm.types import ToolSpec
from swarm.prompts import Prompt, render

BOARD = "post_to_board"
BOARD_LIMIT = 200
BOARD_SHOWN = 12
BOARD_PROMPTS = ("board_rules", "board_posted", "board_refused")
SNAPSHOT_PROMPTS = ("board_snapshot", "board_empty", "board_post_line")
POST_TO_BOARD_TOOL = ToolSpec(
    name=BOARD,
    description=(
        "Post a message to the group's shared board (communication phase only; one post per "
        f"episode, at most {BOARD_LIMIT} characters). Every agent of the group sees it from the "
        "next episode on."
    ),
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Your post."}},
        "required": ["text"],
        "additionalProperties": False,
    },
    strict=True,
)


def board_posts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every post in these pair-episode records, in board order: by episode, then pair index,
    then role (A before B). Each post: episode, pair, role, agent (group index), planted, text."""
    posts = []
    ordered = sorted(records, key=lambda r: (r["episode"], r["group"]["pair_index"]))
    for record in ordered:
        for role in ("A", "B"):
            for post in record["agents"][role].get("board_posts", []):
                posts.append(
                    {
                        "episode": record["episode"],
                        "pair": record["group"]["pair_index"],
                        "role": role,
                        "agent": record["group"]["agents"][role],
                        "planted": record["group"]["planted"][role],
                        "text": post["text"],
                    }
                )
    return posts


def board_shown(records: list[dict[str, Any]], episode: int) -> list[dict[str, Any]]:
    """The posts shown at the start of `episode`: the last BOARD_SHOWN from earlier episodes."""
    earlier = [r for r in records if r["episode"] < episode]
    return board_posts(earlier)[-BOARD_SHOWN:]


def board_snapshot(shown: list[dict[str, Any]], prompts: dict[str, Prompt]) -> str:
    """The text an agent reads: the shown posts, or board_empty."""
    if not shown:
        return prompts["board_empty"].text.strip()
    lines = "\n".join(
        render(prompts["board_post_line"], episode=p["episode"], text=p["text"]) for p in shown
    )
    return render(prompts["board_snapshot"], n=len(shown), posts=lines)
