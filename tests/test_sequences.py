"""Episode sequences: every constraint holds for 20 sequences, and regeneration is
byte-identical."""

from collections import Counter

import pytest

from swarm.tasks.generate import Manifest, ManifestTask
from swarm.tasks.sequences import load_sequences, make_sequences, write_sequences

TEMPLATE_NAMES = ("T1", "T2", "T3", "T4")


def fake_manifest(per_template: int = 25) -> Manifest:
    """A manifest with only what sequencing reads (task ids and templates) filled in."""
    tasks = [
        ManifestTask(
            task_id=f"task_{i:04d}",
            template=TEMPLATE_NAMES[i % len(TEMPLATE_NAMES)],
            difficulty="base",
            db_seed=i,
            params={},
            nl_request="",
            reference_ids=[1, 2, 3, 4, 5],
            answer_table="support_cases",
            n_answer=5,
            n_samples=1,
            schema_dump_chars=4000,
            fingerprint=f"fingerprint-{i}",
        )
        for i in range(per_template * len(TEMPLATE_NAMES))
    ]
    return Manifest(generator="test", base_seed=0, n_tasks=len(tasks), row_counts={}, tasks=tasks)


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return fake_manifest()


@pytest.fixture(scope="module")
def sequences(manifest):
    return make_sequences(manifest, seed=0, n_sequences=20, episodes=10)


def test_twenty_sequences_of_ten_episodes(sequences):
    assert len(sequences) == 20
    assert [s.sequence_id for s in sequences] == list(range(20))
    for sequence in sequences:
        assert [episode.episode for episode in sequence.episodes] == list(range(1, 11))


def test_each_agent_sees_ten_distinct_tasks_and_the_two_sets_are_disjoint(sequences):
    for sequence in sequences:
        a_tasks = [episode.a for episode in sequence.episodes]
        b_tasks = [episode.b for episode in sequence.episodes]
        assert len(set(a_tasks)) == 10 and len(set(b_tasks)) == 10
        assert not set(a_tasks) & set(b_tasks)
        assert all(episode.a != episode.b for episode in sequence.episodes)


def test_templates_are_balanced_across_the_ten_episodes(sequences, manifest):
    template_of = {task.task_id: task.template for task in manifest.tasks}
    for sequence in sequences:
        for agent in ("a", "b"):
            counts = Counter(template_of[getattr(e, agent)] for e in sequence.episodes)
            assert sorted(counts.values()) == [2, 2, 3, 3]  # no sequence is dominated by one
        both = Counter(template_of[t] for e in sequence.episodes for t in (e.a, e.b))
        assert set(both.values()) == {5}


def test_every_task_is_used_exactly_four_times(sequences, manifest):
    uses = Counter(t for s in sequences for e in s.episodes for t in (e.a, e.b))
    assert set(uses) == {task.task_id for task in manifest.tasks}
    assert set(uses.values()) == {4}


def test_regeneration_with_the_same_seed_is_byte_identical(manifest, tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    write_sequences(first, manifest, make_sequences(manifest, seed=0), seed=0)
    write_sequences(second, manifest, make_sequences(manifest, seed=0), seed=0)
    assert first.read_bytes() == second.read_bytes()
    loaded = load_sequences(first)
    assert loaded.seed == 0
    assert loaded.manifest_digest == manifest.digest()
    assert (loaded.n_sequences, loaded.episodes) == (20, 10)


def test_a_different_seed_gives_different_sequences(manifest):
    assert make_sequences(manifest, seed=0) != make_sequences(manifest, seed=1)


def test_counts_that_cannot_be_balanced_are_refused():
    with pytest.raises(ValueError, match="used equally"):
        make_sequences(fake_manifest(per_template=24), seed=0)
