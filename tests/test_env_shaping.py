"""What the agent sees: the result table, both truncations, the runaway guard, and the save tool."""

import time

import pytest

from swarm.env.database import CUT_MARK, MAX_RESULT_CHARS, MAX_ROWS, TaskEnv, render_rows
from swarm.llm.loop import ToolError

TASK = "task_0000"


def query(env: TaskEnv, sql: str) -> str:
    return env.call("query_database", {"sql": sql})


@pytest.fixture
def env(task_dir):
    with TaskEnv.open(task_dir, TASK) as env:
        yield env


# The result table ----------------------------------------------------------------------------


def test_the_table_format():
    assert render_rows(["id", "closed_at"], [(3, None), (6, 2.5)], 2) == (
        "id | closed_at\n3 | NULL\n6 | 2.5\n(2 rows)"
    )
    assert render_rows(["n"], [(1,)], 1) == "n\n1\n(1 row)"
    assert render_rows(["id"], [], 0) == "id\n(0 rows)"
    assert (
        render_rows(["body"], [("line one\nline two",)], 1) == "body\nline one\\nline two\n(1 row)"
    )
    assert render_rows(["data"], [(b"\x00\x01",)], 1) == "data\n<blob 2 bytes>\n(1 row)"


def test_the_row_cap_truncates_and_says_so(env):
    result = query(env, "SELECT id FROM case_notes")
    lines = result.splitlines()
    assert len(lines) == 1 + MAX_ROWS + 1
    assert lines[1:-1] == [str(i) for i in range(1, MAX_ROWS + 1)]
    assert lines[-1] == "(showing 50 of 1800 rows …truncated, 1750 more rows)"


def test_the_char_cap_drops_rows_and_says_so(env):
    result = query(env, "SELECT * FROM case_notes")
    assert len(result) <= MAX_RESULT_CHARS
    shown = len(result.splitlines()) - 2
    assert 0 < shown < MAX_ROWS
    assert result.splitlines()[-1] == (
        f"(showing {shown} of 1800 rows …truncated at 4,000 characters, {1800 - shown} more rows)"
    )


def test_a_single_long_cell_is_cut(env):
    result = query(env, "SELECT group_concat(id) AS ids FROM case_notes")
    assert len(result) == MAX_RESULT_CHARS
    body, status = result.rsplit("\n", 1)
    assert body.startswith("ids\n1,2,3,") and body.endswith(CUT_MARK)
    assert status == "(1 row …truncated at 4,000 characters)"


def test_the_total_row_count_is_exact_past_the_caps(env):
    assert query(env, "SELECT COUNT(*) AS n FROM case_notes").splitlines()[1] == "1800"
    assert query(env, "SELECT id FROM support_cases").endswith("750 more rows)")


# Errors and the runaway guard ----------------------------------------------------------------


def test_a_cross_join_is_aborted_quickly_and_logged(env):
    sql = "SELECT * FROM case_notes CROSS JOIN support_cases"
    started = time.perf_counter()
    with pytest.raises(ToolError) as error:
        query(env, sql)
    assert time.perf_counter() - started < 2.0
    assert str(error.value) == "error: query aborted: too expensive"
    assert env.log[-1]["result"] == "error: query aborted: too expensive"
    [event] = env.events
    assert event.kind == "query_abort" and event.detail["sql"] == sql
    assert env.stats().query_aborts == 1


def test_an_endless_recursive_query_is_aborted(env):
    with pytest.raises(ToolError, match="too expensive"):
        query(
            env, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c"
        )


def test_a_cheap_aggregate_over_a_cross_join_still_runs(env):
    """The guard counts VM steps, so an aggregate over 1.4M pairs (about 2.9M steps) runs."""
    assert (
        query(env, "SELECT COUNT(*) AS n FROM case_notes, support_cases") == "n\n1440000\n(1 row)"
    )


def test_every_reference_query_fits_the_budget(task_dir):
    for task_id in [f"task_{i:04d}" for i in range(8)]:
        with TaskEnv.open(task_dir, task_id) as env:
            result = query(env, env.spec.reference_sql)
            assert [int(line) for line in result.splitlines()[1:-1]] == env.spec.reference_ids


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT nope FROM customers", "no such column"),
        ("SELECT 1; SELECT 2", "one statement at a time"),
        ("SELECT zeroblob(500000000)", "too big"),
        ("SELECT length(randomblob(200000))", "too big"),
    ],
)
def test_sql_errors_come_back_as_error_results(env, sql, message):
    with pytest.raises(ToolError, match=message):
        query(env, sql)
    assert env.log[-1]["result"].startswith("error: ")
    assert [(event.kind, event.detail["sql"]) for event in env.events] == [("query_error", sql)]


@pytest.mark.parametrize("arguments", [{}, {"sql": ""}, {"sql": "   "}, {"sql": 5}, {"query": "x"}])
def test_query_database_needs_a_sql_string(env, arguments):
    with pytest.raises(ToolError, match="needs one argument, sql"):
        env.call("query_database", arguments)
    assert env.log[-1]["arguments"] == arguments
    assert env.query_count == 1


# Saving --------------------------------------------------------------------------------------


def test_save_works_once(env):
    assert env.call("save_final_answer", {"ids": [5, 3, 5]}) == "saved 2 ids (1 duplicate ignored)"
    assert env.saved_ids == [3, 5]
    with pytest.raises(ToolError, match="answer already saved"):
        env.call("save_final_answer", {"ids": [1, 2, 3]})
    assert env.saved_ids == [3, 5]
    assert env.log[-1]["result"] == "error: answer already saved"
    assert [event.kind for event in env.events] == ["save_repeat"]


def test_an_empty_list_is_an_answer(env):
    assert env.call("save_final_answer", {"ids": []}) == "saved 0 ids"
    assert env.saved_ids == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"ids": ["12"]},
        {"ids": [1.5]},
        {"ids": [True]},
        {"ids": "1,2"},
        {"ids": None},
        {"ids": {"a": 1}},
        {},
        {"ids": [1], "extra": 2},
    ],
)
def test_bad_ids_are_rejected_and_nothing_is_saved(env, arguments):
    with pytest.raises(ToolError, match="list of integers"):
        env.call("save_final_answer", arguments)
    assert env.saved_ids is None
    assert [event.kind for event in env.events] == ["save_invalid"]
    # A rejected call does not use up the one save.
    assert env.call("save_final_answer", {"ids": [1]}) == "saved 1 id"
