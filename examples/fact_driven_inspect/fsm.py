"""Fact-driven inspection FSM. Same graph, distro-correct execution.

Treats Ansible's ``gather_facts`` as Burr State expansion. The FSM
has no hard-coded Debian assumptions; it asks the target host what
it is, then branches the rest of the work accordingly.

Topology::

    gather_facts -> branch on ansible_pkg_mgr -+-> inspect_apt    -+
                                               +-> inspect_dnf    -+--> summarize -> done
                                               +-> inspect_pacman -+
                                               +-> escalate_unknown_pkg_mgr

Each inspect action runs the distro-appropriate package-listing command and
uses ``register="pkg_inspect"`` to capture the entire module result
(stdout, rc, cmd, delta). ``summarize`` is a plain Python ``@action`` that
reads both the gathered facts and the registered result to produce a final
report.

Library features exercised:
  - ``Host.gather_facts()``: state-expansion of ansible_facts into top-level keys
  - ``Host.initial_facts()``: seed placeholders so reads resolve pre-gather
  - ``register="..."`` on a Host module: capture the whole result dict
  - Branching transitions on fact values via ``expr("ansible_pkg_mgr == 'apt'")``

Run::

    cd ../service_remediation && ./start.sh
    uv run python examples/fact_driven_inspect/fsm.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import host, initial_sentinels

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE.parent / "service_remediation" / ".demo_key"

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


@target.shell(register="pkg_inspect")
def inspect_apt(state: State) -> dict[str, Any]:
    """Debian/Ubuntu: ``dpkg -l`` lists installed packages."""
    return {"cmd": "dpkg -l | tail -n +6 | wc -l && dpkg -l | tail -5"}


@target.shell(register="pkg_inspect")
def inspect_dnf(state: State) -> dict[str, Any]:
    """RHEL/Fedora: ``rpm -qa`` lists installed packages."""
    return {"cmd": "rpm -qa | wc -l && rpm -qa | tail -5"}


@target.shell(register="pkg_inspect")
def inspect_pacman(state: State) -> dict[str, Any]:
    """Arch: ``pacman -Q`` lists installed packages."""
    return {"cmd": "pacman -Q | wc -l && pacman -Q | tail -5"}


@action(
    reads=[
        "ansible_distribution",
        "ansible_distribution_version",
        "ansible_pkg_mgr",
        "ansible_service_mgr",
        "ansible_kernel",
        "ansible_memtotal_mb",
        "ansible_processor_count",
        "pkg_inspect",
    ],
    writes=["outcome"],
)
def summarize(state: State) -> State:
    inspect = state["pkg_inspect"] or {}
    stdout = (inspect.get("stdout") or "").splitlines()
    pkg_count = stdout[0].strip() if stdout else "?"
    sample = "; ".join(stdout[1:6]) if len(stdout) > 1 else "(no sample)"
    return state.update(
        outcome=(
            f"{state['ansible_distribution']} {state['ansible_distribution_version']} "
            f"on {state['ansible_kernel']}, "
            f"pkg_mgr={state['ansible_pkg_mgr']}, service_mgr={state['ansible_service_mgr']}, "
            f"{state['ansible_processor_count']} CPU, {state['ansible_memtotal_mb']} MB RAM. "
            f"{pkg_count} packages installed. Sample: {sample}"
        )
    )


@action(reads=["ansible_pkg_mgr", "ansible_distribution"], writes=["outcome"])
def escalate_unknown_pkg_mgr(state: State) -> State:
    return state.update(
        outcome=(
            f"ESCALATE: unsupported pkg_mgr='{state['ansible_pkg_mgr']}' "
            f"on {state['ansible_distribution']}; no inspection module defined"
        )
    )


@action(reads=["outcome"], writes=[])
def done(state: State) -> State:
    return state


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            gather_facts=target.gather_facts(),
            inspect_apt=inspect_apt,
            inspect_dnf=inspect_dnf,
            inspect_pacman=inspect_pacman,
            summarize=summarize,
            escalate_unknown_pkg_mgr=escalate_unknown_pkg_mgr,
            done=done,
        )
        .with_transitions(
            # gather_facts populates ansible_* keys; the next set of edges
            # branches on those keys to dispatch to the distro-correct module.
            # No hard-coded distro assumption anywhere in the graph.
            ("gather_facts", "inspect_apt", expr("ansible_pkg_mgr == 'apt'")),
            ("gather_facts", "inspect_dnf", expr("ansible_pkg_mgr == 'dnf'")),
            ("gather_facts", "inspect_dnf", expr("ansible_pkg_mgr == 'yum'")),
            ("gather_facts", "inspect_pacman", expr("ansible_pkg_mgr == 'pacman'")),
            ("gather_facts", "escalate_unknown_pkg_mgr"),
            ("inspect_apt", "summarize"),
            ("inspect_dnf", "summarize"),
            ("inspect_pacman", "summarize"),
            ("summarize", "done"),
            ("escalate_unknown_pkg_mgr", "done"),
        )
        .with_tracker(LocalTrackingClient(project="philip-fact-driven-inspect"))
        .with_state(
            **initial_sentinels(),
            **target.initial_facts(),
            pkg_inspect={},
            outcome="",
        )
        .with_entrypoint("gather_facts")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY}. Run ../service_remediation/setup.sh && ./start.sh first."
        )
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["done"])
    print(f"Final action:  {last_action}")
    print(f"Outcome:       {final_state['outcome']}")


if __name__ == "__main__":
    main()
