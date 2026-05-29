"""Lift Mermaid flowchart / graph diagrams into a Hamilton DAG.

Mermaid's ``flowchart`` (or legacy ``graph``) directive declares a
directed graph: nodes are units of work, edges declare "downstream
depends on upstream." That is structurally the same shape as a Hamilton
dataflow DAG, where a function's parameter names declare its upstream
dependencies. The lift is mechanical.

Example::

    flowchart TD
        raw[Raw Orders] --> clean[Cleaned]
        clean --> joined[Joined With Customers]
        customers --> joined
        joined --> summary[Region Summary]

That diagram lifts to a Hamilton module with one function per node.
``joined`` takes ``clean`` and ``customers`` as parameters; ``summary``
takes ``joined``. The function bodies return a :class:`MermaidNode`
sentinel carrying the node id, the human-readable label, and the
upstream dependency list. Downstream Hamilton materializers can replace
the sentinel with real computation by overriding the relevant node
with a Driver input.

Supported subset:

* The directive opens the block: ``flowchart``, ``graph``, or any of
  those followed by a direction (``TD``, ``LR``, ``RL``, ``BT``).
* Node declarations with shape suffixes (``A[label]``, ``A(label)``,
  ``A((label))``, ``A{label}``). The shape is preserved as metadata but
  doesn't affect the lift.
* Edges of the form ``A --> B``, ``A --> B[Label]``,
  ``A -- text --> B``, and chained ``A --> B --> C`` lift correctly.
* Cycles are rejected; Hamilton requires a DAG.
* Subgraphs (``subgraph X ... end``) are NOT lifted in v1; if a diagram
  uses them the lift refuses with a clear error.

Node ids must be valid Python identifiers (we restrict to identifiers
so they map cleanly to function names). Labels are free-form text.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

# ── Public types ──────────────────────────────────────────────────────────


class MermaidFlowLiftError(ValueError):
    """Raised when a Mermaid flowchart cannot be lifted into a Hamilton DAG."""

    def __init__(self, message: str, *, line: str = "", line_number: int = 0) -> None:
        full = f"line {line_number}: {message}\n  >>> {line.strip()}" if line_number else message
        super().__init__(full)
        self.line = line
        self.line_number = line_number


@dataclass(frozen=True)
class MermaidNode:
    """One materialized node in the lifted DAG.

    Hamilton sees this as the function's return value. Downstream
    callers walk ``.depends_on`` for lineage or override individual
    nodes via the Driver's ``inputs`` to replace the sentinel with a
    real value.
    """

    id: str
    label: str
    depends_on: tuple[str, ...] = ()
    shape: str = "rect"


# ── Public entry point ────────────────────────────────────────────────────


def from_mermaid_flow(
    path: str | Path,
    *,
    module_name: str = "philip_mermaid_flow_dag",
) -> ModuleType:
    """Lift a Mermaid flowchart file into a Hamilton-compatible module."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    return from_mermaid_flow_text(text, module_name=module_name)


def from_mermaid_flow_text(
    source: str,
    *,
    module_name: str = "philip_mermaid_flow_dag",
) -> ModuleType:
    """Lift a Mermaid flowchart source string into a Hamilton module."""
    from hamilton.ad_hoc_utils import create_module

    nodes, edges = _parse(source)
    if not nodes:
        raise MermaidFlowLiftError("no nodes parsed from diagram")
    _check_for_cycles(nodes, edges)

    node_sources = [_node_function_source(node, edges) for node in nodes.values()]
    source_text = _module_preamble() + "\n\n" + "\n\n".join(node_sources) + "\n"
    return create_module(source_text, module_name=module_name)


# ── Parser ────────────────────────────────────────────────────────────────


_DIRECTIVE = re.compile(r"^\s*(?:flowchart|graph)\b(?:\s+(?:TD|TB|BT|LR|RL))?\s*$")
_SUBGRAPH_OPEN = re.compile(r"^\s*subgraph\b", re.IGNORECASE)
_END_KEYWORD = re.compile(r"^\s*end\s*$", re.IGNORECASE)
# Node id followed by an optional shape with label. Shapes we recognize:
# []  rect, ()  rounded, (())  circle, {}  diamond, ([])  stadium.
# We don't validate the bracket pair beyond reading the label text.
_NODE_DECL = re.compile(
    r"""
    ^\s*
    (?P<id>[A-Za-z_]\w*)
    (?:                       # optional shape with label
        \[\[(?P<label_subroutine>[^\]]*)\]\]
      | \(\((?P<label_circle>[^)]*)\)\)
      | \(\[(?P<label_stadium>[^\]]*)\]\)
      | \[(?P<label_rect>[^\]]*)\]
      | \((?P<label_round>[^)]*)\)
      | \{(?P<label_diamond>[^}]*)\}
    )?
    \s*$
    """,
    re.VERBOSE,
)
# Edge: A --> B,  A -- text --> B,  A -->|text| B,  with optional inline shape
# on either node. We capture the raw text and post-process.
_EDGE = re.compile(
    r"""
    ^\s*
    (?P<lhs>.+?)
    \s*
    (?P<arrow>
        -->                       # plain arrow
        | -\.->                  # dotted
        | ==>                    # thick
    )
    (?:\s*\|(?P<piped_label>[^|]*)\|)?
    \s*
    (?P<rhs>.+?)
    \s*$
    """,
    re.VERBOSE,
)
_DASH_LABEL_EDGE = re.compile(
    r"""
    ^\s*
    (?P<lhs>.+?)
    \s+--\s+               # whitespace required to disambiguate `A -- label -->`
    (?P<dash_label>[^->]+?)
    \s*-->
    \s*
    (?P<rhs>.+?)
    \s*$
    """,
    re.VERBOSE,
)
_COMMENT_PREFIXES = ("%%",)


def _parse(source: str) -> tuple[dict[str, MermaidNode], list[tuple[str, str]]]:
    in_block = False
    saw_directive = False
    nodes: dict[str, _NodeAcc] = {}
    edges: list[tuple[str, str]] = []

    for ln, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not in_block:
            if _DIRECTIVE.match(raw):
                in_block = True
                saw_directive = True
            continue
        if line.startswith(_COMMENT_PREFIXES):
            continue
        if _SUBGRAPH_OPEN.match(line):
            raise MermaidFlowLiftError(
                "subgraphs are not lifted in v1; flatten the diagram before lifting",
                line=raw,
                line_number=ln,
            )
        if _END_KEYWORD.match(line):
            # Stray `end` without an open subgraph; ignore.
            continue

        # Try dash-label edge (`A -- text --> B`) first; the plain `_EDGE`
        # regex would consume it greedily.
        dash = _DASH_LABEL_EDGE.match(line)
        edge = _EDGE.match(line) if dash is None else None
        if dash is not None:
            lhs, rhs = dash.group("lhs"), dash.group("rhs")
            _consume_edge_chain(lhs, rhs, nodes, edges, raw, ln)
            continue
        if edge is not None:
            lhs, rhs = edge.group("lhs"), edge.group("rhs")
            _consume_edge_chain(lhs, rhs, nodes, edges, raw, ln)
            continue
        decl = _NODE_DECL.match(line)
        if decl is not None:
            _ingest_node_decl(decl, nodes, raw, ln)
            continue
        raise MermaidFlowLiftError(
            "unrecognized diagram line; expected node decl or edge",
            line=raw,
            line_number=ln,
        )

    if not saw_directive:
        raise MermaidFlowLiftError("missing `flowchart` or `graph` directive")
    finalised = {nid: acc.to_node() for nid, acc in nodes.items()}
    return finalised, edges


# ── Internal parser state ─────────────────────────────────────────────────


@dataclass
class _NodeAcc:
    """Accumulator for one node during parsing; finalises into MermaidNode."""

    id: str
    label: str = ""
    shape: str = "rect"
    depends_on: list[str] = field(default_factory=list)

    def add_dep(self, upstream: str) -> None:
        if upstream not in self.depends_on:
            self.depends_on.append(upstream)

    def to_node(self) -> MermaidNode:
        return MermaidNode(
            id=self.id,
            label=self.label or self.id,
            depends_on=tuple(self.depends_on),
            shape=self.shape,
        )


def _ingest_node_decl(decl: re.Match[str], nodes: dict[str, _NodeAcc], raw: str, ln: int) -> None:
    node_id = decl.group("id")
    _check_identifier(node_id, raw, ln)
    label, shape = _label_and_shape(decl)
    acc = nodes.setdefault(node_id, _NodeAcc(id=node_id))
    if label:
        acc.label = label
    if shape:
        acc.shape = shape


_SHAPE_GROUPS = (
    ("label_subroutine", "subroutine"),
    ("label_circle", "circle"),
    ("label_stadium", "stadium"),
    ("label_rect", "rect"),
    ("label_round", "rounded"),
    ("label_diamond", "diamond"),
)


def _label_and_shape(decl: re.Match[str]) -> tuple[str, str]:
    for group, shape in _SHAPE_GROUPS:
        text = decl.group(group)
        if text is not None:
            return text.strip(), shape
    return "", ""


def _consume_edge_chain(
    lhs: str,
    rhs: str,
    nodes: dict[str, _NodeAcc],
    edges: list[tuple[str, str]],
    raw: str,
    ln: int,
) -> None:
    """Handle ``A --> B`` and chained ``A --> B --> C`` on one line.

    The plain ``_EDGE`` regex captures only the first hop; if the right
    side itself contains an arrow, we recurse so the chain lifts.
    """
    lhs_id = _ingest_inline_node(lhs, nodes, raw, ln)
    # Detect chained arrows by re-applying the regex on the right side.
    chained = _EDGE.match(rhs)
    if chained is not None:
        inner_lhs = chained.group("lhs").strip()
        inner_rhs = chained.group("rhs").strip()
        # Inline node on the head of the chain (the rhs of this hop).
        rhs_id = _ingest_inline_node(inner_lhs, nodes, raw, ln)
        _record_edge(lhs_id, rhs_id, nodes, edges)
        _consume_edge_chain(inner_lhs, inner_rhs, nodes, edges, raw, ln)
        return
    rhs_id = _ingest_inline_node(rhs, nodes, raw, ln)
    _record_edge(lhs_id, rhs_id, nodes, edges)


def _ingest_inline_node(fragment: str, nodes: dict[str, _NodeAcc], raw: str, ln: int) -> str:
    """Parse a node id (with optional inline shape+label) from a fragment."""
    decl = _NODE_DECL.match(fragment.strip())
    if decl is None:
        raise MermaidFlowLiftError(
            f"could not parse node reference in {fragment!r}",
            line=raw,
            line_number=ln,
        )
    _ingest_node_decl(decl, nodes, raw, ln)
    return decl.group("id")


def _record_edge(
    src: str, dst: str, nodes: dict[str, _NodeAcc], edges: list[tuple[str, str]]
) -> None:
    if (src, dst) in edges:
        return
    edges.append((src, dst))
    nodes[dst].add_dep(src)


# ── Cycle detection ───────────────────────────────────────────────────────


def _check_for_cycles(nodes: dict[str, MermaidNode], edges: list[tuple[str, str]]) -> None:
    """Hamilton DAGs reject cycles. Iterative DFS to report the offending pair."""
    successors: dict[str, list[str]] = {nid: [] for nid in nodes}
    for src, dst in edges:
        successors[src].append(dst)
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(nodes, WHITE)
    parent: dict[str, str] = {}

    def dfs(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GREY
        while stack:
            node, idx = stack[-1]
            succs = successors[node]
            if idx >= len(succs):
                color[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, idx + 1)
            nxt = succs[idx]
            if color[nxt] == WHITE:
                parent[nxt] = node
                color[nxt] = GREY
                stack.append((nxt, 0))
            elif color[nxt] == GREY:
                raise MermaidFlowLiftError(
                    f"cycle detected: {node} -> {nxt} would close a loop; Hamilton requires a DAG"
                )

    for nid in nodes:
        if color[nid] == WHITE:
            dfs(nid)


# ── Source generation ────────────────────────────────────────────────────


_IDENT = re.compile(r"^[A-Za-z_]\w*$")


def _check_identifier(name: str, raw: str, ln: int) -> None:
    if not _IDENT.match(name) or keyword.iskeyword(name):
        raise MermaidFlowLiftError(
            f"node id {name!r} is not a valid Python identifier",
            line=raw,
            line_number=ln,
        )


def _module_preamble() -> str:
    return "from philip._lifters.mermaid_flow import MermaidNode\n"


def _node_function_source(node: MermaidNode, edges: list[tuple[str, str]]) -> str:
    params = ", ".join(f"{dep}: MermaidNode" for dep in node.depends_on)
    return (
        f"def {node.id}({params}) -> MermaidNode:\n"
        f"    '''Lifted Mermaid node {node.id!r}: {node.label}.'''\n"
        f"    return MermaidNode(\n"
        f"        id={node.id!r},\n"
        f"        label={node.label!r},\n"
        f"        depends_on={node.depends_on!r},\n"
        f"        shape={node.shape!r},\n"
        f"    )"
    )
