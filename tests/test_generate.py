"""Task generation: determinism, the stored answer, bounds, binding, and both failure paths."""

import dataclasses
import sqlite3

import pytest

from swarm.tasks.generate import (
    MAX_REJECTIONS,
    GroundTruthMismatch,
    TaskGenerationError,
    answer_cap,
    fingerprint,
    generate_task,
    generate_tasks,
    load_task,
)
from swarm.tasks.schema import load_tables
from swarm.tasks.templates import T1, T4, TEMPLATES_BY_NAME


@pytest.fixture(scope="module")
def task_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("tasks")
    manifest = generate_tasks(base_seed=0, n=8, out_root=out)
    return out / "seed0", manifest


def open_db(root, task):
    return sqlite3.connect(root / task.task_id / "db.sqlite")


def test_the_same_seed_gives_an_identical_task():
    first = generate_task(3, 0, T4)
    second = generate_task(3, 0, T4)
    assert first == second
    assert first.fingerprint == second.fingerprint


def test_a_different_seed_or_index_gives_a_different_task():
    base = generate_task(3, 0, T4)
    assert generate_task(3, 1, T4).fingerprint != base.fingerprint
    assert generate_task(7, 0, T4).fingerprint != base.fingerprint


def test_templates_cycle_t1_to_t4(task_set):
    _, manifest = task_set
    assert [task.template for task in manifest.tasks] == ["T1", "T2", "T3", "T4"] * 2


def test_reference_answers_table_holds_exactly_the_reference_ids(task_set):
    root, manifest = task_set
    for task in manifest.tasks:
        with open_db(root, task) as conn:
            rows = conn.execute(
                "SELECT task_id, answer_id FROM _reference_answers ORDER BY answer_id"
            ).fetchall()
        assert rows == [(task.task_id, answer_id) for answer_id in task.reference_ids]


def test_answers_are_within_bounds(task_set):
    root, manifest = task_set
    for task in manifest.tasks:
        with open_db(root, task) as conn:
            table_rows = conn.execute(f"SELECT COUNT(*) FROM {task.answer_table}").fetchone()[0]
        assert 5 <= task.n_answer <= 30
        assert task.n_answer <= answer_cap(table_rows)
        assert task.reference_ids == sorted(set(task.reference_ids))
        assert task.n_answer == len(task.reference_ids)


def test_every_filter_binds_in_the_generated_tasks(task_set):
    root, manifest = task_set
    for task in manifest.tasks:
        template = TEMPLATES_BY_NAME[task.template]
        with open_db(root, task) as conn:
            tables = load_tables(conn)
        answer = set(task.reference_ids)
        assert template.evaluate_py(task.params, tables) == answer
        for f in template.filters:
            assert template.evaluate_py(task.params, tables, f.name) != answer, f.name


def test_written_files_match_the_spec(task_set):
    root, manifest = task_set
    for task in manifest.tasks:
        spec = load_task(root, task.task_id)
        with open_db(root, task) as conn:
            assert fingerprint(conn, spec.reference_ids) == spec.fingerprint
            assert {row[0] for row in conn.execute(spec.reference_sql)} == set(spec.reference_ids)


def test_the_manifest_hides_the_sql_but_task_json_keeps_it(task_set):
    root, manifest = task_set
    raw = (root / "manifest.json").read_text()
    assert "reference_sql" not in raw and "SELECT" not in raw
    for task in manifest.tasks:
        assert load_task(root, task.task_id).reference_sql.startswith("SELECT")


def test_regeneration_reproduces_the_manifest_byte_for_byte(task_set, tmp_path):
    root, manifest = task_set
    again = generate_tasks(0, 8, tmp_path)
    assert again == manifest
    assert (tmp_path / "seed0" / "manifest.json").read_bytes() == (
        root / "manifest.json"
    ).read_bytes()


def test_impossible_parameters_raise_after_50_draws():
    draws = 0

    def impossible(rng, tables):
        nonlocal draws
        draws += 1
        params = T1.sample_params(rng, tables)
        params["min_severity"] = 5  # severities only go up to 4
        return params

    with pytest.raises(TaskGenerationError, match=r"T1 cases_with_refund_and_note .*50 draws"):
        generate_task(0, 0, dataclasses.replace(T1, sample_params=impossible))
    assert draws == MAX_REJECTIONS == 50


def test_a_sql_python_disagreement_raises_instead_of_resampling():
    broken = dataclasses.replace(T4, evaluate_py=lambda params, tables, drop=None: set())
    with pytest.raises(GroundTruthMismatch, match="T4 open_tickets"):
        generate_task(3, 0, broken)


def test_an_existing_task_set_is_only_replaced_with_force(tmp_path):
    generate_tasks(0, 4, tmp_path)
    with pytest.raises(FileExistsError, match="--force"):
        generate_tasks(0, 4, tmp_path)
    assert generate_tasks(0, 4, tmp_path, force=True).n_tasks == 4


def test_only_the_base_difficulty_exists_so_far():
    with pytest.raises(ValueError, match="difficulty"):
        generate_task(0, 0, T1, difficulty="traps")
