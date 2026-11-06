"""The fixed schema: valid foreign keys, NULL rates, value domains, determinism, schema dump."""

import re
from datetime import date

import pytest

from swarm.tasks.schema import (
    ALL_TABLES,
    DATE_MAX,
    DATE_MIN,
    ENUMS,
    NULL_RATES,
    ROW_COUNTS,
    build_database,
    load_tables,
    schema_dump,
)

SEED = 12345
DATE_COLUMNS = {
    "customers": ["created_at"],
    "support_cases": ["opened_at", "closed_at"],
    "case_notes": ["created_at"],
    "refunds": ["approved_at"],
    "employees": ["hired_at"],
    "procurement_requests": ["requested_at", "needed_by"],
    "facility_tickets": ["opened_at", "resolved_at"],
}


@pytest.fixture(scope="module")
def conn():
    connection = build_database(SEED)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def tables(conn):
    return load_tables(conn)


def test_row_counts_match_the_defaults(tables):
    assert {table: len(rows) for table, rows in tables.items()} == ROW_COUNTS


def test_every_foreign_key_points_at_an_existing_row(conn, tables):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    ids = {table: {row["id"] for row in rows} for table, rows in tables.items()}
    checked = 0
    for table in tables:
        nullable = {
            name: not not_null
            for _, name, _, not_null, _, _ in conn.execute(f"PRAGMA table_info({table})")
        }
        for _, _, parent, column, _, *_ in conn.execute(f"PRAGMA foreign_key_list({table})"):
            checked += 1
            for row in tables[table]:
                if row[column] is None:
                    assert nullable[column], f"{table}.{column} is NULL"
                else:
                    assert row[column] in ids[parent], f"{table}.{column}={row[column]}"
    assert checked == 11  # every reference in the schema was checked


@pytest.mark.parametrize(("table", "column"), list(NULL_RATES))
def test_null_rates_are_within_10_points_of_their_targets(tables, table, column):
    rows = tables[table]
    rate = sum(row[column] is None for row in rows) / len(rows)
    assert abs(rate - NULL_RATES[(table, column)]) <= 0.10, f"{table}.{column}: {rate:.2f}"


def test_a_refund_has_an_approver_exactly_when_it_has_an_approval_date(tables):
    for refund in tables["refunds"]:
        assert (refund["approved_at"] is None) == (refund["approved_by_employee_id"] is None)


def test_values_stay_in_their_domains(tables):
    for key, enum in ENUMS.items():
        table, column = key.split(".")
        assert {row[column] for row in tables[table]} <= set(enum), key
    for table, column, low, high in [
        ("support_cases", "severity", 1, 4),
        ("facility_tickets", "priority", 1, 3),
        ("vendors", "risk_rating", 1, 5),
    ]:
        assert {row[column] for row in tables[table]} == set(range(low, high + 1))
    assert all(10 <= refund["amount"] <= 2000 for refund in tables["refunds"])
    assert all(500 <= r["amount"] <= 50000 for r in tables["procurement_requests"])


def test_dates_are_iso_inside_the_span_and_in_a_sensible_order(tables):
    for table, columns in DATE_COLUMNS.items():
        for row in tables[table]:
            for column in columns:
                if row[column] is not None:
                    assert DATE_MIN <= date.fromisoformat(row[column]) <= DATE_MAX
    cases = {case["id"]: case for case in tables["support_cases"]}
    for case in cases.values():
        assert case["closed_at"] is None or case["closed_at"] >= case["opened_at"]
    for note in tables["case_notes"]:
        assert note["created_at"] >= cases[note["case_id"]]["opened_at"]
    for refund in tables["refunds"]:
        approved = refund["approved_at"]
        assert approved is None or approved >= cases[refund["case_id"]]["opened_at"]
    for request in tables["procurement_requests"]:
        assert request["needed_by"] is None or request["needed_by"] >= request["requested_at"]
    for ticket in tables["facility_tickets"]:
        assert ticket["resolved_at"] is None or ticket["resolved_at"] >= ticket["opened_at"]


def test_emails_use_reserved_example_domains(tables):
    for customer in tables["customers"]:
        assert re.fullmatch(r"[^@\s]+@example\.(com|net|org)", customer["email"])


def test_the_same_seed_builds_the_same_rows():
    assert load_tables(build_database(3)) == load_tables(build_database(3))
    assert load_tables(build_database(3)) != load_tables(build_database(4))


def test_schema_dump_is_long_and_deterministic():
    first = schema_dump(build_database(7))
    assert first == schema_dump(build_database(7))
    assert len(first) > 2000


def test_schema_dump_lists_every_table_and_hides_the_reference_row_count(conn):
    dump = schema_dump(conn)
    for table in ALL_TABLES:
        assert f"CREATE TABLE {table} (" in dump
    for table, count in ROW_COUNTS.items():
        assert f"\n{table} ({count} rows)\n" in dump
    conn.execute("INSERT INTO _reference_answers VALUES ('task_9999', 1), ('task_9999', 2)")
    tables_section = schema_dump(conn).split("-- Tables")[1]
    assert "\n_reference_answers\n" in tables_section  # listed, but without a row count
    assert "_reference_answers (" not in tables_section
    conn.execute("DELETE FROM _reference_answers")


def test_schema_dump_does_not_reveal_stored_enum_values(conn):
    dump = schema_dump(conn)
    assert "CHECK" not in dump
    for enum in ENUMS.values():
        for stored in enum:
            if "_" in stored:  # plain words like "open" also occur inside column names
                assert stored not in dump
