"""Walk the mast_sre_agent FSM step by step with colored output.

The mast_sre_agent demo is the agent-side counterpart to the playbook
conversion walker. An LLM picks a remediation label from a fixed
allow-list (out_of_memory, disk_full, ...) given a parsed log summary.
A validator action gates off-script picks. The FSM owns the rest: the
chosen branch's remediation chain runs as Ansible modules, an external
verification step proves the work landed, and only then does the FSM
declare done.

This walker prints one line per action with name, duration, status,
and a small per-action state summary chosen so the LLM pick, the
validator decision, and the verify outcome land in the trace.

Run::

    OLLAMA_MODEL=ibm/granite4:micro uv run python examples/mast_sre_agent_walker.py

Requires the demo container from ``examples/service_remediation/`` and
an Ollama daemon serving the model named in ``OLLAMA_MODEL``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# The example package is not installable; import sibling-by-path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mast_sre_agent.fsm import build_application

# ---- ANSI ---------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

_TERMINALS = frozenset({"done", "escalate"})


def _truncate(value: object, width: int = 60) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "..."


def _summarize_state(action_name: str, state: dict[str, object]) -> list[str]:
    """Return per-action state lines worth showing in the walker.

    The mast graph has 12+ actions; printing every state delta would
    overflow the GIF. So we pick the field most relevant to each action:
    LLM raw output after classify, validator note after validate, http
    status after verify, etc.
    """
    out: list[str] = []
    if action_name == "classify_with_llm":
        raw = state.get("classification_raw")
        out.append(f"LLM raw: {_truncate(raw)}")
    elif action_name == "validate_classification":
        note = state.get("validation_note")
        kind = state.get("classification")
        out.append(f"label: {kind!r}  ({_truncate(note)})")
    elif action_name == "check_for_loop":
        decision = state.get("loop_decision")
        out.append(f"loop check: {decision}")
    elif action_name == "record_attempt":
        history = state.get("remediation_history") or []
        out.append(f"history: {history}")
    elif action_name.startswith(("oom_", "disk_")):
        last_msg = state.get("_last_msg") or ""
        if last_msg:
            out.append(f"msg: {_truncate(last_msg, 56)}")
    elif action_name == "external_verify":
        status = state.get("http_status")
        out.append(f"HTTP {status}")
    return out


def _print_header() -> None:
    print()
    print(f"  {BOLD}{CYAN}philip{RESET}{DIM} . {RESET}{BOLD}MAST-style SRE agent{RESET}")
    print(
        f"  {DIM}LLM picks one label from a fixed allow-list. "
        f"FSM owns termination, validation, and verify-before-done.{RESET}"
    )
    print()


def _print_step(step: int, name: str, ms: float, failed: bool, lines: list[str]) -> None:
    mark = f"{RED}x{RESET}" if failed else f"{GREEN}o{RESET}"
    color = RED if failed else CYAN
    # Mark the LLM-touching steps in a different color so they stand out
    # against the deterministic ones around them.
    is_llm = name in ("classify_with_llm", "validate_classification")
    color = MAGENTA if is_llm else color
    print(
        f"  {DIM}[{step:2d}]{RESET} {BOLD}{color}{name:<28}{RESET}"
        f"  {DIM}{ms:>5.0f}ms{RESET}  {mark}"
    )
    for entry in lines:
        print(f"       {DIM}{entry}{RESET}")


def main() -> None:
    _print_header()
    app = build_application()
    step = 0
    while True:
        step += 1
        t0 = time.perf_counter()
        outcome = app.step()
        ms = (time.perf_counter() - t0) * 1000.0
        if outcome is None:
            print(f"  {DIM}no further reachable actions; halting.{RESET}")
            return
        action_obj, _, state = outcome
        curr = dict(state.get_all())
        failed = bool(curr.get("_last_failed"))
        lines = _summarize_state(action_obj.name, curr)
        _print_step(step, action_obj.name, ms, failed, lines)
        if action_obj.name in _TERMINALS:
            color = GREEN if action_obj.name == "done" else YELLOW
            label = "OK" if action_obj.name == "done" else "ESCALATE"
            print()
            print(f"  {color}{BOLD}-> {label}{RESET}  {_truncate(curr.get('outcome', ''))}")
            print()
            return


if __name__ == "__main__":
    main()
