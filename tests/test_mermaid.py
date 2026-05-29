"""Tests for the Mermaid stateDiagram-v2 lifter."""

from __future__ import annotations

from pathlib import Path

import pytest

import philip

FIXTURES = Path(__file__).parent / "fixtures" / "mermaid"


def test_simple_diagram_builds_and_runs():
    app = philip.from_mermaid(FIXTURES / "simple.mmd")
    names = {a.name for a in app.graph.actions}
    assert "Working" in names
    assert "Done" in names
    last, _, state = app.run(halt_after=["done"])
    assert last.name == "done"
    assert state["_step_count"] >= 2


def test_predicate_label_becomes_guard():
    app = philip.from_mermaid(FIXTURES / "incident_response.mmd")
    guards = [
        getattr(t.condition, "name", "") for t in app.graph.transitions if hasattr(t, "condition")
    ]
    assert any('severity == "critical"' in g for g in guards), (
        f"expected a guarded transition; got {guards}"
    )


def test_descriptive_label_does_not_become_guard():
    """`Acknowledge --> Investigate : on_alert` carries a label that is
    not a Python predicate; it must NOT become a Burr guard."""
    app = philip.from_mermaid(FIXTURES / "incident_response.mmd")
    for t in app.graph.transitions:
        if t.from_.name == "Acknowledge" and t.to.name == "Investigate":
            cond_name = getattr(t.condition, "name", "default")
            assert "on_alert" not in cond_name, "event-name label leaked into a guard condition"
            break
    else:
        pytest.fail("Acknowledge -> Investigate transition not found")


def test_loop_self_transition():
    """`Retry --> Retry` must produce a self-edge."""
    app = philip.from_mermaid(FIXTURES / "loop.mmd")
    self_edges = [
        t for t in app.graph.transitions if t.from_.name == "Retry" and t.to.name == "Retry"
    ]
    assert len(self_edges) == 1


def test_multiple_terminal_states_route_through_done():
    """Both `Done --> [*]` and `Fail --> [*]` should route through a
    synthesized `done` terminal."""
    app = philip.from_mermaid(FIXTURES / "loop.mmd")
    names = {a.name for a in app.graph.actions}
    assert "done" in names
    routes_to_done = [t for t in app.graph.transitions if t.to.name == "done"]
    sources = {t.from_.name for t in routes_to_done}
    assert sources == {"Done", "Fail"}


def test_composite_state_refused():
    with pytest.raises(philip.MermaidLiftError, match="composite states"):
        philip.from_mermaid(FIXTURES / "composite_unsupported.mmd")


def test_missing_directive_refused():
    with pytest.raises(philip.MermaidLiftError, match="directive"):
        philip.from_mermaid_text("A --> B\nB --> [*]\n")


def test_no_entrypoint_refused():
    with pytest.raises(philip.MermaidLiftError, match="entrypoint"):
        philip.from_mermaid_text("stateDiagram-v2\nA --> B\nB --> [*]\n")


def test_multiple_entrypoints_refused():
    with pytest.raises(philip.MermaidLiftError, match="multiple entrypoints"):
        philip.from_mermaid_text("stateDiagram-v2\n[*] --> A\n[*] --> B\nA --> [*]\nB --> [*]\n")


def test_ignores_presentation_and_comments():
    text = """
stateDiagram-v2
    %% This is a comment
    classDef important fill:#f96
    class A important
    note right of A: this is the start
    direction LR

    [*] --> A
    A --> B
    B --> [*]
"""
    app = philip.from_mermaid_text(text)
    names = {a.name for a in app.graph.actions}
    assert names == {"A", "B", "done"}


def test_unrecognized_line_refused_with_line_number():
    text = """
stateDiagram-v2
    [*] --> A
    A --> B
    garbage
    B --> [*]
"""
    with pytest.raises(philip.MermaidLiftError) as exc_info:
        philip.from_mermaid_text(text)
    assert exc_info.value.line_number == 5
    assert "garbage" in str(exc_info.value)


def test_multiple_unlabeled_outbound_lift_to_choice_by_destination():
    """Mermaid's hello-world has multiple unlabeled outbound from one state.
    Burr rejects multiple defaults from one source; the lifter must lift each
    edge to a ``_choice == "<destination>"`` guard so the actor picks at
    runtime."""
    app = philip.from_mermaid(FIXTURES / "multi_unlabeled.mmd")
    still_outbound = [t for t in app.graph.transitions if t.from_.name == "Still"]
    # Still has two outbound: -> Moving and -> done (the [*] terminal).
    assert len(still_outbound) == 2
    guards = sorted(getattr(t.condition, "name", "") for t in still_outbound)
    assert '_choice == "Moving"' in guards
    assert '_choice == "done"' in guards
    # Moving has two outbound too; no default leaks.
    moving_outbound = [t for t in app.graph.transitions if t.from_.name == "Moving"]
    assert len(moving_outbound) == 2
    for t in moving_outbound:
        assert getattr(t.condition, "name", "") != "default"


def test_example_state_diagrams_all_lift():
    """Every stateDiagram in examples/mermaid/ lifts cleanly. Acts as a
    regression safety net for the canonical demo set. flowchart diagrams
    in the same directory are excluded (they belong to the Hamilton
    lifter)."""
    examples_dir = Path(__file__).parent.parent / "examples" / "mermaid"
    if not examples_dir.is_dir():
        pytest.skip("examples/mermaid not present in test layout")
    state_diagrams = [
        p
        for p in sorted(examples_dir.glob("*.mmd"))
        if any(
            line.strip().startswith("stateDiagram")
            for line in p.read_text().splitlines()
        )
    ]
    assert state_diagrams, "expected at least one stateDiagram example"
    for path in state_diagrams:
        app = philip.from_mermaid(path)
        assert len(app.graph.actions) >= 1, f"{path.name} produced empty graph"
