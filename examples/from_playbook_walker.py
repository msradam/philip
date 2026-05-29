"""Walk a converted playbook FSM step by step with colored output.

Takes the multi-feature playbook in ``examples/from_playbook_advanced/``,
runs ``philip.from_playbook(...)`` to lift it into a Burr Application,
then walks the result one action at a time and prints what changed in
state. The point: each Ansible task (and each loop iteration, and each
notify marker, and each handler) is a discrete observable step in the
trace, not one opaque playbook invocation.

Run::

    uv run python examples/from_playbook_walker.py
"""

from __future__ import annotations

import time
from pathlib import Path

import philip

# ---- ANSI ---------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

_HIDDEN_PREFIXES = ("_last_", "_loop_", "_notified_", "__")
_HIDDEN_EXACT = frozenset({"workspace_dir", "marker_file", "manifest_file"})

_PLAYBOOK = (
    Path(__file__).resolve().parent / "from_playbook_advanced" / "playbook.yml"
)


def _truncate(value: object, width: int = 60) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _delta_summary(prev: dict[str, object], curr: dict[str, object]) -> list[str]:
    """Return one short string per state field that changed and is worth
    showing. Internal bookkeeping and the play vars are skipped."""
    out: list[str] = []
    for key, value in curr.items():
        if key.startswith(_HIDDEN_PREFIXES) or key in _HIDDEN_EXACT or key == "outcome":
            continue
        if key not in prev or prev[key] != value:
            out.append(f"{key}={_truncate(value, 50)}")
    return out


def _print_header() -> None:
    print()
    print(f"  {BOLD}{CYAN}philip{RESET}{DIM} · {RESET}{BOLD}playbook -> FSM{RESET}")
    print(
        f"  {DIM}Lifting {_PLAYBOOK.name} into a Burr Application; "
        f"walking step by step.{RESET}"
    )
    print()


def main() -> None:
    _print_header()
    app = philip.from_playbook(_PLAYBOOK)
    prev = dict(app.state.get_all())
    step = 0
    while True:
        step += 1
        t0 = time.perf_counter()
        outcome = app.step()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if outcome is None:
            print(f"  {DIM}no further reachable actions; halting.{RESET}")
            break

        action_obj, _result, state = outcome
        curr = dict(state.get_all())
        delta = _delta_summary(prev, curr)
        prev = curr

        failed = bool(curr.get("_last_failed"))
        mark = f"{RED}✗{RESET}" if failed else f"{GREEN}✓{RESET}"
        color = RED if failed else CYAN
        print(
            f"  {DIM}[{step:2d}]{RESET} {BOLD}{color}{action_obj.name:<38}{RESET}"
            f"  {DIM}{elapsed_ms:>5.0f}ms{RESET}  {mark}"
        )
        for entry in delta:
            print(f"       {DIM}{entry}{RESET}")

        if action_obj.name in ("done", "escalate"):
            terminal_color = GREEN if action_obj.name == "done" else YELLOW
            label = "OK" if action_obj.name == "done" else "ESCALATE"
            print()
            print(
                f"  {terminal_color}{BOLD}-> {label}{RESET}  "
                f"{_truncate(curr.get('outcome', ''))}"
            )
            print()
            break


if __name__ == "__main__":
    main()
