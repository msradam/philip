"""Format-specific lifters: source artifact -> Burr ``Application``.

Each module in this package implements one ``from_<format>`` deterministic
lift. The Ansible playbook lift lives at the package root in
:mod:`philip._convert` for historical reasons; new format lifters land
here.

Currently shipped:

* :func:`philip.from_mermaid` (Mermaid stateDiagram-v2 -> Burr)
"""

from philip._lifters.mermaid import (
    MermaidLiftError,
    from_mermaid,
    from_mermaid_text,
)

__all__ = ["MermaidLiftError", "from_mermaid", "from_mermaid_text"]
