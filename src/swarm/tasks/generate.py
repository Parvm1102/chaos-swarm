"""Task generation: one SQLite database, one request and one reference answer per task.

generate_task:
1. builds the task's database in memory, seeded from (base_seed, task_index);
2. draws template parameters until the answer is acceptable:
   - difficulty "hard" first hardens each draw (swarm.tasks.traps): a day-level date window,
     and traps planted by editing rows (date edges, NULL near misses, amounts equal to the
     threshold), and the draw must also pass traps_bite(). Its edits are rolled back when the
     draw is rejected;
   - the reference SQL and the pure-Python evaluator must agree exactly. A disagreement is a bug
     in one of them, so it stops generation (GroundTruthMismatch) rather than being resampled
     away, which would quietly drop exactly the boundary and NULL cases where they differ;
   - the answer has 5-30 IDs and at most 25% of the answer table;
   - every filter binds: leaving out any single filter changes the answer.
   After 50 rejected draws it raises TaskGenerationError naming the template;
3. stores the answer in _reference_answers, then writes db.sqlite (VACUUM INTO) and task.json.

generate_tasks cycles T1..T4 over the task indices and writes manifest.json: every TaskSpec
without its reference SQL, which stays in task.json only. A base set goes to seed<N>/, a hard
set to seed<N>-hard/.
"""

import hashlib
import random
import shutil
import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict

from swarm.tasks.schema import (
    ALL_TABLES,
    REFERENCE_TABLE,
    ROW_COUNTS,
    Tables,
    build_database,
    load_tables,
    schema_dump,
)
from swarm.tasks.templates import AGG_TEMPLATES, TEMPLATES, Template
from swarm.tasks.traps import Update, harden, traps_bite

DIFFICULTIES = ("base", "hard", "agg")
# Row-count overrides per difficulty. The agg set triples the case notes: at 1,800 notes,
# "notes on at least two different cases" (A2) matches about 3 employees in the whole
# company, too few for a 5-30 answer.
DIFFICULTY_ROW_COUNTS: dict[str, dict[str, int]] = {"agg": {"case_notes": 5400}}
MIN_ANSWER = 5
MAX_ANSWER = 30
MAX_ANSWER_SHARE = 0.25
MAX_REJECTIONS = 50
GENERATOR = "swarm.tasks/1"
MANIFEST_FILE = "manifest.json"
TASK_FILE = "task.json"
DB_FILE = "db.sqlite"
ANSWER_SIZE_BINS = ((5, 9), (10, 14), (15, 19), (20, 24), (25, 30))


class TaskGenerationError(RuntimeError):
    """No acceptable parameters were found within the rejection budget."""


class GroundTruthMismatch(RuntimeError):
    """The reference SQL and the pure-Python evaluator disagree: one of them has a bug."""


class ManifestTask(BaseModel):
    """A task as listed in manifest.json (no reference SQL)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    template: str
    difficulty: str
    db_seed: int
    params: dict[str, Any]
    nl_request: str
    reference_ids: list[int]
    answer_table: str
    n_answer: int
    n_samples: int  # parameter draws until one was accepted (1 = the first draw)
    schema_dump_chars: int
    fingerprint: str
    # Hard tasks only: which rows carry which trap. Left out of the JSON when None, so base
    # task sets stay byte-identical to the ones generated before the hard level existed.
    traps: dict[str, Any] | None = None


class TaskSpec(ManifestTask):
    """The full task, as written to task.json."""

    reference_sql: str


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    generator: str
    base_seed: int
    n_tasks: int
    row_counts: dict[str, int]
    tasks: list[ManifestTask]

    def digest(self) -> str:
        """sha256 over the task fingerprints in order: names this exact task set."""
        return hashlib.sha256("\n".join(t.fingerprint for t in self.tasks).encode()).hexdigest()


def derive_seed(*parts: object) -> int:
    """A stable 64-bit seed from its parts (Python's hash() is not stable across versions)."""
    digest = hashlib.sha256(":".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def answer_cap(answer_table_rows: int) -> int:
    """The largest acceptable answer: 30 IDs, or 25% of the answer table if that is smaller."""
    return min(MAX_ANSWER, int(answer_table_rows * MAX_ANSWER_SHARE))


def fingerprint(conn: sqlite3.Connection, reference_ids: list[int]) -> str:
    """sha256 over every table's rows (sorted canonical JSON, `_reference_answers` included)
    plus the reference IDs."""
    digest = hashlib.sha256()
    for table in sorted(ALL_TABLES):
        digest.update(table.encode() + b"\n")
        for row in sorted(orjson.dumps(list(r)) for r in conn.execute(f"SELECT * FROM {table}")):
            digest.update(row + b"\n")
    digest.update(orjson.dumps(sorted(reference_ids)))
    return digest.hexdigest()


def generate_task(
    task_index: int,
    base_seed: int,
    template: Template,
    difficulty: str = "base",
    *,
    out_dir: Path | None = None,
    row_counts: Mapping[str, int] | None = None,
    max_rejections: int = MAX_REJECTIONS,
) -> TaskSpec:
    """Build one task. With `out_dir`, also write out_dir/db.sqlite and out_dir/task.json.

    `difficulty` is "base", "hard" (traps planted after the draw; see swarm.tasks.traps) or
    "agg" (hard, with an aggregation template and DIFFICULTY_ROW_COUNTS["agg"]). A hard task
    starts from the same database as the base task with the same index, but draws from its
    own random stream.
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty {difficulty!r}; choose from {DIFFICULTIES}")
    task_id = f"task_{task_index:04d}"
    db_seed = derive_seed(base_seed, task_index)
    conn = build_database(
        db_seed, {**DIFFICULTY_ROW_COUNTS.get(difficulty, {}), **(row_counts or {})}
    )
    try:
        tables = load_tables(conn)
        stream = (
            ("params", template.name)
            if difficulty == "base"
            else ("params", template.name, difficulty)
        )
        rng = random.Random(derive_seed(db_seed, *stream))
        params, answer, draws, traps = _draw_until_accepted(
            template,
            conn,
            tables,
            rng,
            answer_cap(len(tables[template.answer_table])),
            max_rejections,
            task_id,
            hard=difficulty in ("hard", "agg"),
        )
        reference_ids = sorted(answer)
        conn.executemany(
            f"INSERT INTO {REFERENCE_TABLE} (task_id, answer_id) VALUES (?, ?)",
            [(task_id, answer_id) for answer_id in reference_ids],
        )
        conn.commit()
        spec = TaskSpec(
            task_id=task_id,
            template=template.name,
            difficulty=difficulty,
            db_seed=db_seed,
            params=params,
            nl_request=template.nl(params),
            reference_sql=template.sql(params),
            reference_ids=reference_ids,
            answer_table=template.answer_table,
            n_answer=len(reference_ids),
            n_samples=draws,
            schema_dump_chars=len(schema_dump(conn)),
            fingerprint=fingerprint(conn, reference_ids),
            traps=traps,
        )
        if out_dir is not None:
            write_task(conn, spec, Path(out_dir))
        return spec
    finally:
        conn.close()


def _draw_until_accepted(
    template: Template,
    conn: sqlite3.Connection,
    tables: Tables,
    rng: random.Random,
    cap: int,
    max_rejections: int,
    task_id: str,
    *,
    hard: bool = False,
) -> tuple[dict[str, Any], set[int], int, dict[str, Any] | None]:
    """Draw parameters until the answer is acceptable.

    Returns (params, answer, draws, traps); traps is None for a base task. A hard draw's row
    edits are made inside a transaction: committed if the draw is accepted, rolled back if not,
    so every draw starts from the same base rows.
    """
    for draw in range(1, max_rejections + 1):
        params = template.sample_params(rng, tables)
        traps = None
        draw_tables = tables
        if hard:
            hardened = harden(template, params, tables, rng)
            if hardened is None:
                continue  # too few rows to plant the traps on
            params, updates, traps = hardened
            _apply_updates(conn, updates)
            draw_tables = load_tables(conn)
        from_sql = {row[0] for row in conn.execute(template.sql(params))}
        from_python = template.evaluate_py(params, draw_tables)
        if from_sql != from_python:
            raise GroundTruthMismatch(
                f"{template.label} ({task_id}): SQL and Python disagree for {params}: "
                f"only in SQL {sorted(from_sql - from_python)}, "
                f"only in Python {sorted(from_python - from_sql)}"
            )
        accepted = (
            MIN_ANSWER <= len(from_sql) <= cap
            # every filter binds: dropping any one of them changes the answer
            and not any(
                template.evaluate_py(params, draw_tables, f.name) == from_sql
                for f in template.filters
            )
            and (not hard or traps_bite(template, params, draw_tables, from_sql))
            # an aggregation must bite: relaxing it (e.g. two -> one) changes the answer
            and (
                template.relax is None
                or template.evaluate_py({**params, **template.relax}, draw_tables) != from_sql
            )
        )
        if accepted:
            conn.commit()
            return params, from_sql, draw, traps
        conn.rollback()
    raise TaskGenerationError(
        f"{template.label} ({task_id}): no acceptable parameters after {max_rejections} draws"
    )


def _apply_updates(conn: sqlite3.Connection, updates: list[Update]) -> None:
    """Row edits from swarm.tasks.traps; table and column names come from our own code."""
    for table, row_id, column, value in updates:
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))


def write_task(conn: sqlite3.Connection, spec: TaskSpec, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / DB_FILE
    db_path.unlink(missing_ok=True)  # VACUUM INTO refuses to overwrite a file
    conn.execute("VACUUM INTO ?", (str(db_path),))
    (out_dir / TASK_FILE).write_bytes(_json(spec.model_dump(mode="json", exclude_none=True)))


def task_set_name(base_seed: int, difficulty: str = "base") -> str:
    """The folder of a task set under the task root: seed0 (base) or seed0-hard."""
    return f"seed{base_seed}" if difficulty == "base" else f"seed{base_seed}-{difficulty}"


def generate_tasks(
    base_seed: int,
    n: int,
    out_root: Path,
    *,
    difficulty: str = "base",
    row_counts: Mapping[str, int] | None = None,
    force: bool = False,
    on_task: Callable[[TaskSpec], None] | None = None,
) -> Manifest:
    """Generate n tasks into out_root/<task_set_name>/, cycling T1..T4, and write the manifest.

    An existing task set is only replaced with force=True (it is deleted first, sequences.json
    included, because sequences belong to one exact task set).
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty {difficulty!r}; choose from {DIFFICULTIES}")
    root = Path(out_root) / task_set_name(base_seed, difficulty)
    templates = AGG_TEMPLATES if difficulty == "agg" else TEMPLATES
    if root.exists():
        if not force:
            raise FileExistsError(f"{root} already exists; use --force to replace it")
        shutil.rmtree(root)
    root.mkdir(parents=True)
    specs = []
    for index in range(n):
        template = templates[index % len(templates)]
        spec = generate_task(
            index,
            base_seed,
            template,
            difficulty,
            out_dir=root / f"task_{index:04d}",
            row_counts=row_counts,
        )
        specs.append(spec)
        if on_task is not None:
            on_task(spec)
    manifest = Manifest(
        generator=GENERATOR,
        base_seed=base_seed,
        n_tasks=n,
        row_counts={
            **ROW_COUNTS,
            **DIFFICULTY_ROW_COUNTS.get(difficulty, {}),
            **(row_counts or {}),
        },
        tasks=[ManifestTask(**spec.model_dump(exclude={"reference_sql"})) for spec in specs],
    )
    (root / MANIFEST_FILE).write_bytes(_json(manifest.model_dump(mode="json", exclude_none=True)))
    return manifest


def load_manifest(task_dir: Path) -> Manifest:
    return Manifest.model_validate_json((Path(task_dir) / MANIFEST_FILE).read_bytes())


def load_task(task_dir: Path, task_id: str) -> TaskSpec:
    return TaskSpec.model_validate_json((Path(task_dir) / task_id / TASK_FILE).read_bytes())


def summarize(manifest: Manifest) -> dict[str, Any]:
    """What `swarm tasks stats` prints: per-template counts and answer sizes, draws per task,
    the answer-size histogram, schema-dump length, duplicate fingerprints, and pass/fail checks."""
    by_template: dict[str, list[ManifestTask]] = {}
    for task in manifest.tasks:
        by_template.setdefault(task.template, []).append(task)

    def histogram(tasks: list[ManifestTask]) -> dict[str, int]:
        return {
            f"{lo}-{hi}": sum(lo <= t.n_answer <= hi for t in tasks) for lo, hi in ANSWER_SIZE_BINS
        }

    templates = {}
    for name in sorted(by_template):
        tasks = by_template[name]
        sizes = [t.n_answer for t in tasks]
        draws = [t.n_samples for t in tasks]
        templates[name] = {
            "tasks": len(tasks),
            "answer_min": min(sizes),
            "answer_median": statistics.median(sizes),
            "answer_max": max(sizes),
            "mean_draws": statistics.fmean(draws),
            "max_draws": max(draws),
            "histogram": histogram(tasks),
        }
    sizes = [t.n_answer for t in manifest.tasks]
    dumps = [t.schema_dump_chars for t in manifest.tasks]
    duplicates = sum(
        count - 1 for count in Counter(t.fingerprint for t in manifest.tasks).values() if count > 1
    )
    checks = {
        "same number of tasks per template": len({v["tasks"] for v in templates.values()}) == 1,
        "every answer has 5-30 IDs": all(MIN_ANSWER <= size <= MAX_ANSWER for size in sizes),
        "median schema dump >= 2,000 chars": statistics.median(dumps) >= 2000,
        "no duplicate fingerprints": duplicates == 0,
    }
    return {
        "n_tasks": len(manifest.tasks),
        "templates": templates,
        "histogram": histogram(manifest.tasks),
        "schema_dump_chars": {"min": min(dumps), "median": statistics.median(dumps)},
        "duplicate_fingerprints": duplicates,
        "checks": checks,
    }


def _json(data: Any) -> bytes:
    return orjson.dumps(data, option=orjson.OPT_INDENT_2) + b"\n"
