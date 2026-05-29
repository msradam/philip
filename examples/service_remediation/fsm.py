"""Service-remediation FSM.

check_endpoint  --(200)-->                                    success
       |                                                      ^
       |--(non-200 / unreachable)--> bump_retries --(under)--> restart_nginx -+
                                          |                                   |
                                          +--(over MAX_RETRIES)--> escalate   |
       ^----------------------------------------------------------------------+

Run-of-show:

    ./setup.sh    # idempotent: key + image
    ./start.sh    # container with nginx NOT auto-started
    uv run python fsm.py
    ./teardown.sh
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import module_action

MAX_RETRIES = 3
TARGET_URL = "http://127.0.0.1:8080/"

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE / ".demo_key"

TARGET_HOST = "target"
TARGET_CONN: dict[str, Any] = {
    "ansible_host": "127.0.0.1",
    "ansible_port": 2222,
    "ansible_user": "ansible",
    "ansible_ssh_private_key_file": str(_DEMO_KEY),
    "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    "ansible_python_interpreter": "/usr/bin/python3",
}


@module_action(
    "ansible.builtin.uri",
    writes={"http_status": "status"},
)
def check_endpoint(state: State) -> dict[str, Any]:
    """Query the target URL from the controller. Accept any HTTP response as success.

    Setting ``status_code`` to the full 100..599 range means the uri module only
    fails on connection errors (refused, timeout). HTTP 5xx still produces a
    structured result with the status code, which the FSM branches on.
    """
    return {
        "url": TARGET_URL,
        "status_code": list(range(100, 600)),
        "timeout": 3,
    }


@module_action(
    "ansible.builtin.service",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    writes={"service_changed": "changed"},
)
def restart_nginx(state: State) -> dict[str, Any]:
    """Ensure nginx is running on the target. ``state=started`` is idempotent."""
    return {"name": "nginx", "state": "started"}


@action(reads=["retries"], writes=["retries"])
def bump_retries(state: State) -> State:
    return state.update(retries=state["retries"] + 1)


@action(reads=["http_status", "retries"], writes=["outcome"])
def success(state: State) -> State:
    return state.update(
        outcome=(
            f"HEALTHY after {state['retries']} retries "
            f"(HTTP {state['http_status']} from {TARGET_URL})"
        )
    )


@action(reads=["retries"], writes=["outcome"])
def escalate(state: State) -> State:
    return state.update(
        outcome=f"ESCALATE: still down after {state['retries']} retries; paging human"
    )


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            check_endpoint=check_endpoint,
            bump_retries=bump_retries,
            restart_nginx=restart_nginx,
            success=success,
            escalate=escalate,
        )
        .with_transitions(
            ("check_endpoint", "success", expr("http_status == 200")),
            ("check_endpoint", "bump_retries"),
            ("bump_retries", "escalate", expr(f"retries > {MAX_RETRIES}")),
            ("bump_retries", "restart_nginx"),
            ("restart_nginx", "check_endpoint"),
        )
        .with_tracker(LocalTrackingClient(project="philip-service-remediation"))
        .with_state(retries=0, http_status=-1)
        .with_entrypoint("check_endpoint")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY}. Run ./setup.sh && ./start.sh in this directory first."
        )
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["success", "escalate"])
    print(f"Final action: {last_action}")
    print(f"Outcome:      {final_state['outcome']}")
    print(f"Retries:      {final_state['retries']}")
    print(f"Last status:  HTTP {final_state['http_status']}")


if __name__ == "__main__":
    main()
