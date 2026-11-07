"""The hard difficulty: every planted trap is where it should be, and the sloppy SQL an agent
might write (inclusive end, exclusive start, the other inequality, no NULL check) gets a
different answer. The checks run real SQL against the written databases, independently of the
evaluator-based check the generator itself uses."""

import random
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from swarm.tasks.generate import generate_task, generate_tasks, load_manifest, load_task
from swarm.tasks.schema import DATE_MAX
from swarm.tasks.templates import T1, T3, TEMPLATES_BY_NAME
from swarm.tasks.traps import N_NULL_TRAPS, TRAP_SPECS, day_level_window

# The reference SQL's NULL condition and amount comparison, per template (see templates.py).
NULL_SQL = {
    "T1": "AND r.approved_at IS NOT NULL",
    "T2": "AND sc.closed_at IS NULL",
    "T3": "AND pr.needed_by IS NULL",
    "T4": "AND t.resolved_at IS NULL",
}


@pytest.fixture(scope="module")
def hard_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("tasks")
    manifest = generate_tasks(base_seed=0, n=8, out_root=out, difficulty="hard")
    return out / "seed0-hard", manifest


def run(db, sql: str) -> set[int]:
    with closing(sqlite3.connect(db)) as conn:
        return {row[0] for row in conn.execute(sql)}


def cases(hard_set):
    root, manifest = hard_set
    for task in manifest.tasks:
        yield root / task.task_id / "db.sqlite", load_task(root, task.task_id)


def test_a_hard_set_has_its_own_folder_and_every_task_has_traps(hard_set):
    root, manifest = hard_set
    assert root.name == "seed0-hard"
    assert load_manifest(root) == manifest
    for task in manifest.tasks:
        assert task.difficulty == "hard"
        assert set(task.traps) >= {"date_start", "date_end", "null"}
        assert len(task.traps["null"]) == N_NULL_TRAPS
        assert ("amount" in task.traps) == (task.template in ("T1", "T3"))


def test_windows_are_on_days_not_month_starts(hard_set):
    for _, spec in cases(hard_set):
        start, end = (spec.params[key] for key in TRAP_SPECS[spec.template].window)
        assert date.fromisoformat(start).day != 1 and date.fromisoformat(end).day != 1
        assert start < end <= DATE_MAX.isoformat()
        assert start in spec.nl_request and end in spec.nl_request


def test_date_edge_rows_sit_on_the_window_edges(hard_set):
    for db, spec in cases(hard_set):
        trap = TRAP_SPECS[spec.template]
        table = TEMPLATES_BY_NAME[spec.template].answer_table
        start, end = (spec.params[key] for key in trap.window)
        with closing(sqlite3.connect(db)) as conn:
            day_of = dict(conn.execute(f"SELECT id, {trap.date_column} FROM {table}").fetchall())
        assert day_of[spec.traps["date_start"]] == start
        assert spec.traps["date_start"] in spec.reference_ids
        assert day_of[spec.traps["date_end"]] == end
        assert spec.traps["date_end"] not in spec.reference_ids
        for row_id in spec.traps["null"]:
            assert start < day_of[row_id] < end
            assert row_id not in spec.reference_ids


def test_sloppy_date_bounds_change_the_answer(hard_set):
    for db, spec in cases(hard_set):
        start, end = (spec.params[key] for key in TRAP_SPECS[spec.template].window)
        sql = spec.reference_sql
        assert run(db, sql) == set(spec.reference_ids)
        exclusive_start = run(db, sql.replace(f">= '{start}'", f"> '{start}'"))
        assert spec.traps["date_start"] in set(spec.reference_ids) - exclusive_start
        inclusive_end = run(db, sql.replace(f"< '{end}'", f"<= '{end}'"))
        assert spec.traps["date_end"] in inclusive_end - set(spec.reference_ids)


def test_ignoring_the_null_condition_adds_the_planted_near_misses(hard_set):
    for db, spec in cases(hard_set):
        sql = spec.reference_sql
        assert NULL_SQL[spec.template] in sql
        extra = run(db, sql.replace(NULL_SQL[spec.template], "")) - set(spec.reference_ids)
        assert set(spec.traps["null"]) <= extra
        assert len(extra) >= N_NULL_TRAPS


def test_amounts_on_the_threshold_punish_the_wrong_inequality(hard_set):
    for db, spec in cases(hard_set):
        reference = set(spec.reference_ids)
        if spec.template == "T1":  # "greater than A": a best approved refund of exactly A is out
            amount = spec.params["refund_above"]
            sloppy = run(
                db, spec.reference_sql.replace(f"r.amount > {amount}", f"r.amount >= {amount}")
            )
            assert spec.traps["amount"] in sloppy - reference
        elif spec.template == "T3":  # "at least L": an amount of exactly L is in
            amount = spec.params["min_amount"]
            sloppy = run(
                db, spec.reference_sql.replace(f"pr.amount >= {amount}", f"pr.amount > {amount}")
            )
            assert spec.traps["amount"] in reference - sloppy


def test_moved_rows_keep_their_own_dates_in_order(hard_set):
    for db, spec in cases(hard_set):
        trap = TRAP_SPECS[spec.template]
        if trap.closing_column is None:
            continue
        table = TEMPLATES_BY_NAME[spec.template].answer_table
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute(
                f"SELECT {trap.date_column}, {trap.closing_column} FROM {table} "
                f"WHERE {trap.closing_column} IS NOT NULL"
            ).fetchall()
        assert all(opened <= closing_day for opened, closing_day in rows)


def test_hard_generation_is_deterministic_and_differs_from_base():
    first = generate_task(1, 0, T3, "hard")
    assert generate_task(1, 0, T3, "hard") == first
    base = generate_task(1, 0, T3)
    assert base.difficulty == "base" and base.traps is None
    assert first.fingerprint != base.fingerprint


def test_a_hard_t1_task_through_the_python_evaluator():
    spec = generate_task(0, 0, T1, "hard")
    assert spec.n_answer == len(spec.reference_ids) >= 5
    assert spec.traps["amount"] not in spec.reference_ids


def test_day_level_windows_stay_inside_the_data():
    rng = random.Random(0)
    for _ in range(200):
        start, end = day_level_window("2025-03-01", "2026-01-01", rng)
        assert "2025-03-02" <= start <= "2025-03-28"
        assert end <= DATE_MAX.isoformat() and date.fromisoformat(end).day != 1
