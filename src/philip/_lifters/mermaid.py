"""Lift Mermaid stateDiagram-v2 source into a Burr ``Application``.

The supported subset covers the constructs that appear in real README
diagrams. Every line outside this subset is reported via
:class:`MermaidLiftError` (when the construct is structurally ambiguous)
or silently ignored (when it carries presentation information only).

Supported::

    stateDiagram-v2
        [*] --> Idle
        Idle --> Active : start
        Active --> Idle : stop
        Active --> Done
        Done --> [*]

* The ``stateDiagram-v2`` directive opens the block. ``stateDiagram`` (no
  v2 suffix) is accepted as a synonym.
* ``[*]`` is the initial / terminal pseudo-state. Edges out of ``[*]``
  define the entrypoint; edges into ``[*]`` mark terminal states. Burr
  has no formal pseudo-state, so terminals become explicit ``done``-style
  states (one per inbound ``[*]`` edge if multiple, or a shared ``done``
  if there is only one).
* ``A --> B`` is an unconditional transition.
* ``A --> B : label`` carries a transition label. If the label looks like
  a Python expression (contains ``==``, ``<``, ``>``, ``and``, ``or``,
  ``not in``, etc.), it lowers to :func:`burr.core.expr` as the
  transition guard. Otherwise it is recorded as descriptive metadata in
  state under ``_transition_labels`` and the transition fires
  unconditionally.
* Lines beginning with ``%%``, ``classDef``, ``class ``, ``note``,
  ``direction``, ``link``, or ``state`` (composite block opener) are
  ignored. Composite states are NOT inlined in v1; if the diagram has a
  composite block the lift refuses with a clear error.

Each state becomes a Burr ``@action`` that does nothing but advance the
state machine. The action body writes the current state name into
``_current_state`` so a downstream consumer (an LLM driving via MCP, a
test) can observe the path taken without having to introspect Burr's
graph.

Example::

    from philip import from_mermaid

    text = '''
    stateDiagram-v2
        [*] --> Acknowledge
        Acknowledge --> Investigate : on_alert
        Investigate --> Mitigate
        Investigate --> Escalate : severity == "critical"
        Mitigate --> Verify
        Verify --> Done
        Escalate --> Done
        Done --> [*]
    '''

    app = from_mermaid(text)
    last, _, state = app.run(halt_after=["done"])
"""

from __future__ import annotations

import re
from pathlib import Path

from burr.core import Application, ApplicationBuilder, State, action, expr

# ── Public API ────────────────────────────────────────────────────────────


class MermaidLiftError(ValueError):
    """Raised when a Mermaid source cannot be lifted into a Burr graph.

    Carries the offending line text plus the 1-based line number in the
    original source so a generated FSM doesn't silently misrepresent the
    diagram.
    """

    def __init__(self, message: str, *, line: str = "", line_number: int = 0) -> None:
        full = (
            f"line {line_number}: {message}\n  >>> {line.strip()}"
            if line_number
            else message
        )
        super().__init__(full)
        self.line = line
        self.line_number = line_number


def from_mermaid_text(source: str) -> Application:
    """Lift a Mermaid stateDiagram-v2 source string into a Burr Application."""
    edges, terminals = _parse(source)
    if not edges:
        raise MermaidLiftError("no transitions parsed from diagram")
    return _build_application(edges, terminals)


def from_mermaid(path: str | Path) -> Application:
    """Lift a ``.mmd``, ``.mermaid``, or ``.md`` file containing a state diagram."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    return from_mermaid_text(text)


# ── Parser ────────────────────────────────────────────────────────────────


_DIRECTIVE = re.compile(r"^\s*stateDiagram(?:-v2)?\b")
_TRANSITION = re.compile(
    r"""
    ^\s*
    (?P<src>\[\*\]|[A-Za-z_]\w*)
    \s*-->\s*
    (?P<dst>\[\*\]|[A-Za-z_]\w*)
    (?:\s*:\s*(?P<label>.+?))?
    \s*$
    """,
    re.VERBOSE,
)
# Constructs we intentionally skip (presentation, comments) vs reject
# (composite states require lowering we don't do in v1).
_SKIP_PREFIXES = (
    "%%",  # comment
    "classDef ",
    "class ",
    "note ",
    "direction ",
    "link ",
    "click ",
    "accTitle:",
    "accDescr:",
)
_COMPOSITE_PREFIX = re.compile(r"^\s*state\s+\w+\s*\{")
# A label looks like a Python predicate if it contains comparison ops,
# logical ops, or membership tests. Anything else is treated as
# descriptive metadata (e.g. event names like ``on_alert``).
_PREDICATE_HINTS = re.compile(r"==|!=|<=|>=|<|>|\band\b|\bor\b|\bnot\b|\bin\b|\bis\b")


def _parse(source: str) -> tuple[list[tuple[str, str, str | None]], set[str]]:
    """Return (edges, terminals).

    ``edges`` is a list of ``(src, dst, label_or_None)`` with ``[*]``
    preserved where it appears. ``terminals`` is the set of state names
    that have an outgoing edge to ``[*]``.
    """
    in_block = False
    saw_directive = False
    edges: list[tuple[str, str, str | None]] = []
    terminals: set[str] = set()

    for ln, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not in_block:
            if _DIRECTIVE.match(raw):
                in_block = True
                saw_directive = True
            # Tolerate diagrams embedded in markdown fences:
            # the lines before ``stateDiagram-v2`` are ignored.
            continue
        if line.startswith(_SKIP_PREFIXES):
            continue
        if _COMPOSITE_PREFIX.search(line):
            raise MermaidLiftError(
                "composite states (state X { ... }) are not lifted in v1",
                line=raw,
                line_number=ln,
            )
        if line.startswith("state "):
            # `state X` declares a state without a transition; we don't
            # need to record it because transitions will surface it.
            continue
        m = _TRANSITION.match(line)
        if m:
            src, dst = m.group("src"), m.group("dst")
            label = m.group("label")
            if label is not None:
                label = label.strip()
                # Mermaid wraps labels with unicode in double quotes to escape
                # them. Strip the wrapper only when the entire label is quoted,
                # to avoid mangling Python expressions like
                # ``severity == "critical"``.
                if (
                    len(label) >= 2
                    and label[0] == '"'
                    and label[-1] == '"'
                    and label[1:-1].count('"') == 0
                ):
                    label = label[1:-1]
            if dst == "[*]":
                terminals.add(src)
            edges.append((src, dst, label))
            continue
        # Anything else inside the block is structurally ambiguous and
        # gets reported rather than silently dropped.
        raise MermaidLiftError(
            "unrecognized diagram line; expected `A --> B` or `A --> B : label`",
            line=raw,
            line_number=ln,
        )

    if not saw_directive:
        raise MermaidLiftError("missing `stateDiagram` or `stateDiagram-v2` directive")
    return edges, terminals


# ── Builder ───────────────────────────────────────────────────────────────


def _safe_action_name(label: str) -> str:
    """Map a Mermaid state name to a safe Burr action identifier."""
    return re.sub(r"\W", "_", label) or "_state"


def _build_application(
    edges: list[tuple[str, str, str | None]],
    terminals: set[str],
) -> Application:
    # First pass: determine the entrypoint (target of `[*] -->`).
    entry_candidates = [dst for src, dst, _ in edges if src == "[*]"]
    if not entry_candidates:
        raise MermaidLiftError("no entrypoint: expected at least one `[*] --> X` transition")
    if len(set(entry_candidates)) > 1:
        raise MermaidLiftError(
            f"multiple entrypoints: {sorted(set(entry_candidates))}; "
            "Burr requires a single entrypoint"
        )
    entry = entry_candidates[0]

    # Real (non-pseudo) state names.
    state_names: list[str] = []
    seen: set[str] = set()
    for src, dst, _ in edges:
        for name in (src, dst):
            if name == "[*]":
                continue
            if name not in seen:
                seen.add(name)
                state_names.append(name)

    # Synthesize a single ``done`` terminal and route every `X --> [*]`
    # through it. This keeps the Burr graph closed without forcing the
    # caller to add a halt action.
    if terminals and "done" not in seen:
        state_names.append("done")
        seen.add("done")

    # Build the action callables. Each is a pure passthrough that records
    # the action name in state under ``_current_state`` and increments a
    # counter so the caller can detect loops.
    actions = [_make_passthrough_action(_safe_action_name(name), name) for name in state_names]

    # Tally fan-out per source state so we know which sources have
    # multiple labeled outbound edges and need labels lifted to guards.
    # Skip the synthetic `[*]` source.
    runtime_edges = [(s, d, label) for s, d, label in edges if s != "[*]"]
    fan_out: dict[str, int] = {}
    for src, _, _ in runtime_edges:
        fan_out[src] = fan_out.get(src, 0) + 1

    # Construct transitions.
    burr_transitions: list[tuple[str, str] | tuple[str, str, object]] = []
    for src, dst, label in runtime_edges:
        target = "done" if dst == "[*]" else _safe_action_name(dst)
        source = _safe_action_name(src)
        if label is None:
            burr_transitions.append((source, target))
            continue
        if _looks_like_predicate(label):
            burr_transitions.append((source, target, expr(label)))
            continue
        # Descriptive token. If the source has more than one outbound edge,
        # the diagram is encoding a choice between cases; lift the token to
        # a ``_choice == "<label>"`` guard so Burr can route. If the source
        # has exactly one outbound edge, the label is documentation only
        # and the transition is the unconditional default.
        if fan_out[src] > 1:
            safe_label = label.replace('"', '\\"')
            burr_transitions.append((source, target, expr(f'_choice == "{safe_label}"')))
        else:
            burr_transitions.append((source, target))

    builder = (
        ApplicationBuilder()
        .with_actions(*actions)
        .with_transitions(*burr_transitions)
        .with_state(_current_state="", _step_count=0, _choice="")
        .with_entrypoint(_safe_action_name(entry))
    )
    return builder.build()


def _looks_like_predicate(label: str) -> bool:
    return bool(_PREDICATE_HINTS.search(label))


def _make_passthrough_action(safe_name: str, display_name: str):
    @action(reads=["_step_count"], writes=["_current_state", "_step_count"])
    def passthrough(state: State) -> State:
        return state.update(
            _current_state=display_name,
            _step_count=state["_step_count"] + 1,
        )

    passthrough.__name__ = safe_name
    return passthrough
