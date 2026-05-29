"""Tests for ``philip.inspect`` (variable provenance + failure topology)."""

from __future__ import annotations

from pathlib import Path

import pytest

import philip

FIXTURES = Path(__file__).parent / "fixtures"


def test_inspect_returns_report_object():
    report = philip.inspect(FIXTURES / "playbook_simple.yml")
    assert isinstance(report, philip.InspectionReport)
    assert report.playbook_path.endswith("playbook_simple.yml")


def test_inspect_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        philip.inspect(FIXTURES / "does_not_exist.yml")


def test_register_definition_detected_and_marked_unused():
    report = philip.inspect(FIXTURES / "playbook_register.yml")
    ping_result = next(v for v in report.variables if v.name == "ping_result")
    assert any(d.source == "register" for d in ping_result.definitions)
    assert ping_result.is_unused


def test_when_predicate_use_detected():
    report = philip.inspect(FIXTURES / "playbook_with_when.yml")
    names = {v.name for v in report.variables}
    assert names  # at least one variable referenced in `when:`
    # Any variable with at least one use should appear, even if undefined
    has_use = any(len(v.uses) > 0 for v in report.variables)
    assert has_use


def test_set_fact_fqcn_recognised_as_definition():
    """The advanced fixture uses ``ansible.builtin.set_fact``."""
    advanced = Path(__file__).parent.parent / "examples" / "from_playbook_advanced" / "playbook.yml"
    if not advanced.exists():
        pytest.skip("advanced example not shipped in test layout")
    report = philip.inspect(advanced)
    marker = next((v for v in report.variables if v.name == "marker_file"), None)
    assert marker is not None
    assert any(d.source == "set_fact" for d in marker.definitions)
    assert not marker.is_undefined


def test_playbook_vars_definition_detected():
    advanced = Path(__file__).parent.parent / "examples" / "from_playbook_advanced" / "playbook.yml"
    if not advanced.exists():
        pytest.skip("advanced example not shipped in test layout")
    report = philip.inspect(advanced)
    workspace = next((v for v in report.variables if v.name == "workspace_dir"), None)
    assert workspace is not None
    assert any(d.source == "playbook_vars" for d in workspace.definitions)


def test_extra_vars_become_definitions():
    report = philip.inspect(FIXTURES / "playbook_simple.yml", extra_vars={"injected_name": "value"})
    injected = next((v for v in report.variables if v.name == "injected_name"), None)
    assert injected is not None
    assert any(d.source == "extra_vars" for d in injected.definitions)


def test_runtime_provided_names_are_filtered():
    """ansible_facts, inventory_hostname etc. should never appear as 'undefined'."""
    advanced = Path(__file__).parent.parent / "examples" / "from_playbook_advanced" / "playbook.yml"
    if not advanced.exists():
        pytest.skip("advanced example not shipped")
    report = philip.inspect(advanced)
    names = {v.name for v in report.variables}
    assert "ansible_facts" not in names
    assert "inventory_hostname" not in names
    assert "item" not in names


def test_failure_topology_built():
    report = philip.inspect(FIXTURES / "playbook_register.yml")
    assert len(report.failure_topology) >= 1
    for act in report.failure_topology:
        kinds = {e.failure_kind for e in act.edges}
        assert "unreachable" in kinds
        assert "auth_failed" in kinds
        assert "timeout" in kinds
        assert "module_error" in kinds


def test_failure_routing_to_escalate_is_not_unhandled():
    """A module action routing to escalate is intended structural recovery."""
    report = philip.inspect(FIXTURES / "playbook_register.yml")
    module_acts = [a for a in report.failure_topology if a.action == "ping_with_register"]
    assert module_acts, "expected ping_with_register in topology"
    act = module_acts[0]
    # Every edge for this module action goes to escalate; that's structural,
    # not unhandled.
    for e in act.edges:
        assert e.destination == "escalate"
        assert e.is_escalation
        assert not e.is_unhandled


def test_markdown_renders_without_error():
    report = philip.inspect(FIXTURES / "playbook_register.yml")
    md = report.rendered_markdown()
    assert "philip inspect" in md
    assert "Variable provenance" in md
    assert "Failure topology" in md


def test_inspect_refuses_unsupported_construct_gracefully():
    """An unsupported construct should appear in the report, not crash."""
    unsupported_fixture = FIXTURES / "playbook_unsupported_block.yml"
    if not unsupported_fixture.exists():
        pytest.skip("unsupported fixture not present")
    report = philip.inspect(unsupported_fixture)
    # Either from_playbook lifted it (so topology is non-empty) or refused
    # cleanly (so unsupported_constructs is non-empty). Both are valid.
    assert report.failure_topology or report.unsupported_constructs
