"""Lift a SQL query with Common Table Expressions into a Hamilton DAG.

Given a SQL string containing CTEs, ``from_sql_cte`` returns a module
whose functions Hamilton's ``Driver`` accepts directly. Each CTE
becomes one function. The function's parameter names mirror the upstream
CTEs the CTE references; external tables (anything that is not itself a
defined CTE) become driver inputs.

The function body returns a :class:`SqlNode` carrying the CTE's name,
the rewritten SQL fragment, and its dependency set. Downstream Hamilton
materializers can execute against a database, render lineage, or be
introspected statically.

Example::

    from philip import from_sql_cte
    from hamilton.driver import Driver

    sql = '''
    WITH raw_orders AS (SELECT * FROM orders),
         enriched   AS (SELECT o.*, c.region FROM raw_orders o
                        JOIN customers c ON o.cid = c.id),
         by_region  AS (SELECT region, COUNT(*) AS n FROM enriched GROUP BY region)
    SELECT * FROM by_region WHERE n > 10
    '''
    module = from_sql_cte(sql)
    driver = Driver({}, module)
    driver.visualize_execution(["query"], output_file_path="./graph.png")

The supported subset:

* One top-level ``SELECT`` statement with a ``WITH`` clause.
* CTE names must be valid Python identifiers (sqlglot's parser accepts
  more; the lift restricts to identifiers so they map cleanly to
  function names).
* External table names (anything referenced that is not a CTE) become
  positional inputs the Hamilton ``Driver`` provides at execution time.
* Recursive CTEs (``WITH RECURSIVE``) are rejected; their semantics map
  poorly to a one-shot DAG.
* If multiple SQL dialects matter, pass ``dialect=`` to ``from_sql_cte``.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from types import ModuleType

import sqlglot
from sqlglot import exp

# ── Public types ──────────────────────────────────────────────────────────


class SqlCteLiftError(ValueError):
    """Raised when a SQL source cannot be lifted into a Hamilton DAG."""


@dataclass(frozen=True)
class SqlNode:
    """One materialized step in the lifted DAG.

    Hamilton sees this as the function's return value. Downstream code
    can read ``.sql`` to actually execute the fragment, ``.depends_on``
    to walk dependencies, or ``.external_inputs`` to know which base
    tables this node depends on.
    """

    name: str
    sql: str
    depends_on: tuple[str, ...] = ()
    external_inputs: tuple[str, ...] = ()


# ── Public entry point ────────────────────────────────────────────────────


def from_sql_cte(
    sql: str,
    *,
    dialect: str | None = None,
    module_name: str = "philip_sql_cte_dag",
) -> ModuleType:
    """Lift a SQL query with CTEs into a Hamilton-compatible module.

    Returns a Python module whose top-level functions correspond to the
    CTEs in the query plus a final ``query`` function for the outer
    ``SELECT``. The module is constructed via
    :func:`hamilton.ad_hoc_utils.create_module` and is accepted by
    Hamilton's ``Driver`` directly.

    ``dialect`` is forwarded to :func:`sqlglot.parse_one`. ``None`` uses
    sqlglot's permissive default.
    """
    from hamilton.ad_hoc_utils import create_module

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise SqlCteLiftError(f"sql parse failed: {e}") from e

    if not isinstance(tree, exp.Select):
        raise SqlCteLiftError(f"expected a top-level SELECT, got {type(tree).__name__}")

    # sqlglot stores the WITH clause under ``with_`` (the trailing underscore
    # avoids the Python keyword) in current versions.
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is None or not with_clause.expressions:
        raise SqlCteLiftError(
            "no CTEs found; expected `WITH name AS (...)` at the top of the query"
        )
    if with_clause.args.get("recursive"):
        raise SqlCteLiftError(
            "WITH RECURSIVE is not lifted; recursive CTEs map poorly to a one-shot DAG"
        )

    ctes = list(with_clause.expressions)
    cte_names = [c.alias_or_name for c in ctes]
    for n in cte_names:
        _check_identifier(n)
    cte_name_set = set(cte_names)

    # Build the per-CTE function source.
    node_sources: list[str] = []
    for cte in ctes:
        name = cte.alias_or_name
        inner = cte.this  # the inner SELECT
        deps, externals = _split_refs(inner, cte_name_set)
        sql_fragment = inner.sql(dialect=dialect, pretty=False)
        node_sources.append(_node_function_source(name, sql_fragment, deps, externals))

    # Top-level SELECT (without the WITH clause) becomes the leaf ``query`` node.
    outer = tree.copy()
    outer.set("with_", None)
    outer.set("with", None)
    outer_sql = outer.sql(dialect=dialect, pretty=False)
    outer_deps, outer_externals = _split_refs(outer, cte_name_set)
    node_sources.append(_node_function_source("query", outer_sql, outer_deps, outer_externals))

    source = _module_preamble() + "\n\n" + "\n\n".join(node_sources) + "\n"
    return create_module(source, module_name=module_name)


# ── Internals ─────────────────────────────────────────────────────────────


_IDENT = re.compile(r"^[A-Za-z_]\w*$")


def _check_identifier(name: str) -> None:
    if not _IDENT.match(name) or keyword.iskeyword(name):
        raise SqlCteLiftError(
            f"CTE name {name!r} is not a valid Python identifier; rename it before lifting"
        )


@dataclass(frozen=True)
class _RefSplit:
    deps: tuple[str, ...]
    externals: tuple[str, ...]


def _split_refs(
    node: exp.Expression, cte_names: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (cte_deps, external_table_inputs) for a node's table refs."""
    referenced = {t.name for t in node.find_all(exp.Table)}
    deps = tuple(sorted(referenced & cte_names))
    externals = tuple(sorted(referenced - cte_names))
    return deps, externals


def _module_preamble() -> str:
    """Module-level imports and shared types for the synthesized DAG module."""
    return "from philip._lifters.sql_cte import SqlNode\nfrom typing import Any\n"


def _node_function_source(
    name: str,
    sql_fragment: str,
    deps: tuple[str, ...],
    externals: tuple[str, ...],
) -> str:
    """Render the source of one Hamilton node function.

    The function's signature carries the CTE dependencies as ``SqlNode``
    parameters plus the external table names as ``Any`` parameters that
    Hamilton's ``Driver`` supplies via inputs.
    """
    params: list[str] = []
    params.extend(f"{p}: SqlNode" for p in deps)
    params.extend(f"{p}: Any" for p in externals)
    param_sig = ", ".join(params)
    return (
        f"def {name}({param_sig}) -> SqlNode:\n"
        f"    '''Lifted SQL node for {name!r}.'''\n"
        f"    return SqlNode(\n"
        f"        name={name!r},\n"
        f"        sql={sql_fragment!r},\n"
        f"        depends_on={deps!r},\n"
        f"        external_inputs={externals!r},\n"
        f"    )"
    )
