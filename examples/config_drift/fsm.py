"""Config-drift FSM: render → validate → reload-if-changed → verify.

Demonstrates two patterns this corpus didn't have yet:

1. **Handler equivalent.** Burr transition ``expr("_last_changed")``
   gates the reload action so the service is only restarted when the
   rendered config actually changed. Mirrors Ansible's ``notify`` +
   ``handlers`` pattern, but expressed as graph topology.

2. **Validate-before-apply + restore-on-fault.** ``nginx -t`` runs as
   its own action; if it fails, the FSM restores from the timestamped
   backup ``template`` created and escalates with the syntax error.

Three scenarios via env::

    uv run python fsm.py                     # default port 80; happy path
    uv run python fsm.py                     # re-run; no-change path
    LISTEN_PORT=8081 uv run python fsm.py    # valid syntax, breaks health -> escalate

Container: reuses ``examples/service_remediation`` setup (ssh on 2222,
nginx on host:8080).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import initial_sentinels, module_action, snapshot_sentinels

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE.parent / "service_remediation" / ".demo_key"
TEMPLATE_PATH = str(_HERE / "default.conf.j2")

NGINX_CONFIG_PATH = "/etc/nginx/conf.d/philip.conf"
TARGET_URL = "http://127.0.0.1:8080/"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "80"))

TARGET_HOST = "target"
TARGET_CONN: dict[str, Any] = {
    "ansible_host": "127.0.0.1",
    "ansible_port": 2222,
    "ansible_user": "ansible",
    "ansible_ssh_private_key_file": str(_DEMO_KEY),
    "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    "ansible_python_interpreter": "/usr/bin/python3",
    "listen_port": LISTEN_PORT,
}


@module_action(
    "ansible.builtin.template",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    writes={"backup_file": "backup_file"},
)
def render_config(state: State) -> dict[str, Any]:
    """Render nginx config from the Jinja template. ``backup=yes`` preserves the prior file."""
    return {
        "src": TEMPLATE_PATH,
        "dest": NGINX_CONFIG_PATH,
        "backup": True,
        "owner": "root",
        "mode": "0644",
    }


@module_action(
    "ansible.builtin.command",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def test_config(state: State) -> dict[str, Any]:
    """Run ``nginx -t`` to validate syntax before we activate the new config."""
    return {"cmd": "nginx -t"}


snapshot_failure = snapshot_sentinels(write="failure_reason")


@module_action(
    "ansible.builtin.copy",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    reads=["backup_file"],
)
def restore_backup(state: State) -> dict[str, Any]:
    """Restore the timestamped backup that ``template`` wrote before the bad change."""
    return {
        "src": state["backup_file"],
        "dest": NGINX_CONFIG_PATH,
        "remote_src": True,
        "owner": "root",
        "mode": "0644",
    }


@module_action(
    "ansible.builtin.service",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def reload_nginx(state: State) -> dict[str, Any]:
    """``state=restarted`` so it works whether nginx is currently running or not."""
    return {"name": "nginx", "state": "restarted"}


@module_action(
    "ansible.builtin.uri",
    writes={"http_status": "status"},
)
def verify_endpoint(state: State) -> dict[str, Any]:
    """Health check from the controller. Wide status_code set treats any HTTP reply as data."""
    return {
        "url": TARGET_URL,
        "status_code": list(range(100, 600)),
        "timeout": 3,
    }


@action(reads=[], writes=["recovery_mode"])
def enter_recovery(state: State) -> State:
    """Mark that we're now executing the rollback path.

    Downstream transitions (in particular the post-reload verify) branch on
    this so the same ``reload_nginx`` / ``verify_endpoint`` nodes can serve
    both the happy path and the recovery path without duplication.
    """
    return state.update(recovery_mode=True)


@action(reads=[], writes=["outcome"])
def done_noop(state: State) -> State:
    return state.update(outcome="OK: config unchanged; no reload needed")


@action(reads=["http_status"], writes=["outcome"])
def done_changed(state: State) -> State:
    return state.update(
        outcome=f"OK: config changed, reload succeeded, endpoint returned {state['http_status']}"
    )


@action(reads=["http_status", "failure_reason"], writes=["outcome"])
def rolled_back(state: State) -> State:
    return state.update(
        outcome=(
            f"ROLLED BACK: bad config restored to backup; "
            f"endpoint returned {state['http_status']} after rollback. "
            f"Original failure: {state['failure_reason']}"
        )
    )


@action(
    reads=["_last_action", "_last_msg", "http_status", "failure_reason", "recovery_mode"],
    writes=["outcome"],
)
def escalate(state: State) -> State:
    reason = state["failure_reason"] or f"{state['_last_action']}: {state['_last_msg'][:200]}"
    prefix = "ESCALATE (rollback also failed)" if state["recovery_mode"] else "ESCALATE"
    return state.update(outcome=f"{prefix}: http_status={state['http_status']} reason={reason}")


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            render_config=render_config,
            test_config=test_config,
            snapshot_failure=snapshot_failure,
            enter_recovery=enter_recovery,
            restore_backup=restore_backup,
            reload_nginx=reload_nginx,
            verify_endpoint=verify_endpoint,
            done_noop=done_noop,
            done_changed=done_changed,
            rolled_back=rolled_back,
            escalate=escalate,
        )
        .with_transitions(
            ("render_config", "done_noop", expr("not _last_changed")),
            ("render_config", "test_config"),
            ("test_config", "snapshot_failure", expr("_last_failed")),
            ("test_config", "reload_nginx"),
            # On any failure, we route through snapshot_failure -> enter_recovery
            # -> restore_backup -> reload_nginx -> verify_endpoint. The post-recovery
            # verify branches on recovery_mode so the same nodes serve both paths.
            ("snapshot_failure", "enter_recovery"),
            ("enter_recovery", "restore_backup"),
            ("restore_backup", "escalate", expr("_last_failed")),
            ("restore_backup", "reload_nginx"),
            # reload_nginx: failure during recovery is unrecoverable; failure in
            # normal mode starts the recovery; success goes to verify.
            ("reload_nginx", "escalate", expr("_last_failed and recovery_mode")),
            ("reload_nginx", "snapshot_failure", expr("_last_failed")),
            ("reload_nginx", "verify_endpoint"),
            # verify_endpoint: 200 in normal mode is success; 200 after recovery
            # means we rolled back to a working state. Non-200 starts recovery in
            # normal mode, escalates if it happens during recovery.
            ("verify_endpoint", "done_changed", expr("http_status == 200 and not recovery_mode")),
            ("verify_endpoint", "rolled_back", expr("http_status == 200")),
            ("verify_endpoint", "escalate", expr("recovery_mode")),
            ("verify_endpoint", "snapshot_failure"),
        )
        .with_tracker(LocalTrackingClient(project="philip-config-drift"))
        .with_state(
            **initial_sentinels(),
            backup_file="",
            http_status=-1,
            failure_reason="",
            recovery_mode=False,
        )
        .with_entrypoint("render_config")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY}. Run ../service_remediation/setup.sh and ./start.sh first."
        )
    app = build_application()
    last_action, _result, final_state = app.run(
        halt_after=["done_noop", "done_changed", "rolled_back", "escalate"]
    )
    print(f"LISTEN_PORT:   {LISTEN_PORT}")
    print(f"Final action:  {last_action}")
    print(f"Outcome:       {final_state['outcome']}")
    print(f"Backup file:   {final_state['backup_file'] or '(no backup needed)'}")


if __name__ == "__main__":
    main()
