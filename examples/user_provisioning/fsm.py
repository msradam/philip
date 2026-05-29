"""User-provisioning FSM with a per-item loop.

For each user in the input list:

    ensure_group -> provision_user -> deploy_key -> record -> advance -+
                       ^                                                |
                       +---(more users)---------------------------------+
                       |
                       +---(done)--> verify_all -> done

Demonstrates the loop pattern the converter will need to map to Ansible
``loop:`` directives. Loop body is three module actions per iteration
(``user`` -> ``authorized_key`` -> a pure-Python ``record``); the
back-edge predicate compares ``current_user_index`` to ``len(users)``.

Modules exercised:
  - ansible.builtin.group
  - ansible.builtin.user
  - ansible.posix.authorized_key  (different collection)
  - ansible.builtin.command       (verify step)

Reuses the ``service_remediation`` container. Run-of-show::

    cd ../service_remediation && ./start.sh
    uv run python ../user_provisioning/fsm.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import initial_sentinels, module_action, snapshot_sentinels

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE.parent / "service_remediation" / ".demo_key"
_DEMO_PUBKEY = _DEMO_KEY.with_suffix(".pub")

USERS = ["alice", "bob", "charlie"]
GROUP_NAME = "philipians"

TARGET_HOST = "target"
TARGET_CONN: dict[str, Any] = {
    "ansible_host": "127.0.0.1",
    "ansible_port": 2222,
    "ansible_user": "ansible",
    "ansible_ssh_private_key_file": str(_DEMO_KEY),
    "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    "ansible_python_interpreter": "/usr/bin/python3",
}


@action(reads=[], writes=["current_user_index", "provisioned_users"])
def start_loop(state: State) -> State:
    return state.update(current_user_index=0, provisioned_users=[])


@module_action(
    "ansible.builtin.group",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def ensure_group(state: State) -> dict[str, Any]:
    return {"name": GROUP_NAME, "state": "present"}


@module_action(
    "ansible.builtin.user",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    reads=["users", "current_user_index"],
)
def provision_user(state: State) -> dict[str, Any]:
    user = state["users"][state["current_user_index"]]
    return {
        "name": user,
        "group": GROUP_NAME,
        "shell": "/bin/bash",
        "create_home": True,
        "state": "present",
    }


@module_action(
    "ansible.posix.authorized_key",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    reads=["users", "current_user_index", "ssh_pubkey"],
)
def deploy_key(state: State) -> dict[str, Any]:
    user = state["users"][state["current_user_index"]]
    return {"user": user, "key": state["ssh_pubkey"], "state": "present"}


@action(
    reads=["users", "current_user_index", "provisioned_users"],
    writes=["provisioned_users"],
)
def record(state: State) -> State:
    user = state["users"][state["current_user_index"]]
    return state.update(provisioned_users=[*state["provisioned_users"], user])


@action(reads=["current_user_index"], writes=["current_user_index"])
def advance(state: State) -> State:
    return state.update(current_user_index=state["current_user_index"] + 1)


@module_action(
    "ansible.builtin.command",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    reads=["users"],
    writes={"verify_stdout": "stdout"},
)
def verify_all(state: State) -> dict[str, Any]:
    names = " ".join(state["users"])
    return {"cmd": f"getent passwd {names}"}


@action(reads=["provisioned_users", "verify_stdout"], writes=["outcome"])
def done(state: State) -> State:
    lines = (state["verify_stdout"] or "").strip().splitlines()
    return state.update(
        outcome=(
            f"OK: provisioned {len(state['provisioned_users'])} users "
            f"({', '.join(state['provisioned_users'])}); "
            f"verify returned {len(lines)} matching entries"
        )
    )


@action(
    reads=["provisioned_users", "current_user_index", "failure_reason"],
    writes=["outcome"],
)
def escalate(state: State) -> State:
    return state.update(
        outcome=(
            f"ESCALATE at user_index={state['current_user_index']} "
            f"(provisioned so far: {state['provisioned_users']}); "
            f"reason={state['failure_reason']}"
        )
    )


snapshot_failure = snapshot_sentinels(write="failure_reason")


def build_application(users: list[str], ssh_pubkey: str) -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            start_loop=start_loop,
            ensure_group=ensure_group,
            provision_user=provision_user,
            deploy_key=deploy_key,
            record=record,
            advance=advance,
            verify_all=verify_all,
            snapshot_failure=snapshot_failure,
            done=done,
            escalate=escalate,
        )
        .with_transitions(
            ("start_loop", "ensure_group"),
            ("ensure_group", "snapshot_failure", expr("_last_failed")),
            ("ensure_group", "provision_user"),
            ("provision_user", "snapshot_failure", expr("_last_failed")),
            ("provision_user", "deploy_key"),
            ("deploy_key", "snapshot_failure", expr("_last_failed")),
            ("deploy_key", "record"),
            ("record", "advance"),
            ("advance", "verify_all", expr("current_user_index >= len(users)")),
            ("advance", "provision_user"),
            ("verify_all", "snapshot_failure", expr("_last_failed")),
            ("verify_all", "done"),
            ("snapshot_failure", "escalate"),
        )
        .with_tracker(LocalTrackingClient(project="philip-user-provisioning"))
        .with_state(
            **initial_sentinels(),
            users=users,
            ssh_pubkey=ssh_pubkey,
            current_user_index=0,
            provisioned_users=[],
            verify_stdout="",
            failure_reason="",
        )
        .with_entrypoint("start_loop")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists() or not _DEMO_PUBKEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY} or {_DEMO_PUBKEY}. Run ../service_remediation/setup.sh first."
        )
    ssh_pubkey = _DEMO_PUBKEY.read_text().strip()
    app = build_application(USERS, ssh_pubkey)
    last_action, _result, final_state = app.run(halt_after=["done", "escalate"])
    print(f"Users:                {USERS}")
    print(f"Final action:         {last_action}")
    print(f"Outcome:              {final_state['outcome']}")
    print(f"Provisioned:          {final_state['provisioned_users']}")
    print(f"Final user_index:     {final_state['current_user_index']}")
    print(f"Verify stdout (head): {(final_state['verify_stdout'] or '').splitlines()[:3]}")


if __name__ == "__main__":
    main()
