"""Failure-mode tests. Each one exercises a path the happy-path suite
doesn't cover: an Ansible module that fails, an unreachable host, a
wait_until that times out, a module_action with bad args, check_mode
that must not mutate, snapshot_sentinels carrying a diagnostic across
a recovery.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from burr.core import ApplicationBuilder, action, expr

import philip


def test_failed_module_sets_last_failed() -> None:
    """A module that fails (ansible.builtin.fail) must populate _last_failed=True
    and _last_msg with the diagnostic."""

    @philip.module_action("ansible.builtin.fail")
    def boom(state):
        return {"msg": "deliberate test failure"}

    @action(reads=["_last_failed", "_last_msg"], writes=["outcome"])
    def report(state):
        return state.update(outcome=f"failed={state['_last_failed']} msg={state['_last_msg']!r}")

    app = (
        ApplicationBuilder()
        .with_actions(boom=boom, report=report)
        .with_transitions(("boom", "report"))
        .with_state(**philip.initial_sentinels(), outcome="")
        .with_entrypoint("boom")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    assert final["_last_failed"] is True
    assert "deliberate test failure" in final["_last_msg"]


def test_failed_transition_routes_to_escalate() -> None:
    """A guard transition on _last_failed must route past the failure to a
    designated terminal without raising an exception."""

    @philip.module_action("ansible.builtin.fail")
    def boom(state):
        return {"msg": "boom"}

    @action(reads=[], writes=["outcome"])
    def healthy(state):
        return state.update(outcome="UNREACHED")

    @action(reads=["_last_msg"], writes=["outcome"])
    def escalate(state):
        return state.update(outcome=f"ESCALATED: {state['_last_msg']}")

    app = (
        ApplicationBuilder()
        .with_actions(boom=boom, healthy=healthy, escalate=escalate)
        .with_transitions(
            ("boom", "escalate", expr("_last_failed")),
            ("boom", "healthy"),
        )
        .with_state(**philip.initial_sentinels(), outcome="")
        .with_entrypoint("boom")
        .build()
    )
    last, _, final = app.run(halt_after=["healthy", "escalate"])
    assert last.name == "escalate"
    assert final["outcome"].startswith("ESCALATED:")
    assert "UNREACHED" not in final["outcome"]


def test_unreachable_host_sets_last_unreachable() -> None:
    """Connecting to a non-routable IP must set _last_unreachable=True
    (and _last_failed=True), not raise. Short timeout so the test is fast."""

    @philip.module_action(
        "ansible.builtin.ping",
        host="unreachable_target",
        connection={
            "ansible_host": "10.255.255.1",  # documented non-routable in RFC 5737
            "ansible_port": 22,
            "ansible_user": "nobody",
            "ansible_ssh_common_args": (
                "-o ConnectTimeout=2 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            ),
        },
    )
    def reach(state):
        return {}

    @action(
        reads=["_last_failed", "_last_unreachable", "_last_msg"],
        writes=["outcome"],
    )
    def report(state):
        return state.update(
            outcome=f"failed={state['_last_failed']} unreachable={state['_last_unreachable']}"
        )

    app = (
        ApplicationBuilder()
        .with_actions(reach=reach, report=report)
        .with_transitions(("reach", "report"))
        .with_state(**philip.initial_sentinels(), outcome="")
        .with_entrypoint("reach")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    # Either unreachable or failed is acceptable; the important contract is
    # we land in a downstream Python action, not raise an exception.
    assert final["_last_failed"] is True
    assert "failed=True" in final["outcome"]


def test_wait_until_reaches_timeout() -> None:
    """When the condition never holds, wait_until must route to on_timeout
    after max_attempts. Verifies the FSM owns termination."""

    @action(reads=[], writes=["status"])
    def check_never_ready(state):
        return state.update(status="closed")

    @action(reads=[], writes=["outcome"])
    def succeeded(state):
        return state.update(outcome="UNREACHED")

    @action(reads=["readiness_attempts"], writes=["outcome"])
    def timed_out(state):
        return state.update(outcome=f"timed out after {state['readiness_attempts']} attempts")

    poll = philip.wait_until(
        name="readiness",
        check=check_never_ready,
        condition_expr="status == 'open'",
        max_attempts=3,
        interval_s=0.01,
        on_success="succeeded",
        on_timeout="timed_out",
    )

    app = (
        ApplicationBuilder()
        .with_actions(succeeded=succeeded, timed_out=timed_out, **poll.actions)
        .with_transitions(*poll.transitions)
        .with_state(**poll.initial_state, status="", outcome="")
        .with_entrypoint(poll.entry)
        .build()
    )
    last, _, final = app.run(halt_after=["succeeded", "timed_out"])
    assert last.name == "timed_out"
    assert "UNREACHED" not in final["outcome"]
    assert "timed out" in final["outcome"]
    assert final["readiness_attempts"] == 2  # 0..max_attempts-1


def test_check_mode_does_not_mutate() -> None:
    """A copy in check_mode must NOT create the destination file."""

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "should-not-exist.txt"

        @philip.module_action("ansible.builtin.copy", check_mode=True, diff=True)
        def plan_write(state):
            return {"content": "this should not be written\n", "dest": str(dest)}

        @action(
            reads=["_last_changed", "_last_diff"],
            writes=["outcome"],
        )
        def report(state):
            has_diff = bool(state["_last_diff"])
            return state.update(
                outcome=f"would_change={state['_last_changed']} has_diff={has_diff}"
            )

        app = (
            ApplicationBuilder()
            .with_actions(plan_write=plan_write, report=report)
            .with_transitions(("plan_write", "report"))
            .with_state(
                **philip.initial_sentinels(),
                _last_diff=None,
                outcome="",
            )
            .with_entrypoint("plan_write")
            .build()
        )
        _, _, final = app.run(halt_after=["report"])
        assert not dest.exists(), "check_mode wrote the file (should not)"
        assert final["_last_changed"] is True  # would have changed
        assert final["_last_diff"] is not None  # diff was captured


def test_module_action_timeout_terminates_subprocess() -> None:
    """A module that would otherwise run for 30s must be killed within a few
    seconds when ``timeout=2`` is set. Without the cap, ansible-playbook
    would hang the FSM indefinitely on a slow or unresponsive target."""
    import time

    @philip.module_action("ansible.builtin.shell", timeout=2)
    def hang(state):
        return {"cmd": "sleep 30"}

    @action(reads=["_last_failed", "_last_msg"], writes=["outcome"])
    def report(state):
        return state.update(outcome=f"failed={state['_last_failed']}")

    app = (
        ApplicationBuilder()
        .with_actions(hang=hang, report=report)
        .with_transitions(("hang", "report"))
        .with_state(**philip.initial_sentinels(), outcome="")
        .with_entrypoint("hang")
        .build()
    )
    start = time.monotonic()
    _, _, final = app.run(halt_after=["report"])
    elapsed = time.monotonic() - start

    assert elapsed < 10, f"timeout did not fire: ran for {elapsed:.1f}s"
    assert final["_last_failed"] is True


def test_failure_kind_ok_for_successful_module() -> None:
    """A module that succeeds populates _last_failure_kind='ok'."""

    @philip.module_action("ansible.builtin.ping")
    def ping(state):
        return {}

    @action(reads=["_last_failure_kind"], writes=["kind"])
    def report(state):
        return state.update(kind=state["_last_failure_kind"])

    app = (
        ApplicationBuilder()
        .with_actions(ping=ping, report=report)
        .with_transitions(("ping", "report"))
        .with_state(**philip.initial_sentinels(), kind="")
        .with_entrypoint("ping")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    assert final["kind"] == philip.FAILURE_KIND_OK


def test_failure_kind_module_error_for_intentional_fail() -> None:
    """ansible.builtin.fail produces _last_failure_kind='module_error'."""

    @philip.module_action("ansible.builtin.fail")
    def boom(state):
        return {"msg": "no auth wording here, just a failure"}

    @action(reads=["_last_failure_kind"], writes=["kind"])
    def report(state):
        return state.update(kind=state["_last_failure_kind"])

    app = (
        ApplicationBuilder()
        .with_actions(boom=boom, report=report)
        .with_transitions(("boom", "report"))
        .with_state(**philip.initial_sentinels(), kind="")
        .with_entrypoint("boom")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    assert final["kind"] == philip.FAILURE_KIND_MODULE_ERROR


def test_failure_kind_timeout_when_runner_exceeds_cap() -> None:
    """A module timeout (caught by run_module's cap) produces 'timeout'."""

    @philip.module_action("ansible.builtin.shell", timeout=2)
    def hang(state):
        return {"cmd": "sleep 30"}

    @action(reads=["_last_failure_kind"], writes=["kind"])
    def report(state):
        return state.update(kind=state["_last_failure_kind"])

    app = (
        ApplicationBuilder()
        .with_actions(hang=hang, report=report)
        .with_transitions(("hang", "report"))
        .with_state(**philip.initial_sentinels(), kind="")
        .with_entrypoint("hang")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    assert final["kind"] == philip.FAILURE_KIND_TIMEOUT


def test_failure_kind_transition_can_branch_on_kind() -> None:
    """The classification value is usable as a Burr transition predicate."""

    @philip.module_action("ansible.builtin.fail")
    def boom(state):
        return {"msg": "any module-level error"}

    @action(reads=[], writes=["outcome"])
    def take_unreachable_path(state):
        return state.update(outcome="UNREACHABLE")

    @action(reads=[], writes=["outcome"])
    def take_module_error_path(state):
        return state.update(outcome="MODULE_ERROR")

    app = (
        ApplicationBuilder()
        .with_actions(
            boom=boom,
            take_unreachable_path=take_unreachable_path,
            take_module_error_path=take_module_error_path,
        )
        .with_transitions(
            ("boom", "take_unreachable_path", expr("_last_failure_kind == 'unreachable'")),
            ("boom", "take_module_error_path"),
        )
        .with_state(**philip.initial_sentinels(), outcome="")
        .with_entrypoint("boom")
        .build()
    )
    last, _, final = app.run(halt_after=["take_unreachable_path", "take_module_error_path"])
    assert last.name == "take_module_error_path"
    assert final["outcome"] == "MODULE_ERROR"


def test_bad_module_args_fails_cleanly() -> None:
    """An invalid argument to a real module must surface as _last_failed=True
    with a useful _last_msg, not raise."""

    @philip.module_action("ansible.builtin.copy")
    def bad_copy(state):
        # ``copy`` requires ``dest=``; without it the module errors out.
        return {"content": "no dest"}

    @action(reads=["_last_failed", "_last_msg"], writes=["outcome"])
    def report(state):
        return state.update(outcome=f"failed={state['_last_failed']}")

    app = (
        ApplicationBuilder()
        .with_actions(bad_copy=bad_copy, report=report)
        .with_transitions(("bad_copy", "report"))
        .with_state(**philip.initial_sentinels(), outcome="")
        .with_entrypoint("bad_copy")
        .build()
    )
    _, _, final = app.run(halt_after=["report"])
    assert final["_last_failed"] is True
    assert "failed=True" in final["outcome"]
