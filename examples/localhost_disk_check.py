"""Localhost disk-pressure FSM.

ping → uptime → df → assess → (warn | ok)

Each Ansible-backed node is a ``@module_action``. The assess node is a plain
Burr ``@action`` that parses ``df`` output and decides which terminal to
transition to. Run with: ``uv run python examples/localhost_disk_check.py``.
"""

from __future__ import annotations

import re
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import module_action

DISK_PRESSURE_THRESHOLD_PCT = 80


@module_action("ansible.builtin.ping", writes=["ping"])
def ping(state: State) -> dict[str, Any]:
    """Verify the local Ansible runtime is healthy."""
    return {}


@module_action("ansible.builtin.shell", writes={"uptime_stdout": "stdout"})
def gather_uptime(state: State) -> dict[str, Any]:
    """Run ``uptime`` and capture stdout."""
    return {"cmd": "uptime"}


@module_action("ansible.builtin.shell", writes={"df_stdout": "stdout"})
def check_disk(state: State) -> dict[str, Any]:
    """Run ``df -k /`` and capture stdout for parsing."""
    return {"cmd": "df -k /"}


@action(reads=["df_stdout"], writes=["usage_pct"])
def assess(state: State) -> State:
    """Parse df output and write the root-filesystem usage percentage."""
    stdout: str = state["df_stdout"] or ""
    match = re.search(r"\s(\d+)%", stdout)
    usage_pct = int(match.group(1)) if match else -1
    return state.update(usage_pct=usage_pct)


@action(reads=["usage_pct", "uptime_stdout"], writes=["status"])
def ok(state: State) -> State:
    """Terminal: disk usage is below the pressure threshold."""
    return state.update(status=f"ok (usage={state['usage_pct']}%)")


@action(reads=["usage_pct"], writes=["status"])
def warn(state: State) -> State:
    """Terminal: disk usage is at or above the pressure threshold."""
    return state.update(status=f"WARN: disk pressure (usage={state['usage_pct']}%)")


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            ping=ping,
            gather_uptime=gather_uptime,
            check_disk=check_disk,
            assess=assess,
            ok=ok,
            warn=warn,
        )
        .with_transitions(
            ("ping", "gather_uptime"),
            ("gather_uptime", "check_disk"),
            ("check_disk", "assess"),
            ("assess", "warn", expr(f"usage_pct >= {DISK_PRESSURE_THRESHOLD_PCT}")),
            ("assess", "ok"),
        )
        .with_tracker(LocalTrackingClient(project="philip-localhost-disk"))
        .with_state(usage_pct=-1)
        .with_entrypoint("ping")
        .build()
    )


def main() -> None:
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["ok", "warn"])
    print(f"Final action: {last_action}")
    print(f"Status:       {final_state['status']}")
    print(f"Usage:        {final_state['usage_pct']}%")
    print(f"Uptime:       {(final_state['uptime_stdout'] or '').strip()}")


if __name__ == "__main__":
    main()
