"""The task database cannot be changed, and nothing outside the private copy can be touched."""

import hashlib
import sqlite3

import pytest

from swarm.env.database import TaskEnv
from swarm.llm.loop import ToolError

TASK = "task_0000"
WRITES = [
    "INSERT INTO customers (id, name, email, tier, region, created_at) "
    "VALUES (9999, 'x', 'x@example.com', 'basic', 'north', '2024-01-01')",
    "UPDATE customers SET tier = 'pro'",
    "DELETE FROM refunds",
    "CREATE TABLE scratch (x INTEGER)",
    "CREATE TEMP TABLE scratch (x INTEGER)",
    "CREATE VIEW recent AS SELECT * FROM support_cases",
    "DROP TABLE customers",
    "ALTER TABLE customers ADD COLUMN note TEXT",
    "VACUUM",
]


def query(env: TaskEnv, sql: str) -> str:
    return env.call("query_database", {"sql": sql})


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("sql", WRITES)
def test_a_write_fails_with_an_error_result_and_is_logged(task_dir, sql):
    with TaskEnv.open(task_dir, TASK) as env:
        with pytest.raises(ToolError, match="read-only") as error:
            query(env, sql)
        assert env.log[-1]["result"] == str(error.value)
        assert env.log[-1]["result"].startswith("error: ")
        assert [event.kind for event in env.events] == ["write_attempt"]
        assert env.events[0].detail["sql"] == sql
        assert env.stats().write_attempts == 1
        # Nothing changed: the table is still there, with all its rows.
        assert query(env, "SELECT COUNT(*) AS n FROM customers") == "n\n240\n(1 row)"


def test_the_copy_is_private_and_deleted_on_close(task_dir):
    original = task_dir / TASK / "db.sqlite"
    before = sha256(original)
    env = TaskEnv.open(task_dir, TASK)
    copy_path = env.db_path
    assert copy_path.is_file() and copy_path != original
    assert sha256(copy_path) == before  # a byte-for-byte copy
    for sql in WRITES:
        with pytest.raises(ToolError):
            query(env, sql)
    env.close()
    assert not copy_path.exists() and not copy_path.parent.exists()
    assert sha256(original) == before
    env.close()  # closing twice is harmless
    with pytest.raises(RuntimeError, match="closed"):
        query(env, "SELECT 1")


def test_attach_and_vacuum_into_cannot_touch_the_file_system(task_dir, tmp_path):
    target = tmp_path / "escape.db"
    with TaskEnv.open(task_dir, TASK) as env:
        for sql in (f"ATTACH DATABASE '{target}' AS outside", f"VACUUM INTO '{target}'"):
            with pytest.raises(ToolError, match="read-only"):
                query(env, sql)
        # Turning query_only off is refused, so it cannot be the first step of an escape.
        with pytest.raises(ToolError, match="not allowed"):
            query(env, "PRAGMA query_only = 0")
        with pytest.raises(ToolError):
            query(env, f"VACUUM INTO '{target}'")
        kinds = [event.kind for event in env.events]
    assert kinds == ["write_attempt", "write_attempt", "blocked", "write_attempt"]
    assert not target.exists()


@pytest.mark.parametrize("sql", ["BEGIN", "SAVEPOINT s1", "PRAGMA journal_mode = OFF"])
def test_other_statements_are_blocked(task_dir, sql):
    with TaskEnv.open(task_dir, TASK) as env:
        with pytest.raises(ToolError, match="not allowed"):
            query(env, sql)
        assert [(event.kind, event.detail["sql"]) for event in env.events] == [("blocked", sql)]


def test_read_only_pragmas_are_allowed(task_dir):
    with TaskEnv.open(task_dir, TASK) as env:
        result = query(env, "PRAGMA table_info(buildings)")
        assert result.splitlines()[0] == "cid | name | type | notnull | dflt_value | pk"
        assert query(env, "SELECT name FROM pragma_table_info('buildings') LIMIT 1") == (
            "name\nid\n(1 row)"
        )
        assert env.events == []


def test_the_connection_itself_is_read_only(task_dir):
    """The layer under the authorizer: with the authorizer removed, writes still fail."""
    with TaskEnv.open(task_dir, TASK) as env:
        conn = env._conn  # private on purpose: tests check this layer on its own
        conn.set_authorizer(None)
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO buildings (id, name, city) VALUES (99, 'x', 'y')")
