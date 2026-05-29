"""Hero demo: deploy something, poll until it's ready, read, clean up.

Six declared actions plus a ``wait_until`` polling sub-graph. A background
subprocess simulates a slow service by touching a marker file after a
two-second delay. The FSM polls for the marker on a fixed interval, reads
the file once it appears, and removes the state directory at the end.

Each poll attempt is a discrete Burr step in the trace rather than one
opaque blocking task.

Run::

    uv run python examples/hero.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action

from philip import initial_sentinels, module_action, wait_until

_HERE = Path(__file__).resolve().parent
_STATE_DIR = _HERE / ".hero-state"
_READY_FILE = _STATE_DIR / "service-ready"
_PAYLOAD = "service ready at {ts}\n"

# ---- ANSI ---------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

_HIDDEN_STATE_KEYS = frozenset(
    {
        "_last_action",
        "_last_failed",
        "_last_changed",
        "_last_unreachable",
        "_last_msg",
        "_last_diff",
        "__SEQUENCE_ID",
        "__PRIOR_STEP",
    }
)


# ---- FSM actions --------------------------------------------------------


@module_action("ansible.builtin.file")
def setup_state_dir(state: State) -> dict[str, Any]:
    return {"path": str(_STATE_DIR), "state": "directory", "mode": "0755"}


@module_action("ansible.builtin.copy")
def deploy_service(state: State) -> dict[str, Any]:
    """Write a placeholder log. Real services would start a daemon here;
    this demo simulates the service externally via a background sleep."""
    return {
        "content": "service deploying...\n",
        "dest": str(_STATE_DIR / "service.log"),
        "mode": "0644",
    }


@module_action(
    "ansible.builtin.find",
    register="probe",
)
def probe_ready(state: State) -> dict[str, Any]:
    """One poll attempt: does the ready marker exist yet?

    ``ansible.builtin.find`` returns ``matched: 0`` when no files match and
    a positive integer otherwise, so the wait_until condition expression
    branches on ``probe['matched'] > 0``."""
    return {"paths": str(_STATE_DIR), "patterns": ["service-ready"]}


_wait = wait_until(
    name="wait_for_ready",
    check=probe_ready,
    condition_expr="probe.get('matched', 0) > 0",
    max_attempts=20,
    interval_s=0.4,
    on_success="read_state",
    on_timeout="escalate",
)


@module_action("ansible.builtin.slurp", register="ready_content")
def read_state(state: State) -> dict[str, Any]:
    return {"src": str(_READY_FILE)}


@module_action("ansible.builtin.file")
def cleanup(state: State) -> dict[str, Any]:
    return {"path": str(_STATE_DIR), "state": "absent"}


@action(reads=["ready_content"], writes=["report"])
def done(state: State) -> State:
    import base64

    raw = base64.b64decode(state["ready_content"].get("content", "") or "").decode(
        "utf-8", errors="replace"
    )
    return state.update(report=f"service reported: {raw.strip()}")


@action(reads=["_last_action", "_last_msg"], writes=["report"])
def escalate(state: State) -> State:
    return state.update(report=f"ESCALATE at {state['_last_action']}: {state['_last_msg'][:200]}")


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            setup_state_dir=setup_state_dir,
            deploy_service=deploy_service,
            read_state=read_state,
            cleanup=cleanup,
            done=done,
            escalate=escalate,
            **_wait.actions,
        )
        .with_transitions(
            ("setup_state_dir", "deploy_service"),
            ("deploy_service", _wait.entry),
            *_wait.transitions,
            ("read_state", "cleanup"),
            ("cleanup", "done"),
        )
        .with_state(
            **initial_sentinels(),
            **_wait.initial_state,
            probe={},
            ready_content={},
            report="",
        )
        .with_entrypoint("setup_state_dir")
        .build()
    )


# ---- Pretty walker ------------------------------------------------------


def _truncate(value: object, width: int = 56) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _state_delta(prev: dict[str, object], curr: dict[str, object]) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for key, value in curr.items():
        if key in _HIDDEN_STATE_KEYS:
            continue
        if key not in prev or prev[key] != value:
            out.append((key, value))
    return out


def _print_header() -> None:
    print()
    print(f"  {BOLD}{CYAN}philip{RESET}{DIM} · {RESET}{BOLD}deploy and wait for ready{RESET}")
    print(f"  {DIM}Ansible modules for I/O. wait_until polling sub-graph for the gate.{RESET}")
    print()


def _print_step(
    step: int,
    name: str,
    duration_ms: float,
    delta: list[tuple[str, object]],
    failed: bool,
) -> None:
    mark = f"{RED}✗{RESET}" if failed else f"{GREEN}✓{RESET}"
    name_color = RED if failed else CYAN
    print(
        f"  {DIM}[{step:2d}]{RESET} {BOLD}{name_color}{name:<24}{RESET}"
        f"  {DIM}{duration_ms:>5.0f}ms{RESET}  {mark}"
    )
    for key, value in delta:
        # Truncate dict-shaped values to the keys that matter most for the demo.
        if key == "probe" and isinstance(value, dict):
            matched = value.get("matched", "?")
            print(f"       {DIM}{key}.matched={RESET}{matched}")
            continue
        if key == "ready_content" and isinstance(value, dict):
            import base64

            raw = base64.b64decode(value.get("content", "") or "").decode("utf-8", errors="replace")
            print(f"       {DIM}{key}={RESET}{_truncate(raw)}")
            continue
        print(f"       {DIM}{key}={RESET}{_truncate(value)}")
    print()


def _spawn_slow_service(delay_s: float) -> subprocess.Popen[bytes]:
    """Touch ``_READY_FILE`` after ``delay_s`` seconds. Detached from the
    foreground process so the FSM polling sees it appear mid-run."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            "sh",
            "-c",
            (f"sleep {delay_s}; printf '{_PAYLOAD.format(ts=int(time.time()))}' > {_READY_FILE}"),
        ]
    )


def main() -> None:
    # Reset any leftover state from a prior run.
    if _STATE_DIR.exists():
        shutil.rmtree(_STATE_DIR)

    _print_header()

    service = _spawn_slow_service(delay_s=2.0)
    try:
        app = build_application()
        prev_state = dict(app.state.get_all())
        step_no = 0
        terminal_names = {"done", "escalate"}

        while True:
            step_no += 1
            t0 = time.perf_counter()
            outcome = app.step()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            if outcome is None:
                print(f"  {DIM}no further reachable actions; halting.{RESET}")
                break

            action_obj, _result, state = outcome
            state_now = dict(state.get_all())
            delta = _state_delta(prev_state, state_now)
            prev_state = state_now

            failed = bool(state_now.get("_last_failed", False))
            _print_step(step_no, action_obj.name, elapsed_ms, delta, failed)

            if action_obj.name in terminal_names:
                color = GREEN if action_obj.name == "done" else YELLOW
                label = "OK" if action_obj.name == "done" else "ESCALATE"
                print(f"  {color}{BOLD}→ {label}{RESET}  {_truncate(state_now['report'])}")
                print()
                break
    finally:
        if service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=2)
            except subprocess.TimeoutExpired:
                service.kill()
        if _STATE_DIR.exists():
            shutil.rmtree(_STATE_DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
