"""The fixed task schema: DDL, value lists, the seeded row generator, and the schema dump.

Every task database has the same ten domain tables plus `_reference_answers`. Rows are random
but fully determined by the task's seed: names, emails, company names and note bodies come from
Faker seeded with it, and every date, number and choice from `random.Random(seed)`. Foreign keys
are always valid (SQLite enforces them while the rows are inserted).

Enum columns are plain TEXT with no CHECK lists, so the schema dump does not reveal the stored
values: an agent that needs them has to look (e.g. SELECT DISTINCT). Requests describe enum
values in plain words (the display forms below), never as the stored literal.
"""

import random
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from faker import Faker

DATE_MIN = date(2024, 1, 1)
DATE_MAX = date(2025, 12, 31)

# Enums: stored value -> the plain words a request uses for it.
TIERS = {"basic": "basic", "pro": "pro", "enterprise": "enterprise"}
REGIONS = {"north": "north", "south": "south", "east": "east", "west": "west"}
CASE_STATUSES = {
    "open": "open",
    "pending": "pending",
    "resolved": "resolved",
    "escalated": "escalated",
}
NOTE_TYPES = {
    "update": "update",
    "legal_review": "legal-review",
    "refund_request": "refund-request",
    "customer_call": "customer-call",
}
ROLES = {"agent": "agent", "analyst": "analyst", "manager": "manager", "engineer": "engineer"}
PROCUREMENT_STATUSES = {
    "draft": "draft",
    "submitted": "submitted",
    "approved": "approved",
    "rejected": "rejected",
}
TICKET_CATEGORIES = {
    "hvac": "HVAC",
    "electrical": "electrical",
    "plumbing": "plumbing",
    "access": "access",
    "network": "network",
}
ENUMS: dict[str, dict[str, str]] = {
    "customers.tier": TIERS,
    "customers.region": REGIONS,
    "support_cases.status": CASE_STATUSES,
    "case_notes.note_type": NOTE_TYPES,
    "employees.role": ROLES,
    "procurement_requests.status": PROCUREMENT_STATUSES,
    "facility_tickets.category": TICKET_CATEGORIES,
}

# Fixed name lists (fictional, like everything else here).
CITIES = ("Port Alder", "Vellmoor", "Kestrel Falls", "Marrowick", "Duskwater", "Ironvale")
COUNTRIES = (
    "Arvenia",
    "Belmora",
    "Corvath",
    "Dravonia",
    "Estmark",
    "Fenwarra",
    "Galdoria",
    "Hesperon",
)
BUILDING_NAMES = (
    "Atlas House",
    "Beacon Hall",
    "Cedar Court",
    "Delta Works",
    "Elm Annex",
    "Foundry Point",
)
DEPARTMENT_NAMES = (
    "Customer Support",
    "Billing",
    "Legal",
    "Facilities",
    "Procurement",
    "Engineering",
    "Operations",
    "Quality Assurance",
)

# Default rows per table (2x the first draft, so every template reaches 5-30 answers).
ROW_COUNTS: dict[str, int] = {
    "customers": 240,
    "support_cases": 800,
    "case_notes": 1800,
    "refunds": 300,
    "employees": 120,
    "departments": 8,
    "buildings": 6,
    "vendors": 50,
    "procurement_requests": 400,
    "facility_tickets": 500,
}

# Target share of NULLs in each nullable column. refunds.approved_by_employee_id is NULL
# exactly when approved_at is, so it shares that rate.
NULL_RATES: dict[tuple[str, str], float] = {
    ("support_cases", "closed_at"): 0.35,
    ("support_cases", "assigned_employee_id"): 0.15,
    ("refunds", "approved_at"): 0.40,
    ("procurement_requests", "needed_by"): 0.40,
    ("facility_tickets", "resolved_at"): 0.30,
}

DDL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    tier TEXT NOT NULL,
    region TEXT NOT NULL,
    created_at DATE NOT NULL
);
CREATE TABLE support_cases (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    opened_at DATE NOT NULL,
    closed_at DATE,
    severity INTEGER NOT NULL,
    status TEXT NOT NULL,
    assigned_employee_id INTEGER REFERENCES employees (id)
);
CREATE TABLE case_notes (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES support_cases (id),
    author_employee_id INTEGER NOT NULL REFERENCES employees (id),
    note_type TEXT NOT NULL,
    created_at DATE NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE refunds (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES support_cases (id),
    amount REAL NOT NULL,
    approved_at DATE,
    approved_by_employee_id INTEGER REFERENCES employees (id)
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    department_id INTEGER NOT NULL REFERENCES departments (id),
    hired_at DATE NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    building_id INTEGER NOT NULL REFERENCES buildings (id)
);
CREATE TABLE buildings (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL
);
CREATE TABLE vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    risk_rating INTEGER NOT NULL
);
CREATE TABLE procurement_requests (
    id INTEGER PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors (id),
    requested_by_employee_id INTEGER NOT NULL REFERENCES employees (id),
    amount REAL NOT NULL,
    requested_at DATE NOT NULL,
    needed_by DATE,
    status TEXT NOT NULL
);
CREATE TABLE facility_tickets (
    id INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings (id),
    category TEXT NOT NULL,
    opened_at DATE NOT NULL,
    priority INTEGER NOT NULL,
    resolved_at DATE
);
CREATE TABLE _reference_answers (
    task_id TEXT NOT NULL,
    answer_id INTEGER NOT NULL
);
"""

DOMAIN_TABLES = tuple(ROW_COUNTS)
REFERENCE_TABLE = "_reference_answers"
ALL_TABLES = (*DOMAIN_TABLES, REFERENCE_TABLE)

Tables = dict[str, list[dict[str, Any]]]


def build_database(seed: int, row_counts: Mapping[str, int] | None = None) -> sqlite3.Connection:
    """A new in-memory task database whose rows are fully determined by `seed`.

    `_reference_answers` is created empty; generation fills it once the answer is known.
    """
    counts = {**ROW_COUNTS, **(row_counts or {})}
    if counts["buildings"] > len(BUILDING_NAMES) or counts["departments"] > len(DEPARTMENT_NAMES):
        raise ValueError("at most 6 buildings and 8 departments (one per fixed name)")
    rows = _generate_rows(seed, counts)
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DDL)
    # Parents before children, so every foreign key already points at an existing row.
    for table in (
        "buildings",
        "departments",
        "employees",
        "customers",
        "support_cases",
        "case_notes",
        "refunds",
        "vendors",
        "procurement_requests",
        "facility_tickets",
    ):
        table_rows = rows[table]
        columns = list(table_rows[0])
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            [tuple(row[c] for c in columns) for row in table_rows],
        )
    conn.commit()
    return conn


def _generate_rows(seed: int, n: Mapping[str, int]) -> Tables:
    rng = random.Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)

    def any_date() -> str:
        return (
            DATE_MIN + timedelta(days=rng.randrange((DATE_MAX - DATE_MIN).days + 1))
        ).isoformat()

    def up_to(after: str, max_days: int) -> str:
        """A date 0..max_days after `after`, never past DATE_MAX."""
        moved = date.fromisoformat(after) + timedelta(days=rng.randrange(max_days + 1))
        return min(moved, DATE_MAX).isoformat()

    def maybe(null_rate: float) -> bool:
        """True when the value should be present (False -> NULL)."""
        return rng.random() >= null_rate

    def person() -> str:
        return f"{fake.first_name()} {fake.last_name()}"

    rows: Tables = {}
    rows["buildings"] = [
        {"id": i, "name": BUILDING_NAMES[i - 1], "city": rng.choice(CITIES)}
        for i in range(1, n["buildings"] + 1)
    ]
    rows["departments"] = [
        {"id": i, "name": DEPARTMENT_NAMES[i - 1], "building_id": rng.randint(1, n["buildings"])}
        for i in range(1, n["departments"] + 1)
    ]
    rows["employees"] = [
        {
            "id": i,
            "name": person(),
            "department_id": rng.randint(1, n["departments"]),
            "hired_at": any_date(),
            "role": rng.choice(list(ROLES)),
        }
        for i in range(1, n["employees"] + 1)
    ]
    rows["customers"] = [
        {
            "id": i,
            "name": person(),
            "email": fake.safe_email(),  # reserved example.* domains only
            "tier": rng.choice(list(TIERS)),
            "region": rng.choice(list(REGIONS)),
            "created_at": any_date(),
        }
        for i in range(1, n["customers"] + 1)
    ]
    cases = []
    for i in range(1, n["support_cases"] + 1):
        opened = any_date()
        cases.append(
            {
                "id": i,
                "customer_id": rng.randint(1, n["customers"]),
                "opened_at": opened,
                "closed_at": up_to(opened, 60) if maybe(0.35) else None,
                "severity": rng.randint(1, 4),
                "status": rng.choice(list(CASE_STATUSES)),
                "assigned_employee_id": rng.randint(1, n["employees"]) if maybe(0.15) else None,
            }
        )
    rows["support_cases"] = cases
    notes = []
    for i in range(1, n["case_notes"] + 1):
        case = rng.choice(cases)
        notes.append(
            {
                "id": i,
                "case_id": case["id"],
                "author_employee_id": rng.randint(1, n["employees"]),
                "note_type": rng.choice(list(NOTE_TYPES)),
                "created_at": up_to(case["opened_at"], 30),
                "body": fake.sentence(nb_words=10),
            }
        )
    rows["case_notes"] = notes
    refunds = []
    for i in range(1, n["refunds"] + 1):
        case = rng.choice(cases)
        approved = up_to(case["opened_at"], 45) if maybe(0.40) else None
        refunds.append(
            {
                "id": i,
                "case_id": case["id"],
                "amount": round(rng.uniform(10, 2000), 2),
                "approved_at": approved,
                "approved_by_employee_id": None
                if approved is None
                else rng.randint(1, n["employees"]),
            }
        )
    rows["refunds"] = refunds
    rows["vendors"] = [
        {
            "id": i,
            "name": fake.company(),
            "country": rng.choice(COUNTRIES),
            "risk_rating": rng.randint(1, 5),
        }
        for i in range(1, n["vendors"] + 1)
    ]
    requests = []
    for i in range(1, n["procurement_requests"] + 1):
        requested = any_date()
        requests.append(
            {
                "id": i,
                "vendor_id": rng.randint(1, n["vendors"]),
                "requested_by_employee_id": rng.randint(1, n["employees"]),
                "amount": round(rng.uniform(500, 50000), 2),
                "requested_at": requested,
                "needed_by": up_to(requested, 120) if maybe(0.40) else None,
                "status": rng.choice(list(PROCUREMENT_STATUSES)),
            }
        )
    rows["procurement_requests"] = requests
    tickets = []
    for i in range(1, n["facility_tickets"] + 1):
        opened = any_date()
        tickets.append(
            {
                "id": i,
                "building_id": rng.randint(1, n["buildings"]),
                "category": rng.choice(list(TICKET_CATEGORIES)),
                "opened_at": opened,
                "priority": rng.randint(1, 3),
                "resolved_at": up_to(opened, 30) if maybe(0.30) else None,
            }
        )
    rows["facility_tickets"] = tickets
    return rows


def load_tables(conn: sqlite3.Connection, tables: tuple[str, ...] = DOMAIN_TABLES) -> Tables:
    """Rows as lists of dicts, ordered by id: the input to the pure-Python evaluators."""
    loaded: Tables = {}
    for table in tables:
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        columns = [column[0] for column in cursor.description]
        loaded[table] = [dict(zip(columns, row, strict=True)) for row in cursor]
    return loaded


def schema_dump(conn: sqlite3.Connection) -> str:
    """The canonical schema text (what `inspect_database` will return in step 2).

    The CREATE statements, then per table its columns with types and its row count. The row
    count of `_reference_answers` is left out: it equals the size of the answer.
    """
    creates = [
        sql
        for (sql,) in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY rowid"
        )
    ]
    lines = [
        "SQLite database: operations data for a fictional company.",
        "Dates are stored as ISO-8601 text (YYYY-MM-DD).",
        "",
        "-- Schema",
        *(f"{sql};" for sql in creates),
        "",
        "-- Tables",
    ]
    for table in ALL_TABLES:
        if table == REFERENCE_TABLE:
            lines.append(f"{table}")
        else:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            lines.append(f"{table} ({count} rows)")
        foreign = {
            row[3]: f"{row[2]}.{row[4]}"
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        for _, name, col_type, not_null, _, primary_key in conn.execute(
            f"PRAGMA table_info({table})"
        ):
            notes = []
            if primary_key:
                notes.append("primary key")
            if not_null and not primary_key:
                notes.append("not null")
            if name in foreign:
                notes.append(f"references {foreign[name]}")
            suffix = f" ({', '.join(notes)})" if notes else ""
            lines.append(f"  {name} {col_type}{suffix}")
    return "\n".join(lines) + "\n"
