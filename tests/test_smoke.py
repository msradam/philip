"""Smoke tests covering the small primitives. Aim is structure, not exhaustive
coverage. Localhost-only; nothing in here needs ssh or Docker.
"""

from __future__ import annotations

from burr.core import Application, ApplicationBuilder, action

import philip


def test_version_resolves() -> None:
    assert isinstance(philip.__version__, str)
    assert philip.__version__  # non-empty


def test_public_exports_stable() -> None:
    # If anything in __all__ is removed or renamed, this catches it.
    expected_core = {
        "ActionFailureTopology",
        "DEFAULT_FACT_KEYS",
        "Edge",
        "ExcalidrawLiftError",
        "FAILURE_KINDS",
        "FAILURE_KIND_AUTH_FAILED",
        "FAILURE_KIND_MODULE_ERROR",
        "FAILURE_KIND_OK",
        "FAILURE_KIND_TIMEOUT",
        "FAILURE_KIND_UNREACHABLE",
        "FailureEdge",
        "GraphNode",
        "Host",
        "InspectionReport",
        "MermaidLiftError",
        "Node",
        "PhilipGraph",
        "SENTINEL_KEYS",
        "UnsupportedPlaybookConstruct",
        "VariableDefinition",
        "VariableProvenance",
        "VariableUse",
        "WaitGraph",
        "__version__",
        "from_excalidraw",
        "from_excalidraw_text",
        "from_mermaid",
        "from_mermaid_text",
        "from_playbook",
        "host",
        "initial_sentinels",
        "inspect",
        "module_action",
        "run_module",
        "snapshot_sentinels",
        "to_playbook",
        "wait_until",
    }
    optional_hamilton = {
        "MermaidFlowLiftError",
        "MermaidNode",
        "SqlCteLiftError",
        "SqlNode",
        "from_mermaid_flow",
        "from_mermaid_flow_text",
        "from_sql_cte",
    }
    actual = set(philip.__all__)
    # Core names must always be present; Hamilton lifters appear when the
    # optional extra is installed.
    assert expected_core.issubset(actual), f"missing core exports: {expected_core - actual}"
    extras = actual - expected_core
    assert extras.issubset(optional_hamilton), f"unexpected exports: {extras - optional_hamilton}"
    for name in actual:
        assert hasattr(philip, name), name


def test_initial_sentinels_shape() -> None:
    sentinels = philip.initial_sentinels()
    assert sentinels == {
        "_last_action": "",
        "_last_failed": False,
        "_last_changed": False,
        "_last_unreachable": False,
        "_last_msg": "",
        "_last_failure_kind": philip.FAILURE_KIND_OK,
    }


def test_host_connection_dict() -> None:
    h = philip.host(
        "target",
        ansible_host="10.0.0.5",
        ansible_port=22,
        ansible_user="ops",
        ansible_python_interpreter="/usr/bin/python3",
        become=True,
    )
    conn = h._connection()
    assert conn is not None
    assert conn["ansible_host"] == "10.0.0.5"
    assert conn["ansible_port"] == 22
    assert conn["ansible_user"] == "ops"
    assert conn["ansible_python_interpreter"] == "/usr/bin/python3"
    assert h.become is True
    assert h.name == "target"


def test_host_extra_passthrough() -> None:
    """Uncommon hostvars land in ``extra`` and reach the connection dict."""
    h = philip.host(
        "target",
        ansible_host="10.0.0.5",
        ansible_become_method="doas",  # not a known field; should go to extra
    )
    conn = h._connection()
    assert conn is not None
    assert conn["ansible_become_method"] == "doas"


def test_host_localhost_no_explicit_conn() -> None:
    """``host("localhost")`` with no hostvars returns None so the runner falls
    through to its local-connection default."""
    h = philip.host("localhost")
    assert h._connection() is None


def test_initial_facts_includes_defaults_plus_extra() -> None:
    h = philip.host("target")
    facts = h.initial_facts(extra=["ansible_local"])
    assert "ansible_os_family" in facts  # from DEFAULT_FACT_KEYS
    assert "ansible_pkg_mgr" in facts
    assert "ansible_local" in facts  # from extra
    assert facts["facts"] == {}  # all_facts=True default
    assert all(v == "" for k, v in facts.items() if k != "facts")


def test_wait_until_graph_shape() -> None:
    """``wait_until`` builds a 2-action sub-graph with the expected wiring."""

    @action(reads=[], writes=["dummy_result"])
    def dummy_check(state):  # type: ignore[no-untyped-def]
        return state.update(dummy_result="open")

    poll = philip.wait_until(
        name="readiness",
        check=dummy_check,
        condition_expr="dummy_result == 'open'",
        max_attempts=5,
        interval_s=0.01,
        on_success="done",
        on_timeout="escalate",
    )
    assert isinstance(poll, philip.WaitGraph)
    assert poll.entry == "readiness_check"
    assert set(poll.actions.keys()) == {"readiness_check", "readiness_wait"}
    assert poll.initial_state == {"readiness_attempts": 0}
    # Four transitions: success / timeout / retry / wait-back-to-check.
    assert len(poll.transitions) == 4
    sources = [t[0] for t in poll.transitions]
    destinations = [t[1] for t in poll.transitions]
    assert "readiness_check" in sources
    assert "readiness_wait" in sources
    assert "done" in destinations
    assert "escalate" in destinations


def test_wait_until_runs_to_success_immediately() -> None:
    """Synthetic FSM that drives a wait_until to its on_success terminal on
    the first check, since the condition is satisfied at start."""

    @action(reads=[], writes=["status"])
    def check_ready(state):  # type: ignore[no-untyped-def]
        return state.update(status="open")

    @action(reads=["status"], writes=["outcome"])
    def done(state):  # type: ignore[no-untyped-def]
        return state.update(outcome=f"ready ({state['status']})")

    @action(reads=[], writes=["outcome"])
    def escalate(state):  # type: ignore[no-untyped-def]
        return state.update(outcome="timed out")

    poll = philip.wait_until(
        name="r",
        check=check_ready,
        condition_expr="status == 'open'",
        max_attempts=3,
        interval_s=0.01,
        on_success="done",
        on_timeout="escalate",
    )
    app: Application = (
        ApplicationBuilder()
        .with_actions(done=done, escalate=escalate, **poll.actions)
        .with_transitions(*poll.transitions)
        .with_state(**poll.initial_state, status="", outcome="")
        .with_entrypoint(poll.entry)
        .build()
    )
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["outcome"] == "ready (open)"


def test_module_action_localhost_ping() -> None:
    """End-to-end: @module_action against ansible.builtin.ping on localhost.
    No ssh, no container. Just exercises the runner + decorator path."""

    @philip.module_action("ansible.builtin.ping", writes=["ping"])
    def ping(state):  # type: ignore[no-untyped-def]
        return {}

    @action(reads=["ping", "_last_failed"], writes=["ok"])
    def assess(state):  # type: ignore[no-untyped-def]
        return state.update(ok=(state["ping"] == "pong" and not state["_last_failed"]))

    app = (
        ApplicationBuilder()
        .with_actions(ping=ping, assess=assess)
        .with_transitions(("ping", "assess"))
        .with_state(**philip.initial_sentinels(), ping="", ok=False)
        .with_entrypoint("ping")
        .build()
    )
    last, _, final = app.run(halt_after=["assess"])
    assert last.name == "assess"
    assert final["ok"] is True
    assert final["_last_failed"] is False
    assert final["_last_action"] == "ping"


def test_module_action_register_captures_full_result() -> None:
    """``register=`` projects the entire module result dict, not just one field."""

    @philip.module_action("ansible.builtin.ping", register="full")
    def ping(state):  # type: ignore[no-untyped-def]
        return {}

    @action(reads=["full"], writes=["ok"])
    def assess(state):  # type: ignore[no-untyped-def]
        result = state["full"]
        return state.update(ok=isinstance(result, dict) and result.get("ping") == "pong")

    app = (
        ApplicationBuilder()
        .with_actions(ping=ping, assess=assess)
        .with_transitions(("ping", "assess"))
        .with_state(**philip.initial_sentinels(), full={}, ok=False)
        .with_entrypoint("ping")
        .build()
    )
    _, _, final = app.run(halt_after=["assess"])
    assert final["ok"] is True


def test_snapshot_sentinels_preserves_failure_reason() -> None:
    """The helper's whole point is keeping a failure diagnostic across a
    subsequent recovery step. Drive it with a hand-written failing state."""
    snapshot = philip.snapshot_sentinels(write="failure_reason")

    @action(reads=[], writes=["_last_action", "_last_msg", "_last_failed"])
    def stage_failure(state):  # type: ignore[no-untyped-def]
        return state.update(
            _last_action="some_module",
            _last_msg="something broke at the network layer",
            _last_failed=True,
        )

    @action(reads=["failure_reason"], writes=["outcome"])
    def report(state):  # type: ignore[no-untyped-def]
        return state.update(outcome=state["failure_reason"])

    app = (
        ApplicationBuilder()
        .with_actions(stage_failure=stage_failure, snapshot=snapshot, report=report)
        .with_transitions(
            ("stage_failure", "snapshot"),
            ("snapshot", "report"),
        )
        .with_state(
            **philip.initial_sentinels(),
            failure_reason="",
            outcome="",
        )
        .with_entrypoint("stage_failure")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    assert final["outcome"].startswith("some_module: something broke")
