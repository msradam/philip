"""Intermediate representation for the dual-substrate lift family.

Every format-specific parser produces a :class:`PhilipGraph`. The graph
is then projected to one of two substrates via the methods on the
graph itself:

* :meth:`PhilipGraph.to_burr` builds a runnable Burr Application. FSM
  semantics (entrypoint, terminals, branching guards) come from optional
  metadata the parser populates; the projector is happy without them.
* :meth:`PhilipGraph.to_hamilton` builds a Hamilton-compatible module.
  Requires the graph to be acyclic; otherwise raises ``ValueError``.

The motivation for routing through an IR rather than building Burr or
Hamilton directly from each parser is composition. A Mermaid flowchart
and an Excalidraw sketch describe the same shape of artifact (a typed
directed graph); they should share their projection logic. New formats
parse to PhilipGraph and get both substrate projections for free.

The existing lifters in :mod:`philip._convert` (Ansible),
:mod:`philip._lifters.mermaid` (stateDiagram), :mod:`philip._lifters.mermaid_flow`
(flowchart), and :mod:`philip._lifters.sql_cte` (SQL CTEs) currently
build Burr or Hamilton directly. They will be migrated to the IR pattern
in a subsequent cleanup pass; the public API stays the same either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    from burr.core import Application


@dataclass(frozen=True)
class Node:
    """A node in the IR graph.

    ``id`` must be a valid Python identifier (it becomes the action or
    Hamilton function name). ``label`` carries the human-readable text
    from the source diagram. ``shape`` is a format-specific hint
    (``"rectangle"``, ``"diamond"``, ``"ellipse"``) preserved for any
    downstream tooling that cares; the projectors ignore it.
    """

    id: str
    label: str = ""
    shape: str = ""


@dataclass(frozen=True)
class Edge:
    """A directed edge in the IR graph.

    ``label`` is the transition label. Burr projection inspects it: an
    expression-shaped label (``"severity == 'critical'"``) lifts to a
    :func:`burr.core.expr` guard; a descriptive label (``"on_alert"``)
    lifts to ``_choice == "<label>"`` when the source state has fan-out
    greater than one. Unlabeled edges from a branching source lift to
    ``_choice == "<destination>"``. Hamilton projection ignores labels.
    """

    src: str
    dst: str
    label: str = ""


@dataclass(frozen=True)
class GraphNode:
    """Sentinel value returned by Hamilton functions generated from the IR.

    Downstream callers walk ``.depends_on`` for lineage or override
    individual nodes via the Driver's ``inputs`` to replace the sentinel
    with real computation.
    """

    id: str
    label: str
    depends_on: tuple[str, ...] = ()
    shape: str = ""


@dataclass(frozen=True)
class PhilipGraph:
    """The IR: a typed directed graph with optional FSM metadata.

    ``nodes`` and ``edges`` are required; everything else is optional and
    used only when the graph is projected to Burr.
    """

    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    entrypoint: str | None = None
    terminals: frozenset[str] = field(default_factory=frozenset)
    source_format: str = ""

    # ── Convenience views ──────────────────────────────────────────────────

    def node_ids(self) -> frozenset[str]:
        return frozenset(n.id for n in self.nodes)

    def fan_out(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.edges:
            out[e.src] = out.get(e.src, 0) + 1
        # Terminals add one synthetic outbound to ``done``.
        for t in self.terminals:
            out[t] = out.get(t, 0) + 1
        return out

    def is_acyclic(self) -> bool:
        """Iterative Kahn's algorithm; True iff the graph has no cycles."""
        in_degree = dict.fromkeys(self.node_ids(), 0)
        for e in self.edges:
            if e.dst in in_degree:
                in_degree[e.dst] += 1
        queue = [n for n, d in in_degree.items() if d == 0]
        successors: dict[str, list[str]] = {n: [] for n in self.node_ids()}
        for e in self.edges:
            if e.src in successors and e.dst in successors:
                successors[e.src].append(e.dst)
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for nxt in successors[node]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        return visited == len(in_degree)

    # ── Projections ────────────────────────────────────────────────────────

    def to_burr(self) -> Application:
        from philip._projectors.burr_proj import project_to_burr

        return project_to_burr(self)

    def to_hamilton(self, *, module_name: str = "philip_ir_dag") -> ModuleType:
        from philip._projectors.hamilton_proj import project_to_hamilton

        return project_to_hamilton(self, module_name=module_name)
