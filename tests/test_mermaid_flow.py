"""Tests for ``philip.from_mermaid_flow`` (Mermaid flowchart -> Hamilton DAG)."""

from __future__ import annotations

import pytest

import philip

hamilton_required = pytest.importorskip("hamilton")

if not hasattr(philip, "from_mermaid_flow"):
    pytest.skip("philip[hamilton] not installed", allow_module_level=True)


SIMPLE = """
flowchart TD
    raw[Raw Orders] --> clean[Cleaned]
    clean --> joined[Joined]
    customers --> joined
    joined --> summary[Region Summary]
"""


def _driver(module):
    from hamilton.driver import Driver

    return Driver({}, module)


def test_lift_returns_module_with_functions_per_node():
    module = philip.from_mermaid_flow_text(SIMPLE)
    funcs = {f for f in dir(module) if callable(getattr(module, f)) and not f.startswith("_")}
    assert {"raw", "clean", "joined", "customers", "summary"}.issubset(funcs)


def test_hamilton_driver_loads_module():
    module = philip.from_mermaid_flow_text(SIMPLE)
    dr = _driver(module)
    nodes = {v.name for v in dr.list_available_variables()}
    assert {"raw", "clean", "joined", "customers", "summary"}.issubset(nodes)


def test_dependencies_match_diagram_edges():
    module = philip.from_mermaid_flow_text(SIMPLE)
    # `joined` depends on `clean` AND `customers`.
    sig = list(module.joined.__annotations__.keys())
    sig.remove("return")
    assert sorted(sig) == ["clean", "customers"]
    # `summary` depends on `joined`.
    summary_sig = list(module.summary.__annotations__.keys())
    summary_sig.remove("return")
    assert summary_sig == ["joined"]


def test_executing_summary_returns_node_carrying_metadata():
    module = philip.from_mermaid_flow_text(SIMPLE)
    dr = _driver(module)
    result = dr.execute(["summary"])
    val = result["summary"].iloc[0] if hasattr(result["summary"], "iloc") else result["summary"]
    assert val.id == "summary"
    assert "joined" in val.depends_on
    assert val.label == "Region Summary"


def test_graph_directive_accepted_as_synonym():
    text = """
    graph LR
        a --> b
        b --> c
    """
    module = philip.from_mermaid_flow_text(text)
    assert hasattr(module, "a")
    assert hasattr(module, "b")
    assert hasattr(module, "c")


def test_chained_arrows_lift():
    text = """
    flowchart TD
        a --> b --> c --> d
    """
    module = philip.from_mermaid_flow_text(text)
    funcs = {f for f in dir(module) if callable(getattr(module, f)) and not f.startswith("_")}
    assert {"a", "b", "c", "d"}.issubset(funcs)
    # `d` depends on `c`, transitively on `b` and `a`.
    sig = list(module.d.__annotations__.keys())
    sig.remove("return")
    assert sig == ["c"]


def test_labeled_edge_does_not_break():
    text = """
    flowchart TD
        a --> b
        a -- transforms --> c
        b --> c
    """
    module = philip.from_mermaid_flow_text(text)
    c_sig = list(module.c.__annotations__.keys())
    c_sig.remove("return")
    assert sorted(c_sig) == ["a", "b"]


def test_cycle_refused():
    text = """
    flowchart TD
        a --> b
        b --> c
        c --> a
    """
    with pytest.raises(philip.MermaidFlowLiftError, match="cycle detected"):
        philip.from_mermaid_flow_text(text)


def test_subgraph_refused():
    text = """
    flowchart TD
        subgraph cluster
            a --> b
        end
        b --> c
    """
    with pytest.raises(philip.MermaidFlowLiftError, match="subgraphs"):
        philip.from_mermaid_flow_text(text)


def test_missing_directive_refused():
    with pytest.raises(philip.MermaidFlowLiftError, match="directive"):
        philip.from_mermaid_flow_text("a --> b\n")


def test_invalid_node_id_refused():
    """sqlglot accepts quoted identifiers; we restrict to Python names."""
    text = """
    flowchart TD
        1invalid --> b
    """
    with pytest.raises(philip.MermaidFlowLiftError):
        philip.from_mermaid_flow_text(text)


def test_mermaid_node_dataclass_carries_fields():
    n = philip.MermaidNode(id="x", label="Display", depends_on=("a", "b"), shape="diamond")
    assert n.id == "x"
    assert n.label == "Display"
    assert n.depends_on == ("a", "b")
    assert n.shape == "diamond"


def test_example_flowchart_diagrams_all_lift():
    """Every flowchart in examples/mermaid/ lifts cleanly."""
    from pathlib import Path

    examples_dir = Path(__file__).parent.parent / "examples" / "mermaid"
    if not examples_dir.is_dir():
        pytest.skip("examples/mermaid not present")
    flow_diagrams = [
        p
        for p in sorted(examples_dir.glob("*.mmd"))
        if any(
            line.strip().startswith(("flowchart", "graph "))
            for line in p.read_text().splitlines()
        )
    ]
    assert flow_diagrams, "expected at least one flowchart example"
    for path in flow_diagrams:
        module = philip.from_mermaid_flow(path)
        funcs = [f for f in dir(module) if callable(getattr(module, f)) and not f.startswith("_")]
        assert funcs, f"{path.name} produced empty module"
