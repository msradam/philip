"""Project a :class:`philip.PhilipGraph` into a Hamilton-compatible module.

Each IR node becomes a Hamilton function whose parameter names declare
its upstream dependencies. Function bodies return a :class:`GraphNode`
sentinel carrying the id, label, dependency list, and shape hint.

Requires the ``hamilton`` extra. Raises :class:`ImportError` with a
clear message if Hamilton is not installed. Raises ``ValueError`` if
the graph contains a cycle.
"""

from __future__ import annotations

import keyword
import re
from types import ModuleType

from philip._ir import PhilipGraph

_IDENT = re.compile(r"^[A-Za-z_]\w*$")


def project_to_hamilton(graph: PhilipGraph, *, module_name: str = "philip_ir_dag") -> ModuleType:
    try:
        from hamilton.ad_hoc_utils import create_module
    except ImportError as e:
        raise ImportError(
            "philip.to_hamilton requires the 'hamilton' extra; "
            "install with: pip install 'philip-machine[hamilton]'"
        ) from e

    if not graph.nodes:
        raise ValueError("cannot project an empty graph to Hamilton")

    for n in graph.nodes:
        if not _IDENT.match(n.id) or keyword.iskeyword(n.id):
            raise ValueError(
                f"node id {n.id!r} is not a valid Python identifier; "
                "rename before projecting to Hamilton"
            )

    if not graph.is_acyclic():
        raise ValueError("graph has cycles; Hamilton requires a DAG")

    # Build the depends_on list per node from the edges, preserving the
    # source declaration order for stable function signatures.
    deps_by_id: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    seen: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
    for e in graph.edges:
        if e.dst not in deps_by_id:
            continue
        if e.src in seen[e.dst]:
            continue
        deps_by_id[e.dst].append(e.src)
        seen[e.dst].add(e.src)

    lines = ["from philip._ir import GraphNode", ""]
    for n in graph.nodes:
        deps = deps_by_id[n.id]
        params = ", ".join(f"{d}: GraphNode" for d in deps)
        lines.extend(
            [
                f"def {n.id}({params}) -> GraphNode:",
                f"    '''Lifted IR node {n.id!r}: {n.label}.'''",
                "    return GraphNode(",
                f"        id={n.id!r},",
                f"        label={n.label!r},",
                f"        depends_on={tuple(deps)!r},",
                f"        shape={n.shape!r},",
                "    )",
                "",
            ]
        )
    source = "\n".join(lines)
    return create_module(source, module_name=module_name)
