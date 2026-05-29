"""Tests for ``philip.from_sql_cte`` (SQL with CTEs -> Hamilton DAG).

Skipped wholesale when the ``hamilton`` extra is not installed.
"""

from __future__ import annotations

import pytest

import philip

hamilton_required = pytest.importorskip("hamilton")
sqlglot_required = pytest.importorskip("sqlglot")

if not hasattr(philip, "from_sql_cte"):
    pytest.skip("philip[hamilton] not installed", allow_module_level=True)


SIMPLE_SQL = """
WITH raw_orders AS (SELECT * FROM orders),
     enriched AS (
        SELECT o.*, c.region FROM raw_orders o JOIN customers c ON o.cid = c.id
     ),
     by_region AS (
        SELECT region, COUNT(*) AS n FROM enriched GROUP BY region
     )
SELECT * FROM by_region WHERE n > 10
"""


def _driver(module):
    from hamilton.driver import Driver

    return Driver({}, module)


def test_module_exposes_cte_functions():
    module = philip.from_sql_cte(SIMPLE_SQL)
    names = {f for f in dir(module) if callable(getattr(module, f)) and not f.startswith("_")}
    assert {"raw_orders", "enriched", "by_region", "query"}.issubset(names)


def test_hamilton_driver_loads_module():
    module = philip.from_sql_cte(SIMPLE_SQL)
    dr = _driver(module)
    nodes = {v.name for v in dr.list_available_variables()}
    assert {"raw_orders", "enriched", "by_region", "query"}.issubset(nodes)


def test_dependencies_resolve_to_upstream_ctes_and_external_inputs():
    module = philip.from_sql_cte(SIMPLE_SQL)
    enriched = module.enriched
    sig = list(enriched.__annotations__.keys())
    # `enriched` depends on the `raw_orders` CTE and the external `customers` table.
    assert "raw_orders" in sig
    assert "customers" in sig
    # Returns SqlNode
    assert enriched.__annotations__["return"].__name__ == "SqlNode"


def test_executing_query_returns_sql_node_carrying_metadata():
    module = philip.from_sql_cte(SIMPLE_SQL)
    dr = _driver(module)
    result = dr.execute(
        ["query"],
        inputs={"orders": "orders_table", "customers": "customers_table"},
    )
    # Hamilton wraps the single requested var in a DataFrame; pull the value.
    sql_node = result["query"].iloc[0] if hasattr(result["query"], "iloc") else result["query"]
    assert sql_node.name == "query"
    assert "by_region" in sql_node.depends_on


def test_no_cte_refused():
    with pytest.raises(philip.SqlCteLiftError, match="no CTEs found"):
        philip.from_sql_cte("SELECT * FROM orders")


def test_recursive_cte_refused():
    sql = """
    WITH RECURSIVE roots(n) AS (
        SELECT 1 UNION ALL SELECT n+1 FROM roots WHERE n < 5
    )
    SELECT * FROM roots
    """
    with pytest.raises(philip.SqlCteLiftError, match="RECURSIVE"):
        philip.from_sql_cte(sql)


def test_invalid_identifier_cte_name_refused():
    sql = """
    WITH "my cte" AS (SELECT 1)
    SELECT * FROM "my cte"
    """
    with pytest.raises(philip.SqlCteLiftError, match="valid Python identifier"):
        philip.from_sql_cte(sql)


def test_dialect_argument_passed_through():
    # PostgreSQL-specific: DISTINCT ON
    sql = """
    WITH latest AS (
        SELECT DISTINCT ON (user_id) user_id, ts FROM events ORDER BY user_id, ts DESC
    )
    SELECT * FROM latest
    """
    module = philip.from_sql_cte(sql, dialect="postgres")
    assert hasattr(module, "latest")
    assert hasattr(module, "query")


def test_sql_node_dataclass_carries_fields():
    node = philip.SqlNode(
        name="x",
        sql="SELECT 1",
        depends_on=("a", "b"),
        external_inputs=("t",),
    )
    assert node.name == "x"
    assert node.sql == "SELECT 1"
    assert node.depends_on == ("a", "b")
    assert node.external_inputs == ("t",)


def test_unreferenced_cte_still_lifted():
    """A defined CTE that nothing else references must still appear as a node."""
    sql = """
    WITH unused AS (SELECT 1),
         used AS (SELECT 2)
    SELECT * FROM used
    """
    module = philip.from_sql_cte(sql)
    assert hasattr(module, "unused")
    assert hasattr(module, "used")
