"""``wait_until()``: polling sub-graph builder.

Ansible's ``wait_for`` (and ``wait_for_connection``, ``pause``) differ from
one-shot modules like ``copy`` or ``service``: they block until a condition
holds, with a timeout. Wrapping such a module in a single ``@module_action``
collapses the polling loop into one opaque Burr step, which loses the
per-iteration visibility a tracker provides.

``wait_until()`` produces a polling loop as native FSM structure: a check
action and an increment_wait action, plus transitions that branch on the
condition each iteration. Every poll attempt is a discrete tracker step.
Timeouts route to a caller-supplied terminal action; termination lives in
FSM transitions rather than inside the module.

Usage::

    @target.shell(register="port_check")
    def check_port(state):
        return {"cmd": "nc -z 127.0.0.1 80; echo $?"}

    poll = philip.wait_until(
        name="wait_for_listener",
        check=check_port,
        condition_expr="port_check['rc'] == 0",
        max_attempts=10,
        interval_s=1.0,
        on_success="verify",
        on_timeout="escalate",
    )

    app = (
        ApplicationBuilder()
        .with_actions(reload_nginx=reload_nginx, verify=verify, escalate=escalate,
                      **poll.actions)
        .with_transitions(
            ("reload_nginx", poll.entry),
            *poll.transitions,
            ...
        )
        .with_state(..., **poll.initial_state)
        ...
    )
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from burr.core import State, action, expr


@dataclass
class WaitGraph:
    """Built by :func:`wait_until`. The fields are the components a caller
    merges into their ApplicationBuilder.

    - ``actions``: action_name -> action object. Merge via ``with_actions(**poll.actions)``.
    - ``transitions``: list of tuples for ``with_transitions(*poll.transitions)``.
    - ``entry``: the name of the first action in the sub-graph; the caller
      transitions INTO this from the action preceding the wait.
    - ``initial_state``: seeds needed in ``with_state(...)`` so the attempt
      counter starts at 0.
    """

    actions: dict[str, Any]
    transitions: list[tuple]
    entry: str
    initial_state: dict[str, Any]


def wait_until(
    *,
    name: str,
    check: Any,
    condition_expr: str,
    max_attempts: int = 10,
    interval_s: float = 1.0,
    on_success: str,
    on_timeout: str,
) -> WaitGraph:
    """Build a polling-until-condition sub-graph.

    Topology::

        <name>_check
          ├── (condition_expr)               → on_success
          ├── (<name>_attempts >= max-1)     → on_timeout
          └── <name>_wait (sleep + ++count)  → <name>_check

    The ``check`` argument is a ``@module_action`` (or any Burr ``@action``)
    that the caller has already built. Typically a shell, uri, or wait_for
    call that writes a result the ``condition_expr`` references.
    ``condition_expr`` is a Python expression evaluated against state via
    Burr's standard ``expr`` builder.

    On timeout the FSM routes to ``on_timeout``; on success to ``on_success``.
    These are action names the caller registers separately. The wait sub-graph
    has no escalation logic of its own; escalation is a graph-level concern.
    """
    check_name = f"{name}_check"
    wait_name = f"{name}_wait"
    attempts_key = f"{name}_attempts"

    @action(reads=[attempts_key], writes=[attempts_key])
    def _increment_wait(state: State) -> State:
        """Sleep ``interval_s`` and bump the attempt counter, then re-enter check."""
        time.sleep(interval_s)
        return state.update(**{attempts_key: state[attempts_key] + 1})

    actions: dict[str, Any] = {
        check_name: check,
        wait_name: _increment_wait,
    }

    # Transitions are tried in declaration order; first matching predicate wins.
    # Order: success path first (exit as soon as the condition holds), then
    # timeout (bail if attempts is at the cap), then the default retry edge
    # into wait. The check action runs on entry; transitions downstream branch
    # on the condition and attempt count.
    transitions: list[tuple] = [
        (check_name, on_success, expr(condition_expr)),
        (check_name, on_timeout, expr(f"{attempts_key} >= {max_attempts - 1}")),
        (check_name, wait_name),
        (wait_name, check_name),
    ]

    initial_state = {attempts_key: 0}

    return WaitGraph(
        actions=actions,
        transitions=transitions,
        entry=check_name,
        initial_state=initial_state,
    )
