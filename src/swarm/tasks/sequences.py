"""Fixed episode sequences: which task each agent gets in every episode.

make_sequences builds n_sequences sequences of `episodes` episodes for agents A and B:
- within a sequence A and B get disjoint task sets: each agent sees `episodes` distinct tasks,
  and B never later receives a task that A solved and talked about (or the other way round);
- every sequence uses the same number of tasks per template (5 for 10 episodes and 4 templates),
  split between A and B as evenly as possible (3/2), so no agent's sequence is dominated by one
  template;
- across all sequences every task is used exactly the same number of times (4 for 100 tasks and
  20 sequences): each template's tasks are dealt from shuffled decks, one block per sequence.
The same sequences.json is reused by every experimental arm.

make_sequence_set deals more than SEQUENCE_BATCH sequences in batches of SEQUENCE_BATCH: batch 0
is exactly what make_sequences gives for a single batch, and every later batch draws from its
own seeded stream. A set of 40 therefore starts with the very same 20 sequences as a set of 20
(append-only), and each batch on its own uses every task equally often.
"""

import random
from pathlib import Path

import orjson
from pydantic import BaseModel, ConfigDict

from swarm.tasks.generate import Manifest, derive_seed

SEQUENCES_FILE = "sequences.json"
SEQUENCE_BATCH = 20


class Episode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode: int  # 1-based
    a: str  # task id for agent A
    b: str  # task id for agent B


class Sequence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence_id: int
    episodes: list[Episode]


class SequencesFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    task_set_seed: int
    manifest_digest: str  # ties these sequences to one exact task set
    n_sequences: int
    episodes: int
    sequences: list[Sequence]


def make_sequences(
    manifest: Manifest,
    seed: int,
    n_sequences: int = 20,
    episodes: int = 10,
    *,
    batch: int = 0,
) -> list[Sequence]:
    """Deterministic sequences for this manifest (see the module docstring for the rules).

    Raises ValueError when the counts cannot be balanced exactly (for example a template whose
    task count does not divide the number of slots it has to fill).
    """
    # Batch 0 keeps the original stream, so files written before batches existed are unchanged.
    stream_seed = (
        derive_seed(seed, "sequences") if batch == 0 else derive_seed(seed, "sequences", batch)
    )
    rng = random.Random(stream_seed)
    by_template: dict[str, list[str]] = {}
    for task in manifest.tasks:
        by_template.setdefault(task.template, []).append(task.task_id)
    names = sorted(by_template)
    if (2 * episodes) % len(names):
        raise ValueError(f"{2 * episodes} task slots per sequence do not split over {names}")
    per_template = 2 * episodes // len(names)
    # A gets a_base tasks of every template, plus one more for `a_extra` of them; B gets the rest
    # of each block. With 10 episodes and 4 templates: per_template 5, A gets 3/3/2/2, B 2/2/3/3.
    a_base = per_template // 2
    a_extra = episodes - a_base * len(names)

    blocks: dict[str, list[list[str]]] = {}
    for name in names:
        task_ids = by_template[name]
        slots = n_sequences * per_template
        if slots % len(task_ids) or len(task_ids) % per_template:
            raise ValueError(
                f"template {name}: {len(task_ids)} tasks cannot fill {slots} slots "
                f"in blocks of {per_template} with every task used equally"
            )
        stream: list[str] = []
        for _ in range(slots // len(task_ids)):  # one shuffled deck per use
            deck = list(task_ids)
            rng.shuffle(deck)
            stream.extend(deck)
        # A block never straddles two decks (deck size is a multiple of the block size), so the
        # tasks of one template inside one sequence are always distinct.
        blocks[name] = [stream[i : i + per_template] for i in range(0, slots, per_template)]

    sequences = []
    for index in range(n_sequences):
        a_more = set(rng.sample(names, a_extra))
        a_tasks: list[str] = []
        b_tasks: list[str] = []
        for name in names:
            block = blocks[name][index]
            take = a_base + (1 if name in a_more else 0)
            a_tasks.extend(block[:take])
            b_tasks.extend(block[take:])
        rng.shuffle(a_tasks)
        rng.shuffle(b_tasks)
        sequences.append(
            Sequence(
                sequence_id=batch * n_sequences + index,
                episodes=[
                    Episode(episode=i + 1, a=a, b=b)
                    for i, (a, b) in enumerate(zip(a_tasks, b_tasks, strict=True))
                ],
            )
        )
    return sequences


def make_sequence_set(
    manifest: Manifest, seed: int, n_sequences: int = SEQUENCE_BATCH, episodes: int = 10
) -> list[Sequence]:
    """n_sequences sequences: one make_sequences batch up to SEQUENCE_BATCH, else whole batches of
    SEQUENCE_BATCH (see the module docstring)."""
    if n_sequences <= SEQUENCE_BATCH:
        return make_sequences(manifest, seed, n_sequences, episodes)
    if n_sequences % SEQUENCE_BATCH:
        raise ValueError(
            f"more than {SEQUENCE_BATCH} sequences come in whole batches of {SEQUENCE_BATCH}, "
            f"got {n_sequences}"
        )
    return [
        sequence
        for batch in range(n_sequences // SEQUENCE_BATCH)
        for sequence in make_sequences(manifest, seed, SEQUENCE_BATCH, episodes, batch=batch)
    ]


def write_sequences(
    path: Path, manifest: Manifest, sequences: list[Sequence], seed: int
) -> SequencesFile:
    """Write sequences.json. The output is byte-identical for the same inputs (no timestamps)."""
    content = SequencesFile(
        seed=seed,
        task_set_seed=manifest.base_seed,
        manifest_digest=manifest.digest(),
        n_sequences=len(sequences),
        episodes=len(sequences[0].episodes) if sequences else 0,
        sequences=sequences,
    )
    Path(path).write_bytes(
        orjson.dumps(content.model_dump(mode="json"), option=orjson.OPT_INDENT_2) + b"\n"
    )
    return content


def load_sequences(path: Path) -> SequencesFile:
    return SequencesFile.model_validate_json(Path(path).read_bytes())
