"""Project a :class:`philip.PhilipGraph` into a Burr ``Application``.

The IR carries an optional ``entrypoint`` and ``terminals``; the
projector uses them when present and falls back to sensible defaults
otherwise (first declared node as entrypoint, no synthesized ``done``
when there are no terminals).

The branching rules mirror :mod:`philip._lifters.mermaid`: a source with
fan-out > 1 produces all guarded transitions, with the guard either an
:func:`burr.core.expr`-shaped Python predicate (when the label looks
like one) or ``_choice == "<label-or-destination>"`` otherwise. A
source with exactly one outbound edge produces an unconditional default
transition.
"""

from __future__ import annotations

import re

from burr.core import Application, ApplicationBuilder, State, action, expr

from philip._ir import PhilipGraph

# A label is treated as a Python predicate when it contains comparison
# or logical operators. Anything else is a descriptive token that lifts
# to a ``_choice == "..."`` guard on branching sources.
_PREDICATE_HINTS = re.compile(r"==|!=|<=|>=|<|>|\band\b|\bor\b|\bnot\b|\bin\b|\bis\b")


def _safe_action_name(name: str) -> str:
    return re.sub(r"\W", "_", name) or "_state"


def _make_passthrough(safe_name: str, display_name: str):
    @action(reads=["_step_count"], writes=["_current_state", "_step_count"])
    def passthrough(state: State) -> State:
        return state.update(
            _current_state=display_name,
            _step_count=state["_step_count"] + 1,
        )

    passthrough.__name__ = safe_name
    return passthrough


def project_to_burr(graph: PhilipGraph) -> Application:
    if not graph.nodes:
        raise ValueError("cannot project an empty graph to Burr")

    state_names = [n.id for n in graph.nodes]
    if graph.terminals and "done" not in state_names:
        state_names.append("done")

    actions = [_make_passthrough(_safe_action_name(name), name) for name in state_names]

    fan_out = graph.fan_out()

    burr_transitions: list[tuple[str, str] | tuple[str, str, object]] = []
    for e in graph.edges:
        source = _safe_action_name(e.src)
        target = _safe_action_name(e.dst)
        if fan_out.get(e.src, 0) <= 1:
            burr_transitions.append((source, target))
            continue
        if e.label and _PREDICATE_HINTS.search(e.label):
            burr_transitions.append((source, target, expr(e.label)))
            continue
        choice_value = e.label or e.dst
        safe = choice_value.replace('"', '\\"')
        burr_transitions.append((source, target, expr(f'_choice == "{safe}"')))

    # Route every terminal node to the synthesized ``done`` action.
    for t in graph.terminals:
        source = _safe_action_name(t)
        if fan_out.get(t, 0) <= 1:
            burr_transitions.append((source, "done"))
        else:
            burr_transitions.append((source, "done", expr('_choice == "done"')))

    entry = graph.entrypoint or graph.nodes[0].id

    return (
        ApplicationBuilder()
        .with_actions(*actions)
        .with_transitions(*burr_transitions)
        .with_state(_current_state="", _step_count=0, _choice="")
        .with_entrypoint(_safe_action_name(entry))
        .build()
    )
