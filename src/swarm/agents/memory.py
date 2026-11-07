"""Cross-episode memory: what an agent carries from earlier episodes into the next one.

Memory is rendered, never replayed (step-0 decision): each episode starts a fresh message array,
and the agent's digest of its earlier episodes fills the $memory slot of its first user message.
The digest is built from the episode records alone, from the agent's own point of view. For each
earlier episode, in order, it holds:
- the channel: every message both ways, with its round, and every silence notice;
- the agent's own verdict and justification;
- the feedback it received (both verdicts, whether each was correct, the shared reward);
- its own note to itself (the reflection).
It never holds task tool traces (a documented deviation from the paper: memory keeps only the
communication-onward history), the peer's justification or note, or either request.

Episode 1 gets memory_none ("None yet: this is your first episode."). In a memory-off arm every
later episode gets memory_off ("None available.") instead of a digest.

A message longer than MEMORY_MESSAGE_MAX (400) characters is shown cut to its first 400, with
the marker "[… N more characters]" (memory_truncated; user decision for Experiment 1). Without
it the 8,000-character arm's digest alone would reach about 200k tokens by episode 10. In every
200-character arm no message is long enough to be cut, so nothing changes there.

Experiment 3: the agent's own reports to the operator follow the channel lines, one line each
(memory_report, cut the same way). The operator's reply is part of the feedback, which memory
already carries.
"""

from typing import Any

from swarm.prompts import Prompt, load_prompt, render

MEMORY_PROMPTS = (
    "memory_episode",
    "memory_message",
    "memory_silence",
    "memory_no_note",
    "memory_none",
    "memory_off",
    "memory_truncated",
    "memory_report",
)
MEMORY_MESSAGE_MAX = 400


def load_memory_prompts() -> dict[str, Prompt]:
    return {name: load_prompt(name) for name in MEMORY_PROMPTS}


def memory_text(
    agent: str, records: list[dict[str, Any]], *, enabled: bool, prompts: dict[str, Prompt]
) -> str:
    """The text for the $memory slot of the next episode, given the earlier episode records."""
    if not records:
        return prompts["memory_none"].text.strip()
    if not enabled:
        return prompts["memory_off"].text.strip()
    return "\n\n".join(_episode_text(agent, record, prompts) for record in records)


def _episode_text(agent: str, record: dict[str, Any], prompts: dict[str, Prompt]) -> str:
    own = record["agents"][agent]
    peer = next(name for name in record["agents"] if name != agent)
    lines = []
    for item in record["channel"]:
        sender = "you" if item["sender"] == agent else item["sender"]
        if item["text"] is None:
            lines.append(render(prompts["memory_silence"], round=item["round"], sender=sender))
        else:
            lines.append(
                render(
                    prompts["memory_message"],
                    round=item["round"],
                    sender=sender,
                    text=_shown(item["text"], prompts),
                )
            )
    for report in own.get("reports", []):
        lines.append(
            render(
                prompts["memory_report"],
                phase=report["phase"],
                text=_shown(report["text"], prompts),
            )
        )
    verdict = own["verdict"]
    shown = verdict["verdict"] or "none submitted (counted as REJECT)"  # as the feedback says it
    none = prompts["memory_no_note"].text.strip()
    return render(
        prompts["memory_episode"],
        episode=record["episode"],
        messages="\n".join(lines),
        peer=peer,
        verdict=shown,
        justification=verdict["justification"].strip() or none,
        feedback=own["feedback"],
        note=own["reflection"].strip() or none,
    )


def _shown(text: str, prompts: dict[str, Prompt]) -> str:
    """A message as memory shows it: whole, or its first MEMORY_MESSAGE_MAX characters + marker."""
    if len(text) <= MEMORY_MESSAGE_MAX:
        return text
    more = len(text) - MEMORY_MESSAGE_MAX
    return render(prompts["memory_truncated"], head=text[:MEMORY_MESSAGE_MAX], more=f"{more:,}")
