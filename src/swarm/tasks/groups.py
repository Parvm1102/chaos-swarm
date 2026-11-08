"""Fixed task schedules for Experiment 4's groups: which task each agent of a group solves in
every episode (`swarm tasks groups`).

make_groups builds n_groups groups of `group_size` agents and `episodes` episodes:
- within a group, all group_size * episodes tasks are distinct, so no agent ever solves or
  verifies a task twice (its own ten tasks and the ten it verifies are all different);
- every group draws the same number of tasks per template (15 for 6 agents, 10 episodes and 4
  templates), and every agent gets 3/3/2/2 tasks of the four templates (AGENT_PATTERN: each
  agent has two "3" templates, and each template has exactly three "3" agents);
- across groups every task is used equally often (6 times for 25 tasks per template and 10
  groups): group g takes the window of 15 positions starting at 15 * g in a cyclic, shuffled
  deck per template. A window of 15 out of 25 never repeats a task, and the windows cover every
  position the same number of times.
The same groups.json is reused by every population arm, so group g is comparable across arms.
"""

import random
from pathlib import Path

import orjson
from pydantic import BaseModel, ConfigDict

from swarm.tasks.generate import Manifest, derive_seed

GROUPS_FILE = "groups.json"
# Rows: agents (before the per-group shuffle); 1 = that template gets one extra task (3, not 2).
AGENT_PATTERN = ((0, 1), (2, 3), (0, 2), (1, 3), (0, 3), (1, 2))


class Group(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: int
    agents: list[list[str]]  # agents[i][e - 1]: agent i's task in episode e


class GroupsFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    task_set_seed: int
    manifest_digest: str  # ties these groups to one exact task set
    n_groups: int
    group_size: int
    episodes: int
    groups: list[Group]


def make_groups(
    manifest: Manifest, seed: int, n_groups: int = 10, group_size: int = 6, episodes: int = 10
) -> list[Group]:
    """Deterministic group schedules (see the module docstring for the rules)."""
    by_template: dict[str, list[str]] = {}
    for task in manifest.tasks:
        by_template.setdefault(task.template, []).append(task.task_id)
    names = sorted(by_template)
    if len(names) != 4 or group_size != len(AGENT_PATTERN) or episodes != 10:
        raise ValueError("the group schedule is built for 4 templates, 6 agents and 10 episodes")
    per_group = group_size * episodes // len(names)  # 15
    rng = random.Random(derive_seed(seed, "groups"))
    decks = {}
    for name in names:
        deck = sorted(by_template[name])
        if len(deck) <= per_group or (n_groups * per_group) % len(deck):
            raise ValueError(f"template {name}: {len(deck)} tasks cannot fill the windows evenly")
        rng.shuffle(deck)
        decks[name] = deck
    groups = []
    for g in range(n_groups):
        window = {
            name: [deck[(per_group * g + i) % len(deck)] for i in range(per_group)]
            for name, deck in decks.items()
        }
        rows = list(AGENT_PATTERN)
        rng.shuffle(rows)
        taken = dict.fromkeys(names, 0)
        agents = []
        for extra in rows:
            tasks = []
            for index, name in enumerate(names):
                count = 3 if index in extra else 2
                tasks.extend(window[name][taken[name] : taken[name] + count])
                taken[name] += count
            rng.shuffle(tasks)
            agents.append(tasks)
        groups.append(Group(group_id=g, agents=agents))
    return groups


def write_groups(path: Path, manifest: Manifest, groups: list[Group], seed: int) -> GroupsFile:
    """Write groups.json (byte-identical for the same inputs)."""
    content = GroupsFile(
        seed=seed,
        task_set_seed=manifest.base_seed,
        manifest_digest=manifest.digest(),
        n_groups=len(groups),
        group_size=len(groups[0].agents) if groups else 0,
        episodes=len(groups[0].agents[0]) if groups else 0,
        groups=groups,
    )
    Path(path).write_bytes(
        orjson.dumps(content.model_dump(mode="json"), option=orjson.OPT_INDENT_2) + b"\n"
    )
    return content


def load_groups(path: Path) -> GroupsFile:
    return GroupsFile.model_validate_json(Path(path).read_bytes())
