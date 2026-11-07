"""The "hard" difficulty: traps planted into a task's rows after its parameters are drawn.

A hard task asks the same question as a base task, but its data punishes sloppy SQL. After the
template's parameters are drawn, harden():
1. moves both ends of the date window off month starts onto random days (day-level windows);
2. plants traps by editing existing rows, so row counts never change:
   - date edges: of the rows that match everything except the date, one moves onto the window's
     first day (it joins the answer; `> start` would miss it) and one onto the window's end (it
     stays out; an inclusive `BETWEEN start AND end` would let it in);
   - NULL near misses: three more such rows move inside the window and break the NULL condition
     (T1: their refunds over the amount lose the approval date; T2: the open cases they wrote on
     get a closed date; T3: a need-by date; T4: a resolution date), so they match everything
     except that condition; ignoring it adds at least three wrong IDs;
   - an amount equal to the threshold (T1, T3): a row that fails only the amount gets exactly the
     threshold. T1 asks for a refund "greater than" A, so a case whose best approved refund is
     exactly A stays out (`>=` would let it in); T3 asks for an amount "of at least" L, so a
     request of exactly L joins the answer (`>` would miss it).
traps_bite() then checks that the sloppy reading of every trapped condition (start excluded, end
included, the other inequality, the NULL condition dropped) changes the answer. generate.py
rejects a draw whose traps cannot be planted or do not all bite, and draws again.

Moved rows keep their own dates consistent: a closing date (closed_at, resolved_at, needed_by)
never ends up before the row's new date.
"""

import copy
import dataclasses
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from swarm.tasks.schema import DATE_MAX, Tables
from swarm.tasks.templates import Params, Template

N_NULL_TRAPS = 3
Update = tuple[str, int, str, Any]  # (table, row id, column, new value)
# Plants the trap on one row (by id) and returns the edits; it may read the current rows.
Planter = Callable[[int, Params, Tables, random.Random], list[Update]]


@dataclass(frozen=True)
class AmountTrap:
    filter: str  # the template filter the amount belongs to
    key: str  # the params key of the threshold
    strict: bool  # True: "greater than" (T1); False: "at least" (T3)
    plant: Planter


@dataclass(frozen=True)
class TrapSpec:
    window: tuple[str, str]  # params keys of the window: (start, end)
    date_filter: str
    date_column: str  # on the answer table
    closing_column: str | None  # a same-row date that must not precede date_column
    null_filter: str
    break_null: Planter
    amount: AmountTrap | None = None
    plant: bool = True  # False: day-level window only, no planted rows (see A3)


# Per-template planters ----------------------------------------------------------------------


def _t1_break_null(case_id: int, p: Params, tables: Tables, rng: random.Random) -> list[Update]:
    """The case keeps its refunds over the amount, but none of them has an approval date."""
    updates: list[Update] = []
    for refund in tables["refunds"]:
        if (
            refund["case_id"] == case_id
            and refund["amount"] > p["refund_above"]
            and refund["approved_at"] is not None
        ):
            updates.append(("refunds", refund["id"], "approved_at", None))
            updates.append(("refunds", refund["id"], "approved_by_employee_id", None))
    return updates


def _t2_break_null(employee_id: int, p: Params, tables: Tables, rng: random.Random) -> list[Update]:
    """Every open case the employee wrote a matching note on gets a closed date."""
    cases = {case["id"]: case for case in tables["support_cases"]}
    customers = {customer["id"]: customer for customer in tables["customers"]}
    to_close: dict[int, str] = {}
    for note in tables["case_notes"]:
        if note["author_employee_id"] != employee_id or note["note_type"] != p["note_type"]:
            continue
        case = cases[note["case_id"]]
        if (
            case["severity"] >= p["min_severity"]
            and case["closed_at"] is None
            and customers[case["customer_id"]]["region"] == p["region"]
            and case["id"] not in to_close
        ):
            to_close[case["id"]] = _days_after(case["opened_at"], 60, rng)
    return [("support_cases", case_id, "closed_at", day) for case_id, day in to_close.items()]


def _t3_break_null(request_id: int, p: Params, tables: Tables, rng: random.Random) -> list[Update]:
    request = _row(tables, "procurement_requests", request_id)
    return [
        (
            "procurement_requests",
            request_id,
            "needed_by",
            _days_after(request["requested_at"], 120, rng),
        )
    ]


def _t4_break_null(ticket_id: int, p: Params, tables: Tables, rng: random.Random) -> list[Update]:
    ticket = _row(tables, "facility_tickets", ticket_id)
    return [
        ("facility_tickets", ticket_id, "resolved_at", _days_after(ticket["opened_at"], 30, rng))
    ]


def _t1_amount_on_threshold(
    case_id: int, p: Params, tables: Tables, rng: random.Random
) -> list[Update]:
    """The case's largest approved refund (all are at most A) becomes exactly A."""
    approved = [
        refund
        for refund in tables["refunds"]
        if refund["case_id"] == case_id and refund["approved_at"] is not None
    ]
    refund = max(approved, key=lambda r: (r["amount"], r["id"]))
    return [("refunds", refund["id"], "amount", float(p["refund_above"]))]


def _t3_amount_on_threshold(
    request_id: int, p: Params, tables: Tables, rng: random.Random
) -> list[Update]:
    return [("procurement_requests", request_id, "amount", float(p["min_amount"]))]


TRAP_SPECS: dict[str, TrapSpec] = {
    "T1": TrapSpec(
        window=("opened_from", "opened_before"),
        date_filter="opened",
        date_column="opened_at",
        closing_column="closed_at",
        null_filter="refund_approved",
        break_null=_t1_break_null,
        amount=AmountTrap(
            filter="refund_amount", key="refund_above", strict=True, plant=_t1_amount_on_threshold
        ),
    ),
    "T2": TrapSpec(
        window=("hired_from", "hired_before"),
        date_filter="hired",
        date_column="hired_at",
        closing_column=None,
        null_filter="no_closed_date",
        break_null=_t2_break_null,
    ),
    "T3": TrapSpec(
        window=("requested_from", "requested_before"),
        date_filter="requested",
        date_column="requested_at",
        closing_column="needed_by",
        null_filter="no_need_by",
        break_null=_t3_break_null,
        amount=AmountTrap(
            filter="amount", key="min_amount", strict=False, plant=_t3_amount_on_threshold
        ),
    ),
    "T4": TrapSpec(
        window=("opened_from", "opened_before"),
        date_filter="opened",
        date_column="opened_at",
        closing_column="resolved_at",
        null_filter="no_resolution_date",
        break_null=_t4_break_null,
    ),
}

# The aggregation templates (difficulty "agg") ask T1-T3's questions with one aggregate
# condition; their filters keep T1-T3's names, so the same planters apply.
TRAP_SPECS["A1"] = TRAP_SPECS["T1"]
TRAP_SPECS["A2"] = TRAP_SPECS["T2"]
# A3 plants nothing: its date-edge rows would need a requester whose window total already
# reaches X from other requests, which almost never exists (46 of 50 draws had too few).
TRAP_SPECS["A3"] = dataclasses.replace(TRAP_SPECS["T3"], plant=False)


# Hardening ----------------------------------------------------------------------------------


def harden(
    template: Template, params: Params, tables: Tables, rng: random.Random
) -> tuple[Params, list[Update], dict[str, Any]] | None:
    """Day-level window plus planted traps for one parameter draw.

    Returns (new params, the row edits in order, which rows carry which trap), or None when this
    draw has too few rows to plant on. `tables` is not modified.
    """
    spec = TRAP_SPECS[template.name]
    start_key, end_key = spec.window
    p = dict(params)
    p[start_key], p[end_key] = day_level_window(p[start_key], p[end_key], rng)
    if not spec.plant:
        return p, [], {}
    work = copy.deepcopy(tables)
    updates: list[Update] = []

    def plant(edits: list[Update]) -> None:
        apply_updates_to_tables(work, edits)
        updates.extend(edits)

    def move(row_id: int, day: str) -> list[Update]:
        row = _row(work, template.answer_table, row_id)
        edits: list[Update] = [(template.answer_table, row_id, spec.date_column, day)]
        closing = spec.closing_column
        if closing is not None and row[closing] is not None and row[closing] < day:
            edits.append((template.answer_table, row_id, closing, day))
        return edits

    answer = template.evaluate_py(p, work)
    date_misses = sorted(template.evaluate_py(p, work, spec.date_filter) - answer)
    if len(date_misses) < 2 + N_NULL_TRAPS:
        return None
    on_start, on_end, *null_rows = rng.sample(date_misses, 2 + N_NULL_TRAPS)
    plant(move(on_start, p[start_key]))
    plant(move(on_end, p[end_key]))
    for row_id in null_rows:
        plant(move(row_id, _day_inside(p[start_key], p[end_key], rng)))
        plant(spec.break_null(row_id, p, work, rng))
    traps: dict[str, Any] = {"date_start": on_start, "date_end": on_end, "null": sorted(null_rows)}

    if spec.amount is not None:
        answer = template.evaluate_py(p, work)
        amount_misses = sorted(template.evaluate_py(p, work, spec.amount.filter) - answer)
        if not amount_misses:
            return None
        row_id = rng.choice(amount_misses)
        plant(spec.amount.plant(row_id, p, work, rng))
        traps["amount"] = row_id
    return p, updates, traps


def traps_bite(template: Template, params: Params, tables: Tables, answer: set[int]) -> bool:
    """True when the sloppy reading of every trapped condition gives a different answer."""
    spec = TRAP_SPECS[template.name]
    if not spec.plant:
        return True
    start_key, end_key = spec.window

    def changes(**changed: Any) -> bool:
        return template.evaluate_py({**params, **changed}, tables) != answer

    if not changes(**{start_key: _next_day(params[start_key])}):  # "> start" for "on or after"
        return False
    if not changes(**{end_key: _next_day(params[end_key])}):  # "<= end" for "before"
        return False
    if spec.amount is not None:
        threshold = params[spec.amount.key]
        # T1 "greater than A" read as ">= A"; T3 "at least L" read as "> L" (amounts have cents).
        sloppy = threshold - 0.005 if spec.amount.strict else threshold + 0.005
        if not changes(**{spec.amount.key: sloppy}):
            return False
    near_misses = template.evaluate_py(params, tables, spec.null_filter) - answer
    return len(near_misses) >= N_NULL_TRAPS


def apply_updates_to_tables(tables: Tables, updates: list[Update]) -> None:
    for table, row_id, column, value in updates:
        _row(tables, table, row_id)[column] = value


def day_level_window(start: str, end: str, rng: random.Random) -> tuple[str, str]:
    """Move a month-boundary window [start, end) onto random days: each end moves 1-27 days
    later (never onto a 1st), and the end stays inside the data (at most DATE_MAX), so rows
    can sit on both edges and some data always lies before the start."""
    new_start = date.fromisoformat(start) + timedelta(days=rng.randint(1, 27))
    new_end = min(date.fromisoformat(end) + timedelta(days=rng.randint(1, 27)), DATE_MAX)
    return new_start.isoformat(), new_end.isoformat()


def _day_inside(start: str, end: str, rng: random.Random) -> str:
    """A day strictly after `start` and strictly before `end`."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return (first + timedelta(days=rng.randint(1, (last - first).days - 1))).isoformat()


def _days_after(day: str, max_days: int, rng: random.Random) -> str:
    """A day 0..max_days after `day`, never past DATE_MAX (as the base data does)."""
    moved = date.fromisoformat(day) + timedelta(days=rng.randrange(max_days + 1))
    return min(moved, DATE_MAX).isoformat()


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _row(tables: Tables, table: str, row_id: int) -> dict[str, Any]:
    """Rows are loaded ordered by id, and ids run 1..n, so the row with id i sits at i - 1."""
    row = tables[table][row_id - 1]
    if row["id"] != row_id:
        raise RuntimeError(f"{table} rows are not in id order (wanted {row_id}, got {row['id']})")
    return row
