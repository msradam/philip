"""Format-specific lifters into the Burr (FSM) and Hamilton (DAG) substrates.

Each module here implements one ``from_<format>`` deterministic lift.
The original Ansible playbook lift lives at the package root in
:mod:`philip._convert` for historical reasons; new format lifters land
here.

FSM lifters (return ``burr.core.Application``):

* :func:`philip.from_mermaid` (Mermaid stateDiagram-v2 -> Burr)

DAG lifters (return a Hamilton-compatible Python module):

* :func:`philip.from_sql_cte` (SQL with CTEs -> Hamilton DAG)
  Requires the ``hamilton`` extra: ``pip install 'philip-machine[hamilton]'``.
"""

from philip._lifters.mermaid import (
    MermaidLiftError,
    from_mermaid,
    from_mermaid_text,
)

# Hamilton lifters are gated on the optional ``hamilton`` extra. Importing
# them lazily lets the core install work without ``sf-hamilton`` and
# ``sqlglot`` present.
try:
    from philip._lifters.sql_cte import SqlCteLiftError as SqlCteLiftError
    from philip._lifters.sql_cte import SqlNode as SqlNode
    from philip._lifters.sql_cte import from_sql_cte as from_sql_cte

    _HAMILTON_EXPORTS = ["SqlCteLiftError", "SqlNode", "from_sql_cte"]
except ImportError:  # pragma: no cover - exercised by environments missing the extra
    _HAMILTON_EXPORTS = []

__all__ = [
    "MermaidLiftError",
    *_HAMILTON_EXPORTS,
    "from_mermaid",
    "from_mermaid_text",
]
