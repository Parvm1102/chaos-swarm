"""Templates: the reference SQL and the pure-Python evaluator agree, and the requests follow
the wording rules (display forms for enums, explicit bounds, no schema identifiers)."""

import random
from collections.abc import Iterator
from typing import Any

import pytest

from swarm.tasks.schema import ALL_TABLES, ENUMS, build_database, load_tables
from swarm.tasks.templates import TEMPLATES, Template

SEED = 2024


@pytest.fixture(scope="module")
def db():
    conn = build_database(SEED)
    yield conn, load_tables(conn)
    conn.close()


def samples(template: Template, tables, n: int = 20) -> list[dict[str, Any]]:
    rng = random.Random(f"{template.name}-test")
    return [template.sample_params(rng, tables) for _ in range(n)]


def values(params: dict[str, Any]) -> Iterator[Any]:
    for value in params.values():
        yield from value if isinstance(value, list) else [value]


def schema_identifiers(conn) -> set[str]:
    """Table and column names written in snake_case: these must never appear in a request."""
    names = set(ALL_TABLES)
    for table in ALL_TABLES:
        names |= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return {name for name in names if "_" in name}


def display_forms() -> dict[str, str]:
    forms: dict[str, str] = {}
    for enum in ENUMS.values():
        for stored, shown in enum.items():
            assert forms.setdefault(stored, shown) == shown  # one display form per stored value
    return forms


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_sql_and_python_agree_on_20_sampled_parameter_sets(template, db):
    conn, tables = db
    sizes = []
    for params in samples(template, tables):
        from_sql = {row[0] for row in conn.execute(template.sql(params))}
        assert from_sql == template.evaluate_py(params, tables), params
        sizes.append(len(from_sql))
    assert max(sizes) > 0  # the cross-check is not vacuous


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_requests_follow_the_wording_rules(template, db):
    conn, tables = db
    identifiers = schema_identifiers(conn)
    forms = display_forms()
    for params in samples(template, tables):
        text = template.nl(params)
        assert text.startswith("Find the ")
        assert text.endswith(f"Return the IDs of the matching {template.answer_noun}.")
        assert "_reference" not in text
        assert not [name for name in identifiers if name in text]
        for value in values(params):
            if isinstance(value, str) and value in forms:
                assert forms[value] in text, value
                if forms[value] != value:
                    assert value not in text, f"stored literal {value!r} leaked"
            else:
                assert str(value) in text, value
        for key in [k for k in params if k.endswith("_from")]:
            start, end = params[key], params[key.replace("_from", "_before")]
            assert f"on or after {start} and before {end}" in text
        for key in [k for k in params if k.startswith("min_")]:
            assert f"of at least {params[key]}" in text
        if "refund_above" in params:
            assert f"greater than {params['refund_above']}" in text
        assert "recorded" in text  # every template states its NULL condition explicitly


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_each_template_has_the_four_required_kinds_of_condition(template):
    assert {f.kind for f in template.filters} == {"date_window", "numeric", "categorical", "null"}
    assert 4 <= len(template.filters) <= 6
    assert len({f.name for f in template.filters}) == len(template.filters)
    assert 1 <= template.joins <= 3


def test_only_t4_is_the_one_join_easy_template():
    assert [(t.name, t.difficulty) for t in TEMPLATES if t.joins == 1] == [("T4", "easy")]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_filter_name_is_wired_into_the_evaluator(template, db):
    """Dropping a filter must be able to change the answer, or the binding check is blind."""
    _, tables = db
    params_list = samples(template, tables)
    for f in template.filters:
        assert any(
            template.evaluate_py(p, tables, f.name) != template.evaluate_py(p, tables)
            for p in params_list
        ), f.name
