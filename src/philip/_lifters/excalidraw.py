"""Lift an Excalidraw sketch into a :class:`philip.PhilipGraph`.

Excalidraw is a hand-drawn-style diagramming tool whose native format is
JSON. Every shape carries an ``id``; arrows carry ``startBinding`` and
``endBinding`` records that explicitly name the source and destination
shape ids. That makes the sketch a typed directed graph already; the
lifter walks it and produces a :class:`PhilipGraph`. The caller picks
the substrate via :meth:`PhilipGraph.to_burr` or
:meth:`PhilipGraph.to_hamilton`.

Three input forms:

* ``.excalidraw`` — native JSON. Direct ``json.loads``.
* ``.excalidraw.svg`` — Excalidraw's default SVG export, which embeds
  the full JSON payload in an ``<!-- payload-start -->`` block as
  base64. The lifter extracts and decodes the payload, falling through
  to the JSON path.
* Pure ``.svg`` without an embedded payload — refused. Parsing arbitrary
  SVG into a typed graph requires shape-vs-arrow heuristics and is out
  of scope for the deterministic lift; that path is reserved for a
  future LLM-driven SKILL.

Label resolution:

* Excalidraw 0.16+ binds text to a containing shape via ``containerId``;
  the text becomes the shape's label.
* Older or hand-positioned standalone text without ``containerId`` is
  associated to a shape by bounding-box containment (text origin
  inside the shape's rectangle).
* Shapes that resolve no label keep an empty label and get a generated
  node id derived from the Excalidraw element id.

Arrow handling:

* Arrows with both ``startBinding.elementId`` and ``endBinding.elementId``
  set, and both referenced shapes present, lift to edges.
* Dangling arrows (missing binding on either end) are silently dropped;
  this matches user intent (an unbound arrow is a sketching artifact).
* Arrow labels via ``containerId`` lift onto the resulting edge.

Element types skipped: ``freedraw``, ``line``, ``image``, ``frame``,
``embeddable``, deleted elements (``isDeleted: true``). Only
``rectangle``, ``ellipse``, and ``diamond`` become nodes.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from philip._ir import Edge, Node, PhilipGraph

# ── Public API ────────────────────────────────────────────────────────────


class ExcalidrawLiftError(ValueError):
    """Raised when an Excalidraw input cannot be lifted into a PhilipGraph."""


def from_excalidraw(path: str | Path) -> PhilipGraph:
    """Lift an ``.excalidraw`` or ``.excalidraw.svg`` file into a PhilipGraph."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    return from_excalidraw_text(text)


def from_excalidraw_text(source: str) -> PhilipGraph:
    """Lift an Excalidraw payload string (JSON or default-export SVG) into a PhilipGraph."""
    stripped = source.lstrip()
    data: dict[str, Any]
    if stripped.startswith("<"):
        data = _extract_payload_from_svg(source)
    else:
        try:
            data = json.loads(source)
        except json.JSONDecodeError as e:
            raise ExcalidrawLiftError(f"input is neither valid JSON nor an SVG: {e}") from e

    if data.get("type") != "excalidraw":
        raise ExcalidrawLiftError(
            f"not an Excalidraw document (expected type='excalidraw', got {data.get('type')!r})"
        )
    elements = data.get("elements") or []
    if not isinstance(elements, list):
        raise ExcalidrawLiftError(f"'elements' must be a list; got {type(elements).__name__}")
    return _build_graph(elements)


# ── SVG payload extraction ────────────────────────────────────────────────

# Excalidraw's default SVG export wraps the JSON payload between
# ``<!-- payload-start --><payload-type>...</payload-type><payload-version>...
# <payload-encoding>base64</payload-encoding><payload-start-2>...<!-- payload-end -->``.
# In practice the wrapper varies slightly across Excalidraw versions, so
# the extractor is forgiving: it looks for the base64 blob between the
# start and end markers regardless of the intermediate tags.
_PAYLOAD_REGEX = re.compile(
    r"<!--\s*payload-start\s*-->(.+?)<!--\s*payload-end\s*-->",
    re.DOTALL,
)
_B64_BLOB = re.compile(r"[A-Za-z0-9+/=\s]{32,}")


def _extract_payload_from_svg(svg_text: str) -> dict[str, Any]:
    m = _PAYLOAD_REGEX.search(svg_text)
    if m is None:
        raise ExcalidrawLiftError(
            "SVG has no embedded Excalidraw payload; this is not a default Excalidraw "
            "SVG export. Save as .excalidraw or use 'Export to SVG' (not 'Save as SVG')."
        )
    inner = m.group(1)
    # The inner block has tag markup we ignore; the only thing that
    # matters is the longest base64-shaped string in it.
    candidates = _B64_BLOB.findall(inner)
    if not candidates:
        raise ExcalidrawLiftError(
            "SVG payload block does not contain a base64 blob; the export may be corrupted"
        )
    blob = max(candidates, key=len).replace("\n", "").replace(" ", "")
    try:
        decoded = base64.b64decode(blob, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise ExcalidrawLiftError(f"SVG payload base64 decode failed: {e}") from e
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as e:
        raise ExcalidrawLiftError(f"SVG payload is not valid JSON: {e}") from e


# ── Graph construction ────────────────────────────────────────────────────


_NODE_SHAPES = frozenset({"rectangle", "ellipse", "diamond"})


def _build_graph(elements: list[dict[str, Any]]) -> PhilipGraph:
    shapes: dict[str, dict[str, Any]] = {}
    arrows: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []

    for el in elements:
        if not isinstance(el, dict):
            continue
        if el.get("isDeleted"):
            continue
        kind = el.get("type", "")
        if kind in _NODE_SHAPES:
            shapes[el["id"]] = el
        elif kind == "text":
            texts.append(el)
        elif kind == "arrow":
            arrows.append(el)
        # ignore freedraw, line, image, frame, embeddable

    if not shapes:
        raise ExcalidrawLiftError(
            "diagram has no shapes (rectangle/ellipse/diamond); nothing to lift"
        )

    labels_by_eid = _resolve_shape_labels(shapes, texts)
    # Map Excalidraw element ids to Python-safe IR node ids, deduplicating.
    eid_to_node_id: dict[str, str] = {}
    used: set[str] = set()
    nodes: list[Node] = []
    for eid in shapes:
        label = labels_by_eid.get(eid, "")
        node_id = _pick_node_id(eid, label, used)
        used.add(node_id)
        eid_to_node_id[eid] = node_id
        nodes.append(Node(id=node_id, label=label, shape=shapes[eid].get("type", "")))

    edges: list[Edge] = []
    for arrow in arrows:
        sb = arrow.get("startBinding") or {}
        eb = arrow.get("endBinding") or {}
        src_eid = sb.get("elementId")
        dst_eid = eb.get("elementId")
        if not src_eid or not dst_eid:
            continue
        if src_eid not in eid_to_node_id or dst_eid not in eid_to_node_id:
            continue
        edges.append(
            Edge(
                src=eid_to_node_id[src_eid],
                dst=eid_to_node_id[dst_eid],
                label=_arrow_label(arrow, texts),
            )
        )

    return PhilipGraph(
        nodes=tuple(nodes),
        edges=tuple(edges),
        source_format="excalidraw",
    )


def _resolve_shape_labels(
    shapes: dict[str, dict[str, Any]],
    texts: list[dict[str, Any]],
) -> dict[str, str]:
    """For each shape id, find the most-likely text label.

    First pass uses Excalidraw 0.16+'s ``containerId`` binding. Second
    pass falls back to bounding-box containment for orphan texts.
    """
    labels: dict[str, str] = {}
    used_text_ids: set[str] = set()

    for text in texts:
        cid = text.get("containerId")
        if cid and cid in shapes:
            labels[cid] = (text.get("text") or "").strip()
            used_text_ids.add(text.get("id", ""))

    for sid, shape in shapes.items():
        if sid in labels:
            continue
        for text in texts:
            if text.get("id", "") in used_text_ids:
                continue
            if text.get("containerId"):
                continue
            if _contains_text_origin(shape, text):
                labels[sid] = (text.get("text") or "").strip()
                used_text_ids.add(text.get("id", ""))
                break

    return labels


def _arrow_label(arrow: dict[str, Any], texts: list[dict[str, Any]]) -> str:
    aid = arrow.get("id", "")
    for text in texts:
        if text.get("containerId") == aid:
            return (text.get("text") or "").strip()
    return ""


def _contains_text_origin(shape: dict[str, Any], text: dict[str, Any]) -> bool:
    sx = float(shape.get("x", 0))
    sy = float(shape.get("y", 0))
    sw = float(shape.get("width", 0))
    sh = float(shape.get("height", 0))
    tx = float(text.get("x", 0))
    ty = float(text.get("y", 0))
    return sx <= tx <= sx + sw and sy <= ty <= sy + sh


# ── Node id generation ────────────────────────────────────────────────────


def _pick_node_id(eid: str, label: str, used: set[str]) -> str:
    """Pick a Python-safe node id from the label, falling back to the element id."""
    candidate = _slug(label) if label else ""
    if not candidate:
        # Excalidraw ids look like "abc123XYZ"; sanitize the first segment.
        candidate = "node_" + re.sub(r"\W", "_", eid)[:16]
    if not candidate[:1].isalpha() and candidate[:1] != "_":
        candidate = "node_" + candidate
    if candidate in used:
        i = 2
        while f"{candidate}_{i}" in used:
            i += 1
        candidate = f"{candidate}_{i}"
    return candidate


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text.strip()).strip("_")
