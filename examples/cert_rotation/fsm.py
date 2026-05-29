"""TLS cert rotation FSM, using community.crypto modules.

Flow::

    read_cert -> assess_expiry --(valid)-----------------------> already_valid
                       \\                                                ^
                        \\--(missing/expiring)--> ensure_ssl_dir -> generate_key
                                                       -> generate_cert -> verify_cert -> rotated

Every step out of the renewal path also has an ``expr("_last_failed")``
guard edge into a single ``escalate`` terminal. Branching uses the ambient
``_last_failed`` / ``_last_msg`` sentinels that philip writes on every
module action; no per-action ``writes`` boilerplate needed.

The FSM reuses the service_remediation container (which ships
``python3-cryptography`` for community.crypto's target-side ops).

Run-of-show (service_remediation container must already be up)::

    cd ../service_remediation && ./setup.sh && ./start.sh
    cd ../cert_rotation && uv run python fsm.py
    # re-run: should hit the already_valid path
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import initial_sentinels, module_action

THRESHOLD_DAYS = 30
VALIDITY_DAYS = 90
CERT_PATH = "/etc/nginx/ssl/server.crt"
KEY_PATH = "/etc/nginx/ssl/server.key"

_DEMO_KEY = Path(__file__).resolve().parent.parent / "service_remediation" / ".demo_key"

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
    "community.crypto.x509_certificate_info",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    writes={"cert_not_after": "not_after"},
)
def read_cert(state: State) -> dict[str, Any]:
    """Inspect the cert at CERT_PATH. Missing file lands as _last_failed=True."""
    return {"path": CERT_PATH}


@action(
    reads=["cert_not_after", "_last_failed"],
    writes=["needs_renewal", "days_remaining"],
)
def assess_expiry(state: State) -> State:
    """Decide whether the cert needs renewal: missing, unreadable, or expiring soon."""
    if state["_last_failed"]:
        return state.update(needs_renewal=True, days_remaining=-1)
    raw = str(state["cert_not_after"])
    not_after = datetime.strptime(raw, "%Y%m%d%H%M%SZ").replace(tzinfo=UTC)
    days_remaining = (not_after - datetime.now(UTC)).days
    return state.update(
        needs_renewal=days_remaining < THRESHOLD_DAYS,
        days_remaining=days_remaining,
    )


@module_action(
    "ansible.builtin.file",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def ensure_ssl_dir(state: State) -> dict[str, Any]:
    return {"path": "/etc/nginx/ssl", "state": "directory", "mode": "0755", "owner": "root"}


@module_action(
    "community.crypto.openssl_privatekey",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def generate_key(state: State) -> dict[str, Any]:
    return {"path": KEY_PATH, "size": 2048, "type": "RSA", "mode": "0600"}


@module_action(
    "community.crypto.x509_certificate",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def generate_cert(state: State) -> dict[str, Any]:
    return {
        "path": CERT_PATH,
        "privatekey_path": KEY_PATH,
        "provider": "selfsigned",
        "selfsigned_not_after": f"+{VALIDITY_DAYS}d",
        "selfsigned_create_subject_key_identifier": "always_create",
        "owner": "root",
        "mode": "0644",
    }


@module_action(
    "community.crypto.x509_certificate_info",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    writes={"new_cert_not_after": "not_after"},
)
def verify_cert(state: State) -> dict[str, Any]:
    return {"path": CERT_PATH}


@action(reads=["days_remaining"], writes=["outcome"])
def already_valid(state: State) -> State:
    return state.update(
        outcome=f"OK: cert valid for {state['days_remaining']} more days; no action needed"
    )


@action(reads=["new_cert_not_after"], writes=["outcome"])
def rotated(state: State) -> State:
    return state.update(outcome=f"ROTATED: new cert valid until {state['new_cert_not_after']}")


@action(reads=["_last_action", "_last_msg"], writes=["outcome"])
def escalate(state: State) -> State:
    return state.update(
        outcome=(f"ESCALATE: failed at {state['_last_action']}: {state['_last_msg'][:200]}")
    )


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            read_cert=read_cert,
            assess_expiry=assess_expiry,
            ensure_ssl_dir=ensure_ssl_dir,
            generate_key=generate_key,
            generate_cert=generate_cert,
            verify_cert=verify_cert,
            already_valid=already_valid,
            rotated=rotated,
            escalate=escalate,
        )
        .with_transitions(
            ("read_cert", "assess_expiry"),
            ("assess_expiry", "already_valid", expr("not needs_renewal")),
            ("assess_expiry", "ensure_ssl_dir"),
            ("ensure_ssl_dir", "escalate", expr("_last_failed")),
            ("ensure_ssl_dir", "generate_key"),
            ("generate_key", "escalate", expr("_last_failed")),
            ("generate_key", "generate_cert"),
            ("generate_cert", "escalate", expr("_last_failed")),
            ("generate_cert", "verify_cert"),
            ("verify_cert", "escalate", expr("_last_failed")),
            ("verify_cert", "rotated"),
        )
        .with_tracker(LocalTrackingClient(project="philip-cert-rotation"))
        .with_state(
            **initial_sentinels(),
            cert_not_after="",
            new_cert_not_after="",
            needs_renewal=False,
            days_remaining=0,
        )
        .with_entrypoint("read_cert")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY}. Run ../service_remediation/setup.sh and ./start.sh first."
        )
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["already_valid", "rotated", "escalate"])
    print(f"Final action:        {last_action}")
    print(f"Outcome:             {final_state['outcome']}")
    print(f"Days remaining:      {final_state['days_remaining']}")
    print(f"Old not_after:       {final_state['cert_not_after'] or '(no prior cert)'}")
    print(f"New not_after:       {final_state['new_cert_not_after'] or '(unchanged)'}")


if __name__ == "__main__":
    main()
