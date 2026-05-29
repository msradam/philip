"""Plan-then-apply FSM. Every change is previewed in check+diff mode,
reviewed by a deterministic or LLM-driven policy, then either applied for
real or escalated.

Maps to the MAST paper's force-clarification recommendation (FM-2.2)
using Ansible's ``--check`` and ``--diff`` primitives. The review action
reads the structured diff the plan step produced, applies policy, and
decides whether the apply step runs. The same module configuration is
used for both plan and apply; only ``check_mode`` flips.

Topology::

    ensure_baseline -> plan_listener_change -> review_plan
                                                    |
        +-(approved)----> apply_listener_change -> reload_nginx -> verify -> done
        +-(rejected)----> escalate

State carries:
  - ``_last_diff``: structured before/after from the plan (Ansible's diff field)
  - ``review_decision``: approve | reject (set by policy)
  - ``review_reason``: human-readable explanation of the decision

Library features exercised:
  - ``Host.copy(check_mode=True, diff=True)`` for plan steps
  - ``Host.copy()`` (no check) for apply steps; same module args helper
  - ``_last_diff`` state field populated by diff=True module calls
  - Pure-Python policy gate between plan and apply

Run::

    cd ../service_remediation && ./start.sh
    uv run python examples/plan_then_apply/fsm.py
    NEW_MESSAGE="malicious change very long string ..." uv run python ... # rejection demo
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import host, initial_sentinels, module_action, wait_until

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE.parent / "service_remediation" / ".demo_key"

CONFIG_PATH = "/etc/nginx/conf.d/philip-plan-apply.conf"
TARGET_URL = "http://127.0.0.1:8080/"
NEW_MESSAGE = os.environ.get("NEW_MESSAGE", "philip plan-then-apply demo")
MAX_DIFF_BYTES = int(os.environ.get("MAX_DIFF_BYTES", "200"))


target = host(
    "target",
    ansible_host="127.0.0.1",
    ansible_port=2222,
    ansible_user="ansible",
    ansible_ssh_private_key_file=str(_DEMO_KEY),
    ansible_ssh_common_args="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    ansible_python_interpreter="/usr/bin/python3",
    become=True,
)


def _server_block(message: str) -> str:
    return (
        "server {\n"
        "    listen 80 default_server;\n"
        "    server_name _;\n"
        "    location / {\n"
        f'        return 200 "{message}\\n";\n'
        "        add_header Content-Type text/plain;\n"
        "    }\n"
        "}\n"
    )


# ---- Baseline + plan (same module args; check_mode flips) ----------------


@target.copy()
def ensure_baseline(state: State) -> dict[str, Any]:
    """Make sure SOMETHING is serving on port 80 first. Idempotent if already
    in the right shape."""
    return {
        "content": _server_block("baseline message"),
        "dest": CONFIG_PATH,
        "mode": "0644",
    }


@target.service()
def reload_baseline(state: State) -> dict[str, Any]:
    return {"name": "nginx", "state": "restarted"}


@target.copy(check_mode=True, diff=True)
def plan_listener_change(state: State) -> dict[str, Any]:
    """Same args as the apply step, but ``check_mode=True`` means the module
    reports what it would change without changing it. ``diff=True`` makes
    it return structured before/after content under ``result['diff']``,
    which @module_action projects to ``state['_last_diff']``.
    """
    return {
        "content": _server_block(NEW_MESSAGE),
        "dest": CONFIG_PATH,
        "mode": "0644",
    }


# ---- Review (the clarification gate) ------------------------------------


@action(
    reads=["_last_changed", "_last_diff", "_last_failed"],
    writes=["review_decision", "review_reason"],
)
def review_plan(state: State) -> State:
    """Deterministic policy applied to the plan's diff.

    Replace this with an LLM consultation, a human-approval hook, or an
    OPA/Rego policy as needed. The MAST recommendation is to keep this
    gate external to the LLM, regardless of implementation. The contract
    is: an Ansible action does not run on production until something
    other than the actor that proposed it has approved.
    """
    if state["_last_failed"]:
        return state.update(
            review_decision="reject",
            review_reason="plan step itself failed; refusing to apply",
        )
    if not state["_last_changed"]:
        return state.update(
            review_decision="approve",
            review_reason="plan reports no-op; apply is also no-op (idempotent)",
        )
    diff = state["_last_diff"] or []
    rendered = ""
    for entry in diff:
        if isinstance(entry, dict):
            rendered += str(entry.get("after", ""))
    size = len(rendered)
    if size > MAX_DIFF_BYTES:
        return state.update(
            review_decision="reject",
            review_reason=(
                f"diff size {size}B exceeds MAX_DIFF_BYTES={MAX_DIFF_BYTES}B; "
                f"escalating to human review"
            ),
        )
    return state.update(
        review_decision="approve",
        review_reason=f"diff size {size}B within policy ({MAX_DIFF_BYTES}B); proceeding",
    )


# ---- Apply (same args as plan, no check_mode) ----------------------------


@target.copy()
def apply_listener_change(state: State) -> dict[str, Any]:
    """Real write. Same args as ``plan_listener_change`` minus ``check_mode``."""
    return {
        "content": _server_block(NEW_MESSAGE),
        "dest": CONFIG_PATH,
        "mode": "0644",
    }


@target.service()
def reload_nginx(state: State) -> dict[str, Any]:
    return {"name": "nginx", "state": "restarted"}


# The polling sub-graph for "wait until nginx is accepting connections again".
# Each attempt is a discrete Burr step (one ``shell`` invocation per poll), so
# the trace shows whether we succeeded on the first attempt or polled N times.
# Compare to wrapping ``ansible.builtin.wait_for`` directly, which would
# collapse the whole loop into one opaque step.
@target.shell(register="port_check")
def check_port_open(state: State) -> dict[str, Any]:
    # python3 is already required on the target for ansible; using it for
    # the port check avoids depending on nc/netcat being installed.
    return {
        "cmd": (
            'python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); '
            "sys.exit(s.connect_ex(('127.0.0.1', 80)))\" && echo open || echo closed"
        ),
    }


wait_listener = wait_until(
    name="wait_listener",
    check=check_port_open,
    condition_expr="port_check.get('stdout', '').strip() == 'open'",
    max_attempts=10,
    interval_s=0.5,
    on_success="verify",
    on_timeout="escalate",
)


# Runs on the controller (localhost) so the URL hits the host's port mapping
# (8080 -> container:80); from inside the target the port mapping doesn't exist.
@module_action("ansible.builtin.uri", writes={"http_status": "status", "http_body": "content"})
def verify(state: State) -> dict[str, Any]:
    return {
        "url": TARGET_URL,
        "status_code": list(range(100, 600)),
        "return_content": True,
        "timeout": 3,
    }


# ---- Terminals ----------------------------------------------------------


@action(
    reads=["http_status", "http_body", "review_reason"],
    writes=["outcome"],
)
def done(state: State) -> State:
    body = (state["http_body"] or "").strip()
    return state.update(
        outcome=(
            f"OK: applied (review: {state['review_reason']}); "
            f"endpoint returned {state['http_status']} body={body!r}"
        )
    )


@action(
    reads=["review_reason", "_last_failed", "_last_msg"],
    writes=["outcome"],
)
def escalate(state: State) -> State:
    return state.update(outcome=f"ESCALATE: not applied. {state['review_reason']}")


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            ensure_baseline=ensure_baseline,
            reload_baseline=reload_baseline,
            plan_listener_change=plan_listener_change,
            review_plan=review_plan,
            apply_listener_change=apply_listener_change,
            reload_nginx=reload_nginx,
            **wait_listener.actions,
            verify=verify,
            done=done,
            escalate=escalate,
        )
        .with_transitions(
            ("ensure_baseline", "reload_baseline"),
            ("reload_baseline", "plan_listener_change"),
            ("plan_listener_change", "review_plan"),
            ("review_plan", "apply_listener_change", expr("review_decision == 'approve'")),
            ("review_plan", "escalate"),
            ("apply_listener_change", "reload_nginx"),
            ("reload_nginx", wait_listener.entry),
            *wait_listener.transitions,
            ("verify", "done"),
        )
        .with_tracker(LocalTrackingClient(project="philip-plan-then-apply"))
        .with_state(
            **initial_sentinels(),
            **wait_listener.initial_state,
            _last_diff=None,
            port_check={},
            http_status=-1,
            http_body="",
            review_decision="",
            review_reason="",
            outcome="",
        )
        .with_entrypoint("ensure_baseline")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY}. Run ../service_remediation/setup.sh && ./start.sh first."
        )
    print(f"NEW_MESSAGE:     {NEW_MESSAGE!r}")
    print(f"MAX_DIFF_BYTES:  {MAX_DIFF_BYTES}")
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["done", "escalate"])
    print(f"Final action:    {last_action}")
    print(f"Outcome:         {final_state['outcome']}")
    print(f"Review decision: {final_state['review_decision']}")
    print(f"Review reason:   {final_state['review_reason']}")


if __name__ == "__main__":
    main()
