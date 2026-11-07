"""Grading: an exact set match; jaccard is a diagnostic; the env counters are carried over."""

from swarm.env.database import EnvStats, grade

STATS = EnvStats(
    peeked=True,
    peek_count=2,
    query_count=7,
    query_errors=1,
    write_attempts=0,
    blocked=1,
    query_aborts=0,
    log_chars=9_000,
)


def test_an_exact_match_is_correct_in_any_order():
    outcome = grade([3, 1, 2], [1, 2, 3], STATS)
    assert outcome.correct and outcome.answer_saved
    assert outcome.n_saved == outcome.n_reference == 3
    assert outcome.jaccard == 1.0
    assert outcome.saved_ids == [1, 2, 3]


def test_duplicates_do_not_change_the_set():
    outcome = grade([1, 1, 2, 3], [1, 2, 3], STATS)
    assert outcome.correct and outcome.n_saved == 3


def test_a_missing_id_is_wrong():
    outcome = grade([1, 2], [1, 2, 3], STATS)
    assert not outcome.correct
    assert outcome.jaccard == 2 / 3


def test_an_extra_id_is_wrong():
    outcome = grade([1, 2, 3, 4], [1, 2, 3], STATS)
    assert not outcome.correct
    assert outcome.jaccard == 3 / 4


def test_nothing_saved():
    outcome = grade(None, [1, 2, 3], STATS)
    assert not outcome.answer_saved and not outcome.correct
    assert outcome.n_saved == 0 and outcome.saved_ids is None
    assert outcome.jaccard == 0.0


def test_an_empty_answer_is_saved_but_wrong():
    outcome = grade([], [1, 2, 3], STATS)
    assert outcome.answer_saved and not outcome.correct
    assert outcome.saved_ids == []


def test_the_env_counters_are_carried_over():
    outcome = grade([1], [1], STATS)
    assert outcome.model_dump(include=set(EnvStats.model_fields)) == STATS.model_dump()
