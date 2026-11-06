"""Query templates: sampled parameters -> reference SQL, request text, and an independent check.

Each template answers one question shape over the fixed schema. Given sampled parameters it
produces:
- `sql(params)`: the reference query (stored literals, runnable as-is);
- `nl(params)`: the request an agent sees (plain words, explicit bounds, no schema names);
- `evaluate_py(params, tables, drop=None)`: the same question answered in plain Python over the
  loaded rows, with no SQL. `drop` names one filter to leave out; generation uses it to check
  that every filter binds (dropping any one of them changes the answer).

Every template has at least one date window, one numeric threshold, one categorical filter and
one NULL/non-NULL condition. Request wording rules: dates as "on or after <d1> and before <d2>",
thresholds as "of at least <n>" or "greater than <A>", NULL conditions as "with no <...> recorded",
enum values in their display form, and the request ends with "Return the IDs of the matching
<rows>."
"""

import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from swarm.tasks.schema import NOTE_TYPES, REGIONS, TIERS, Tables

FilterKind = Literal["date_window", "numeric", "categorical", "null"]
Params = dict[str, Any]


@dataclass(frozen=True)
class Filter:
    name: str
    kind: FilterKind


@dataclass(frozen=True)
class Template:
    name: str  # "T1" .. "T4"
    slug: str
    answer_table: str
    answer_noun: str  # how the request names the answer rows
    joins: int  # other tables the reference query has to reach
    difficulty: str
    filters: tuple[Filter, ...]
    sample_params: Callable[[random.Random, Tables], Params]
    sql: Callable[[Params], str]
    nl: Callable[[Params], str]
    evaluate_py: Callable[..., set[int]]  # (params, tables, drop=None) -> ids

    @property
    def label(self) -> str:
        return f"{self.name} {self.slug}"


# Helpers -------------------------------------------------------------------------------------

_MONTH_STARTS = [date(2024 + m // 12, m % 12 + 1, 1).isoformat() for m in range(25)]  # ..2026-01


def month_window(rng: random.Random, min_months: int, max_months: int) -> tuple[str, str]:
    """[start, end) on month boundaries inside 2024-01-01 .. 2026-01-01."""
    length = rng.randint(min_months, max_months)
    start = rng.randrange(0, len(_MONTH_STARTS) - length)
    return _MONTH_STARTS[start], _MONTH_STARTS[start + length]


def _q(value: str) -> str:
    """A SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _in_list(values: Iterable[str]) -> str:
    return ", ".join(_q(v) for v in values)


def _or_words(words: list[str]) -> str:
    """["a"] -> "a"; ["a", "b"] -> "a or b"; ["a", "b", "c"] -> "a, b or c"."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"


def _by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {row["id"]: row for row in rows}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return grouped


def _in_window(value: str, start: str, end: str) -> bool:
    return start <= value < end  # ISO dates compare correctly as text


# T1 cases_with_refund_and_note ---------------------------------------------------------------


def _t1_sample(rng: random.Random, tables: Tables) -> Params:
    opened_from, opened_before = month_window(rng, 4, 18)
    tiers = rng.sample(list(TIERS), rng.choice([1, 2]))
    return {
        "tiers": [t for t in TIERS if t in tiers],
        "opened_from": opened_from,
        "opened_before": opened_before,
        "min_severity": rng.choice([2, 3]),
        "note_type": rng.choice(list(NOTE_TYPES)),
        "refund_above": rng.choice([100, 250, 500, 750, 1000]),
    }


def _t1_sql(p: Params) -> str:
    return f"""SELECT sc.id
FROM support_cases AS sc
JOIN customers AS c ON c.id = sc.customer_id
WHERE c.tier IN ({_in_list(p["tiers"])})
  AND sc.opened_at >= {_q(p["opened_from"])} AND sc.opened_at < {_q(p["opened_before"])}
  AND sc.severity >= {p["min_severity"]}
  AND EXISTS (
    SELECT 1 FROM case_notes AS n
    WHERE n.case_id = sc.id AND n.note_type = {_q(p["note_type"])}
  )
  AND EXISTS (
    SELECT 1 FROM refunds AS r
    WHERE r.case_id = sc.id AND r.amount > {p["refund_above"]} AND r.approved_at IS NOT NULL
  )
ORDER BY sc.id"""


def _t1_nl(p: Params) -> str:
    tiers = _or_words([TIERS[t] for t in p["tiers"]])
    return (
        f"Find the support cases opened on or after {p['opened_from']} and before "
        f"{p['opened_before']} with a severity of at least {p['min_severity']}, for customers on "
        f"the {tiers} tier, that have at least one {NOTE_TYPES[p['note_type']]} note and at least "
        f"one refund greater than {p['refund_above']} with an approval date recorded. "
        "Return the IDs of the matching support cases."
    )


def _t1_eval(p: Params, tables: Tables, drop: str | None = None) -> set[int]:
    customers = _by_id(tables["customers"])
    notes = _group(tables["case_notes"], "case_id")
    refunds = _group(tables["refunds"], "case_id")
    found = set()
    for case in tables["support_cases"]:
        if drop != "tier" and customers[case["customer_id"]]["tier"] not in p["tiers"]:
            continue
        if drop != "opened" and not _in_window(
            case["opened_at"], p["opened_from"], p["opened_before"]
        ):
            continue
        if drop != "severity" and case["severity"] < p["min_severity"]:
            continue
        if drop != "note_type" and not any(
            note["note_type"] == p["note_type"] for note in notes[case["id"]]
        ):
            continue
        if not any(
            (drop == "refund_amount" or refund["amount"] > p["refund_above"])
            and (drop == "refund_approved" or refund["approved_at"] is not None)
            for refund in refunds[case["id"]]
        ):
            continue
        found.add(case["id"])
    return found


T1 = Template(
    name="T1",
    slug="cases_with_refund_and_note",
    answer_table="support_cases",
    answer_noun="support cases",
    joins=3,
    difficulty="standard",
    filters=(
        Filter("opened", "date_window"),
        Filter("severity", "numeric"),
        Filter("tier", "categorical"),
        Filter("note_type", "categorical"),
        Filter("refund_amount", "numeric"),
        Filter("refund_approved", "null"),
    ),
    sample_params=_t1_sample,
    sql=_t1_sql,
    nl=_t1_nl,
    evaluate_py=_t1_eval,
)


# T2 note_authors -----------------------------------------------------------------------------


def _t2_sample(rng: random.Random, tables: Tables) -> Params:
    hired_from, hired_before = month_window(rng, 4, 16)
    return {
        "hired_from": hired_from,
        "hired_before": hired_before,
        "note_type": rng.choice(list(NOTE_TYPES)),
        "min_severity": rng.choice([2, 3]),
        "region": rng.choice(list(REGIONS)),
    }


def _t2_sql(p: Params) -> str:
    return f"""SELECT e.id
FROM employees AS e
WHERE e.hired_at >= {_q(p["hired_from"])} AND e.hired_at < {_q(p["hired_before"])}
  AND EXISTS (
    SELECT 1
    FROM case_notes AS n
    JOIN support_cases AS sc ON sc.id = n.case_id
    JOIN customers AS c ON c.id = sc.customer_id
    WHERE n.author_employee_id = e.id
      AND n.note_type = {_q(p["note_type"])}
      AND sc.severity >= {p["min_severity"]}
      AND sc.closed_at IS NULL
      AND c.region = {_q(p["region"])}
  )
ORDER BY e.id"""


def _t2_nl(p: Params) -> str:
    return (
        f"Find the employees hired on or after {p['hired_from']} and before {p['hired_before']} "
        f"who wrote at least one {NOTE_TYPES[p['note_type']]} note on a support case that has a "
        f"severity of at least {p['min_severity']}, has no closed date recorded, and belongs to a "
        f"customer in the {REGIONS[p['region']]} region. "
        "Return the IDs of the matching employees."
    )


def _t2_eval(p: Params, tables: Tables, drop: str | None = None) -> set[int]:
    cases = _by_id(tables["support_cases"])
    customers = _by_id(tables["customers"])
    notes_by_author = _group(tables["case_notes"], "author_employee_id")

    def qualifies(note: dict[str, Any]) -> bool:
        case = cases[note["case_id"]]
        return (
            (drop == "note_type" or note["note_type"] == p["note_type"])
            and (drop == "severity" or case["severity"] >= p["min_severity"])
            and (drop == "no_closed_date" or case["closed_at"] is None)
            and (drop == "region" or customers[case["customer_id"]]["region"] == p["region"])
        )

    found = set()
    for employee in tables["employees"]:
        if drop != "hired" and not _in_window(
            employee["hired_at"], p["hired_from"], p["hired_before"]
        ):
            continue
        if any(qualifies(note) for note in notes_by_author[employee["id"]]):
            found.add(employee["id"])
    return found


T2 = Template(
    name="T2",
    slug="note_authors",
    answer_table="employees",
    answer_noun="employees",
    joins=3,
    difficulty="standard",
    filters=(
        Filter("hired", "date_window"),
        Filter("note_type", "categorical"),
        Filter("severity", "numeric"),
        Filter("no_closed_date", "null"),
        Filter("region", "categorical"),
    ),
    sample_params=_t2_sample,
    sql=_t2_sql,
    nl=_t2_nl,
    evaluate_py=_t2_eval,
)


# T3 risky_procurement ------------------------------------------------------------------------


def _t3_sample(rng: random.Random, tables: Tables) -> Params:
    requested_from, requested_before = month_window(rng, 6, 18)
    names = [department["name"] for department in tables["departments"]]
    chosen = rng.sample(names, 2)
    return {
        "requested_from": requested_from,
        "requested_before": requested_before,
        "min_amount": rng.choice([5000, 10000, 20000]),
        "min_risk": rng.choice([2, 3, 4]),
        "departments": [name for name in names if name in chosen],
    }


def _t3_sql(p: Params) -> str:
    return f"""SELECT pr.id
FROM procurement_requests AS pr
JOIN vendors AS v ON v.id = pr.vendor_id
JOIN employees AS e ON e.id = pr.requested_by_employee_id
JOIN departments AS d ON d.id = e.department_id
WHERE pr.requested_at >= {_q(p["requested_from"])}
  AND pr.requested_at < {_q(p["requested_before"])}
  AND pr.amount >= {p["min_amount"]}
  AND v.risk_rating >= {p["min_risk"]}
  AND d.name IN ({_in_list(p["departments"])})
  AND pr.needed_by IS NULL
ORDER BY pr.id"""


def _t3_nl(p: Params) -> str:
    return (
        f"Find the procurement requests with a request date on or after {p['requested_from']} "
        f"and before {p['requested_before']}, for an amount of at least {p['min_amount']}, from "
        f"vendors with a risk rating of at least {p['min_risk']}, requested by an employee in the "
        f"{_or_words(p['departments'])} department, with no need-by date recorded. "
        "Return the IDs of the matching procurement requests."
    )


def _t3_eval(p: Params, tables: Tables, drop: str | None = None) -> set[int]:
    vendors = _by_id(tables["vendors"])
    employees = _by_id(tables["employees"])
    departments = _by_id(tables["departments"])
    found = set()
    for request in tables["procurement_requests"]:
        department = departments[employees[request["requested_by_employee_id"]]["department_id"]]
        if drop != "requested" and not _in_window(
            request["requested_at"], p["requested_from"], p["requested_before"]
        ):
            continue
        if drop != "amount" and request["amount"] < p["min_amount"]:
            continue
        if drop != "risk" and vendors[request["vendor_id"]]["risk_rating"] < p["min_risk"]:
            continue
        if drop != "department" and department["name"] not in p["departments"]:
            continue
        if drop != "no_need_by" and request["needed_by"] is not None:
            continue
        found.add(request["id"])
    return found


T3 = Template(
    name="T3",
    slug="risky_procurement",
    answer_table="procurement_requests",
    answer_noun="procurement requests",
    joins=3,
    difficulty="standard",
    filters=(
        Filter("requested", "date_window"),
        Filter("amount", "numeric"),
        Filter("risk", "numeric"),
        Filter("department", "categorical"),
        Filter("no_need_by", "null"),
    ),
    sample_params=_t3_sample,
    sql=_t3_sql,
    nl=_t3_nl,
    evaluate_py=_t3_eval,
)


# T4 open_tickets (the easy template: one join) -----------------------------------------------


def _t4_sample(rng: random.Random, tables: Tables) -> Params:
    opened_from, opened_before = month_window(rng, 4, 18)
    cities = sorted({building["city"] for building in tables["buildings"]})
    return {
        "city": rng.choice(cities),
        "opened_from": opened_from,
        "opened_before": opened_before,
        "min_priority": rng.choice([2, 3]),
    }


def _t4_sql(p: Params) -> str:
    return f"""SELECT t.id
FROM facility_tickets AS t
JOIN buildings AS b ON b.id = t.building_id
WHERE b.city = {_q(p["city"])}
  AND t.opened_at >= {_q(p["opened_from"])} AND t.opened_at < {_q(p["opened_before"])}
  AND t.priority >= {p["min_priority"]}
  AND t.resolved_at IS NULL
ORDER BY t.id"""


def _t4_nl(p: Params) -> str:
    return (
        f"Find the facility tickets for buildings located in {p['city']}, opened on or after "
        f"{p['opened_from']} and before {p['opened_before']}, with a priority of at least "
        f"{p['min_priority']} and with no resolution date recorded. "
        "Return the IDs of the matching facility tickets."
    )


def _t4_eval(p: Params, tables: Tables, drop: str | None = None) -> set[int]:
    buildings = _by_id(tables["buildings"])
    found = set()
    for ticket in tables["facility_tickets"]:
        if drop != "city" and buildings[ticket["building_id"]]["city"] != p["city"]:
            continue
        if drop != "opened" and not _in_window(
            ticket["opened_at"], p["opened_from"], p["opened_before"]
        ):
            continue
        if drop != "priority" and ticket["priority"] < p["min_priority"]:
            continue
        if drop != "no_resolution_date" and ticket["resolved_at"] is not None:
            continue
        found.add(ticket["id"])
    return found


T4 = Template(
    name="T4",
    slug="open_tickets",
    answer_table="facility_tickets",
    answer_noun="facility tickets",
    joins=1,
    difficulty="easy",
    filters=(
        Filter("city", "categorical"),
        Filter("opened", "date_window"),
        Filter("priority", "numeric"),
        Filter("no_resolution_date", "null"),
    ),
    sample_params=_t4_sample,
    sql=_t4_sql,
    nl=_t4_nl,
    evaluate_py=_t4_eval,
)


TEMPLATES: tuple[Template, ...] = (T1, T2, T3, T4)
TEMPLATES_BY_NAME = {template.name: template for template in TEMPLATES}
