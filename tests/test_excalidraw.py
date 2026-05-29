"""Tests for ``philip.from_excalidraw`` and the IR projection pattern."""

from __future__ import annotations

from pathlib import Path

import pytest

import philip

FIXTURES = Path(__file__).parent / "fixtures" / "excalidraw"


def test_lift_returns_philip_graph():
    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    assert isinstance(g, philip.PhilipGraph)
    assert g.source_format == "excalidraw"


def test_shapes_and_arrows_become_nodes_and_edges():
    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    ids = {n.id for n in g.nodes}
    assert {"Raw", "Clean", "Output"} == ids
    assert len(g.edges) == 2
    edge_pairs = {(e.src, e.dst) for e in g.edges}
    assert edge_pairs == {("Raw", "Clean"), ("Clean", "Output")}


def test_container_id_text_becomes_label():
    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    labels = {n.id: n.label for n in g.nodes}
    assert labels["Raw"] == "Raw"
    assert labels["Clean"] == "Clean"


def test_arrow_container_text_becomes_edge_label():
    g = philip.from_excalidraw(FIXTURES / "with_label.excalidraw")
    labels = {(e.src, e.dst): e.label for e in g.edges}
    assert labels[("Decide", "Approve")] == "ok"
    assert labels[("Decide", "Reject")] == "deny"


def test_cycle_detected_by_ir():
    g = philip.from_excalidraw(FIXTURES / "cycle.excalidraw")
    assert g.is_acyclic() is False


def test_acyclic_check_on_dag():
    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    assert g.is_acyclic() is True


def test_dag_projects_to_burr():
    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    app = g.to_burr()
    names = {a.name for a in app.graph.actions}
    assert {"Raw", "Clean", "Output"}.issubset(names)


def test_cyclic_burr_projection_works():
    """Burr is happy with cycles; FSM retry-loops use them all the time."""
    g = philip.from_excalidraw(FIXTURES / "cycle.excalidraw")
    app = g.to_burr()
    names = {a.name for a in app.graph.actions}
    assert {"A", "B"} == names


def test_branching_lifts_to_choice_guards():
    """A shape with multiple outbound arrows produces _choice == "<label>" guards
    on each (or _choice == "<destination>" when the arrow is unlabeled)."""
    g = philip.from_excalidraw(FIXTURES / "with_label.excalidraw")
    app = g.to_burr()
    guards = [
        getattr(t.condition, "name", "") for t in app.graph.transitions if t.from_.name == "Decide"
    ]
    assert any('_choice == "ok"' in g for g in guards)
    assert any('_choice == "deny"' in g for g in guards)


hamilton_required = pytest.importorskip("hamilton")


def test_dag_projects_to_hamilton():
    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    module = g.to_hamilton()
    assert hasattr(module, "Raw")
    assert hasattr(module, "Clean")
    assert hasattr(module, "Output")


def test_cyclic_hamilton_projection_refused():
    g = philip.from_excalidraw(FIXTURES / "cycle.excalidraw")
    with pytest.raises(ValueError, match="cycle"):
        g.to_hamilton()


def test_hamilton_driver_loads_excalidraw_module():
    from hamilton.driver import Driver

    g = philip.from_excalidraw(FIXTURES / "linear_dag.excalidraw")
    dr = Driver({}, g.to_hamilton())
    nodes = {v.name for v in dr.list_available_variables()}
    assert {"Raw", "Clean", "Output"}.issubset(nodes)


def test_non_excalidraw_json_refused():
    with pytest.raises(philip.ExcalidrawLiftError, match="not an Excalidraw"):
        philip.from_excalidraw_text('{"type": "tldraw"}')


def test_pure_svg_without_payload_refused():
    svg = "<svg><rect x='0' y='0' width='10' height='10'/></svg>"
    with pytest.raises(philip.ExcalidrawLiftError, match="payload"):
        philip.from_excalidraw_text(svg)


def test_no_shapes_refused():
    with pytest.raises(philip.ExcalidrawLiftError, match="no shapes"):
        philip.from_excalidraw_text('{"type": "excalidraw", "elements": []}')


def test_dangling_arrow_silently_dropped():
    g = philip.from_excalidraw_text(
        '{"type":"excalidraw","elements":['
        '{"id":"s1","type":"rectangle","x":0,"y":0,"width":10,"height":10},'
        '{"id":"a1","type":"arrow","x":0,"y":0,"points":[[0,0],[10,0]]}'
        "]}"
    )
    assert len(g.nodes) == 1
    assert len(g.edges) == 0


def test_ir_is_acyclic_helper():
    """Direct IR construction works without going through a lifter."""
    g = philip.PhilipGraph(
        nodes=(philip.Node(id="a"), philip.Node(id="b"), philip.Node(id="c")),
        edges=(philip.Edge(src="a", dst="b"), philip.Edge(src="b", dst="c")),
    )
    assert g.is_acyclic() is True

    g_cyclic = philip.PhilipGraph(
        nodes=(philip.Node(id="a"), philip.Node(id="b")),
        edges=(philip.Edge(src="a", dst="b"), philip.Edge(src="b", dst="a")),
    )
    assert g_cyclic.is_acyclic() is False


def test_ir_to_burr_with_entrypoint_and_terminals():
    g = philip.PhilipGraph(
        nodes=(
            philip.Node(id="start"),
            philip.Node(id="middle"),
            philip.Node(id="finish"),
        ),
        edges=(
            philip.Edge(src="start", dst="middle"),
            philip.Edge(src="middle", dst="finish"),
        ),
        entrypoint="start",
        terminals=frozenset({"finish"}),
    )
    app = g.to_burr()
    names = {a.name for a in app.graph.actions}
    assert "done" in names
    assert "start" in names
