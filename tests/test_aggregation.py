"""The agg difficulty: A1-A3 (an aggregation across a join) plus T4 from the hard level.

Checked against the written databases: SQL and Python agree, every filter binds, relaxing the
aggregation changes the answer (also with edited SQL), the requests name no schema identifiers,
and generation is deterministic.
"""

import re
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from swarm.tasks.generate import generate_task, generate_tasks, load_task
from swarm.tasks.schema import load_tables
from swarm.tasks.templates import A2, TEMPLATES, TEMPLATES_BY_NAME
from swarm.tasks.traps import TRAP_SPECS

AGG = {"A1": "min_notes", "A2": "min_cases", "A3": "min_total"}


@pytest.fixture(scope="module")
def agg_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("tasks")
    manifest = generate_tasks(base_seed=0, n=8, out_root=out, difficulty="agg")
    return out / "seed0-agg", manifest


def specs(agg_set):
    root, manifest = agg_set
    for task in manifest.tasks:
        yield root / task.task_id / "db.sqlite", load_task(root, task.task_id)


def run(db, sql: str) -> set[int]:
    with closing(sqlite3.connect(db)) as conn:
        return {row[0] for row in conn.execute(sql)}


def tables_of(db):
    with closing(sqlite3.connect(db)) as conn:
        return load_tables(conn)


def test_the_agg_set_layout(agg_set):
    root, manifest = agg_set
    assert root.name == "seed0-agg"
    assert [task.template for task in manifest.tasks] == ["A1", "A2", "A3", "T4"] * 2
    assert manifest.row_counts["case_notes"] == 5400
    assert {task.difficulty for task in manifest.tasks} == {"agg"}
    assert [t.name for t in TEMPLATES] == ["T1", "T2", "T3", "T4"]  # base and hard unchanged


def test_sql_and_python_agree_on_the_written_databases(agg_set):
    for db, spec in specs(agg_set):
        template = TEMPLATES_BY_NAME[spec.template]
        assert run(db, spec.reference_sql) == set(spec.reference_ids)
        assert template.evaluate_py(spec.params, tables_of(db)) == set(spec.reference_ids)
        assert 5 <= spec.n_answer <= 30


def test_every_filter_binds(agg_set):
    for db, spec in specs(agg_set):
        template = TEMPLATES_BY_NAME[spec.template]
        tables = tables_of(db)
        for f in template.filters:
            assert template.evaluate_py(spec.params, tables, f.name) != set(spec.reference_ids), (
                spec.task_id,
                f.name,
            )


def test_relaxing_the_aggregation_changes_the_answer(agg_set):
    for db, spec in specs(agg_set):
        if spec.template not in AGG:
            continue
        template = TEMPLATES_BY_NAME[spec.template]
        key = AGG[spec.template]
        relaxed = template.evaluate_py({**spec.params, **template.relax}, tables_of(db))
        assert relaxed != set(spec.reference_ids)
        # The same through edited SQL: "at least two" -> "at least one"; the total dropped.
        sql = spec.reference_sql
        old = f") >= {spec.params[key]}"
        assert sql.count(old) == 1
        assert run(db, sql.replace(old, f") >= {template.relax[key]}")) == relaxed


def test_requests_name_no_schema_identifiers(agg_set):
    for _, spec in specs(agg_set):
        text = spec.nl_request
        assert not re.search(r"\b[a-z]+_[a-z_]+\b", text), text
        assert "_reference" not in text
    wording = {spec.template: spec.nl_request for _, spec in specs(agg_set)}
    assert "at least two" in wording["A1"] and "notes" in wording["A1"]
    assert "at least two different support cases" in wording["A2"]
    assert "add up to at least" in wording["A3"] and "whatever their vendor" in wording["A3"]


def test_traps_are_planted_except_for_a3(agg_set):
    for _, spec in specs(agg_set):
        trap = TRAP_SPECS[spec.template]
        start, end = (spec.params[key] for key in trap.window)
        assert date.fromisoformat(start).day != 1 and date.fromisoformat(end).day != 1
        if spec.template == "A3":
            assert spec.traps == {} and not trap.plant
        else:
            assert {"date_start", "date_end", "null"} <= set(spec.traps)
            assert spec.traps["date_start"] in spec.reference_ids
            assert spec.traps["date_end"] not in spec.reference_ids


def test_generation_is_deterministic():
    first = generate_task(5, 0, A2, "agg")
    assert generate_task(5, 0, A2, "agg") == first
    assert first.params["min_cases"] == 2
