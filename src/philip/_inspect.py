"""Static introspection of an Ansible playbook before/without execution.

``philip.inspect(path)`` returns an :class:`InspectionReport` carrying two
analyses computed from the playbook source (and the lifted FSM that
:func:`philip.from_playbook` produces):

* **Variable provenance.** Every ``{{ var_name }}`` reference in module
  args, ``when:`` predicates, ``failed_when:``, and templated values is
  traced back to every site that defines that name. Definition sources
  surfaced: playbook ``vars:``, role ``defaults/main.yml`` and
  ``vars/main.yml``, ``set_fact:`` actions, ``register:`` captures, and
  caller-supplied ``extra_vars``. ``host_vars`` and ``group_vars`` are
  not resolved in v1.0 because they require an inventory file; pass
  ``extra_vars`` to model their values for now.

* **Failure topology.** Walks the FSM lifted by :func:`from_playbook` and
  reports, per action, which transitions are reachable on each
  :data:`FAILURE_KIND`. Actions whose failure has no recovery path
  (``escalate`` is the only destination, or no transition at all) are
  flagged as dead-end failures.

Both analyses are deterministic and run without invoking ``ansible-runner``
or contacting any target host.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from philip._action import FAILURE_KIND_OK, FAILURE_KINDS
from philip._convert import from_playbook

# ── Public data types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class VariableUse:
    """A single ``{{ name }}`` reference site inside the playbook."""

    name: str
    where: str  # "play[0].tasks[3].args.dest" style path
    raw_expression: str  # the full Jinja expression, e.g. "{{ pkg_name | upper }}"


@dataclass(frozen=True)
class VariableDefinition:
    """A single site that binds a variable name."""

    name: str
    source: str  # "playbook_vars", "role_defaults", "role_vars",
    #                                 "set_fact", "register", "extra_vars"
    where: str  # human-readable location string
    value_repr: str = ""  # short repr of the bound value if statically known


@dataclass(frozen=True)
class VariableProvenance:
    """Per-variable definition list + use list."""

    name: str
    definitions: tuple[VariableDefinition, ...]
    uses: tuple[VariableUse, ...]

    @property
    def is_undefined(self) -> bool:
        """No definition site found; this reference would resolve via runtime facts only."""
        return not self.definitions

    @property
    def is_unused(self) -> bool:
        """Defined but never referenced. Often dead state."""
        return not self.uses


@dataclass(frozen=True)
class FailureEdge:
    """An action's behavior on one FAILURE_KIND classification."""

    failure_kind: str
    destination: str | None  # action name; None means no transition matched
    is_escalation: bool  # True when destination is the auto-generated escalate terminal
    is_unhandled: bool  # True when no transition matches (will raise at runtime)


@dataclass(frozen=True)
class ActionFailureTopology:
    """The failure-routing profile for one action."""

    action: str
    edges: tuple[FailureEdge, ...]

    @property
    def has_unhandled_failure(self) -> bool:
        """No matching transition; the action body would raise without a guard."""
        return any(e.is_unhandled for e in self.edges)

    @property
    def has_recovery_branch(self) -> bool:
        """At least one failure routes somewhere other than escalate (true recovery)."""
        return any(
            e.destination is not None and not e.is_escalation and not e.is_unhandled
            for e in self.edges
        )


@dataclass(frozen=True)
class InspectionReport:
    """Result of :func:`philip.inspect`."""

    playbook_path: str
    variables: tuple[VariableProvenance, ...]
    failure_topology: tuple[ActionFailureTopology, ...]
    unsupported_constructs: tuple[str, ...] = ()  # populated if from_playbook refused

    # ── Convenience views ──────────────────────────────────────────────────

    @property
    def undefined_variables(self) -> tuple[VariableProvenance, ...]:
        return tuple(v for v in self.variables if v.is_undefined)

    @property
    def unused_definitions(self) -> tuple[VariableProvenance, ...]:
        return tuple(v for v in self.variables if v.is_unused)

    @property
    def unhandled_failures(self) -> tuple[ActionFailureTopology, ...]:
        return tuple(a for a in self.failure_topology if a.has_unhandled_failure)

    @property
    def actions_with_recovery(self) -> tuple[ActionFailureTopology, ...]:
        return tuple(a for a in self.failure_topology if a.has_recovery_branch)

    def rendered_markdown(self) -> str:
        """Single human-readable inspection report."""
        return _render_markdown(self)


# ── Public entry point ─────────────────────────────────────────────────────


def inspect(
    playbook_path: str | Path,
    *,
    extra_vars: Mapping[str, Any] | None = None,
) -> InspectionReport:
    """Statically inspect an Ansible playbook.

    Reads the playbook file and any referenced roles, extracts variable
    references and definitions, lifts to a Burr Application via
    :func:`from_playbook`, and walks the resulting FSM to compute failure
    topology. Does not execute any module.

    ``extra_vars`` models caller-supplied ``--extra-vars`` and adds
    definition sites at the highest precedence.
    """
    path = Path(playbook_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"playbook not found: {path}")

    raw = yaml.safe_load(path.read_text()) or []

    var_provenance = _analyse_variables(raw, path, extra_vars or {})
    failure_topology, unsupported = _analyse_failure_topology(path)

    return InspectionReport(
        playbook_path=str(path),
        variables=var_provenance,
        failure_topology=failure_topology,
        unsupported_constructs=unsupported,
    )


# ── Variable analysis ──────────────────────────────────────────────────────

# Matches `{{ name }}`, `{{ name.attr }}`, `{{ name['key'] }}`, with optional
# pipe filters which we strip. Top-level names only; downstream attribute
# access is captured separately for context but the provenance key is the
# leading identifier.
_JINJA_REF = re.compile(r"\{\{\s*([A-Za-z_][\w\.\[\]'\"]*)\s*(\|[^}]*)?\s*\}\}")
_NAME_ROOT = re.compile(r"^([A-Za-z_]\w*)")
# Bare-identifier scan for Ansible predicate expressions (``when:``,
# ``failed_when:``, ``changed_when:``, ``until:``). These are implicit-Jinja:
# variable names appear unbraced. Strings, numbers, and Python keywords are
# filtered out separately.
_BARE_IDENTIFIER = re.compile(r"\b([A-Za-z_]\w*(?:\.\w+)*)\b")
_PYTHON_KEYWORDS = frozenset(
    {
        "and",
        "or",
        "not",
        "is",
        "in",
        "if",
        "else",
        "elif",
        "True",
        "False",
        "None",
        "lambda",
        "yes",
        "no",
        "true",
        "false",
        "none",
    }
)

# Ansible built-in / context names that look like variable references but
# are runtime-supplied. Treating them as "defined-by-runtime" keeps the
# undefined list useful.
_RUNTIME_PROVIDED = frozenset(
    {
        "ansible_facts",
        "ansible_hostname",
        "ansible_distribution",
        "ansible_distribution_version",
        "ansible_os_family",
        "ansible_user",
        "ansible_play_hosts",
        "ansible_play_batch",
        "ansible_loop",
        "ansible_failed_task",
        "ansible_failed_result",
        "inventory_hostname",
        "groups",
        "group_names",
        "hostvars",
        "item",  # set inside loop bodies
        "play_hosts",
        "playbook_dir",
        "role_name",
        "role_path",
    }
)


def _analyse_variables(
    raw: Any,
    playbook_path: Path,
    extra_vars: Mapping[str, Any],
) -> tuple[VariableProvenance, ...]:
    uses: list[VariableUse] = []
    definitions: list[VariableDefinition] = []

    # Caller-supplied extras: highest precedence, take them as defined first.
    for name, value in extra_vars.items():
        definitions.append(
            VariableDefinition(
                name=name,
                source="extra_vars",
                where="caller",
                value_repr=_brief_repr(value),
            )
        )

    plays = raw if isinstance(raw, list) else [raw]
    for play_idx, play in enumerate(plays):
        if not isinstance(play, dict):
            continue
        play_path = f"play[{play_idx}]"

        # Play-level vars
        for name, value in (play.get("vars") or {}).items():
            definitions.append(
                VariableDefinition(
                    name=name,
                    source="playbook_vars",
                    where=f"{play_path}.vars",
                    value_repr=_brief_repr(value),
                )
            )
            # Jinja references inside the bound value count as uses too
            _scan_value_for_uses(value, f"{play_path}.vars.{name}", uses)

        # Roles: read defaults/main.yml and vars/main.yml from each role
        for role_idx, role_entry in enumerate(play.get("roles") or []):
            role_name = role_entry if isinstance(role_entry, str) else role_entry.get("role", "")
            if role_name:
                _scan_role(
                    role_name,
                    playbook_path.parent,
                    f"{play_path}.roles[{role_idx}]",
                    definitions,
                    uses,
                )

        # Tasks: scan args, when:, failed_when:, register:, set_fact:, loop:
        for section_name in ("pre_tasks", "tasks", "post_tasks", "handlers"):
            for task_idx, task in enumerate(play.get(section_name) or []):
                if not isinstance(task, dict):
                    continue
                _scan_task(task, f"{play_path}.{section_name}[{task_idx}]", definitions, uses)

    # Build provenance per variable name
    by_name: dict[str, list[VariableDefinition]] = {}
    for d in definitions:
        by_name.setdefault(d.name, []).append(d)
    uses_by_name: dict[str, list[VariableUse]] = {}
    for u in uses:
        uses_by_name.setdefault(u.name, []).append(u)

    all_names = sorted(set(by_name) | set(uses_by_name))
    return tuple(
        VariableProvenance(
            name=name,
            definitions=tuple(by_name.get(name, [])),
            uses=tuple(uses_by_name.get(name, [])),
        )
        for name in all_names
        if name not in _RUNTIME_PROVIDED
    )


def _scan_task(
    task: dict[str, Any],
    where: str,
    definitions: list[VariableDefinition],
    uses: list[VariableUse],
) -> None:
    """Scan one task dict for definitions and references."""
    if "register" in task:
        definitions.append(
            VariableDefinition(
                name=str(task["register"]),
                source="register",
                where=f"{where}.register",
                value_repr="<module result>",
            )
        )
    # set_fact accepts both bare and FQCN form.
    set_fact_dict: dict[str, Any] | None = None
    for fact_key in ("set_fact", "ansible.builtin.set_fact"):
        if isinstance(task.get(fact_key), dict):
            set_fact_dict = task[fact_key]
            break
    if set_fact_dict is not None:
        for name, value in set_fact_dict.items():
            definitions.append(
                VariableDefinition(
                    name=name,
                    source="set_fact",
                    where=f"{where}.set_fact",
                    value_repr=_brief_repr(value),
                )
            )
            _scan_value_for_uses(value, f"{where}.set_fact.{name}", uses)

    for key in ("when", "failed_when", "changed_when", "until"):
        if key in task:
            _scan_predicate_for_uses(task[key], f"{where}.{key}", uses)

    if "loop" in task:
        _scan_value_for_uses(task["loop"], f"{where}.loop", uses)

    # Module args live under whatever key is the module name. Skip well-known
    # non-arg top-level keys and treat the rest as arg dicts.
    skip = {
        "name",
        "when",
        "failed_when",
        "changed_when",
        "until",
        "register",
        "loop",
        "with_items",
        "set_fact",
        "vars",
        "tags",
        "become",
        "become_user",
        "ignore_errors",
        "notify",
        "delegate_to",
        "block",
        "rescue",
        "always",
        "include_tasks",
        "import_tasks",
        "include_role",
        "import_role",
        "include",
        "no_log",
        "check_mode",
        "diff",
    }
    for key, value in task.items():
        if key in skip:
            continue
        _scan_value_for_uses(value, f"{where}.{key}", uses)

    # Nested block bodies recurse.
    for nested_key in ("block", "rescue", "always"):
        for sub_idx, sub_task in enumerate(task.get(nested_key) or []):
            if isinstance(sub_task, dict):
                _scan_task(sub_task, f"{where}.{nested_key}[{sub_idx}]", definitions, uses)


def _scan_value_for_uses(value: Any, where: str, uses: list[VariableUse]) -> None:
    """Walk a YAML value of any shape; record every Jinja reference."""
    if isinstance(value, str):
        for m in _JINJA_REF.finditer(value):
            full_expr = m.group(0)
            head = m.group(1)
            root_match = _NAME_ROOT.match(head)
            if root_match:
                root = root_match.group(1)
                uses.append(VariableUse(name=root, where=where, raw_expression=full_expr))
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_value_for_uses(v, f"{where}.{k}", uses)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_value_for_uses(v, f"{where}[{i}]", uses)


def _scan_predicate_for_uses(value: Any, where: str, uses: list[VariableUse]) -> None:
    """Scan an Ansible predicate (``when:`` etc.) for bare identifiers and Jinja.

    Predicates are implicit-Jinja: ``when: skip_me`` references ``skip_me``
    without ``{{ }}``. We strip string literals first, then scan the residue
    for bare identifiers that look like variable names, filtering Python
    keywords and Ansible truthy/falsy literals.
    """
    if isinstance(value, list):
        for i, v in enumerate(value):
            _scan_predicate_for_uses(v, f"{where}[{i}]", uses)
        return
    if not isinstance(value, str):
        return

    # Pick up explicit Jinja first (a string can mix both forms).
    for m in _JINJA_REF.finditer(value):
        full_expr = m.group(0)
        head = m.group(1)
        root_match = _NAME_ROOT.match(head)
        if root_match:
            root = root_match.group(1)
            uses.append(VariableUse(name=root, where=where, raw_expression=full_expr))

    # Strip string literals to avoid catching identifiers inside quotes.
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "", value)
    # Strip out the Jinja blocks so we don't double-count their identifiers.
    stripped = _JINJA_REF.sub("", stripped)

    for m in _BARE_IDENTIFIER.finditer(stripped):
        ident = m.group(1)
        root = ident.split(".", 1)[0]
        if root in _PYTHON_KEYWORDS:
            continue
        if root.isdigit():
            continue
        uses.append(VariableUse(name=root, where=where, raw_expression=ident))


def _scan_role(
    role_name: str,
    playbook_dir: Path,
    where: str,
    definitions: list[VariableDefinition],
    uses: list[VariableUse],
) -> None:
    """Read defaults/main.yml and vars/main.yml from a role directory if found."""
    candidates = [
        playbook_dir / "roles" / role_name,
        playbook_dir.parent / "roles" / role_name,
    ]
    role_root = next((c for c in candidates if c.is_dir()), None)
    if role_root is None:
        return

    for sub, source in (("defaults", "role_defaults"), ("vars", "role_vars")):
        f = role_root / sub / "main.yml"
        if f.is_file():
            try:
                data = yaml.safe_load(f.read_text()) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data, dict):
                for name, value in data.items():
                    definitions.append(
                        VariableDefinition(
                            name=name,
                            source=source,
                            where=f"{where}({role_name})/{sub}/main.yml",
                            value_repr=_brief_repr(value),
                        )
                    )
                    _scan_value_for_uses(value, f"{where}({role_name})/{sub}.{name}", uses)


# ── Failure topology analysis ──────────────────────────────────────────────


def _analyse_failure_topology(
    playbook_path: Path,
) -> tuple[tuple[ActionFailureTopology, ...], tuple[str, ...]]:
    """Lift via from_playbook, then read failure routing off the graph."""
    try:
        app = from_playbook(str(playbook_path))
    except Exception as e:
        return (), (f"from_playbook refused: {type(e).__name__}: {e}",)

    graph = app.graph
    actions_by_name = {a.name: a for a in graph.actions}

    topology: list[ActionFailureTopology] = []
    for act in graph.actions:
        if act.name in {"done", "escalate"}:
            continue
        out_transitions = [t for t in graph.transitions if t.from_.name == act.name]
        edges: list[FailureEdge] = []
        for kind in FAILURE_KINDS:
            if kind == FAILURE_KIND_OK:
                continue
            dest = _resolve_failure_destination(kind, out_transitions, actions_by_name)
            edges.append(
                FailureEdge(
                    failure_kind=kind,
                    destination=dest,
                    is_escalation=(dest == "escalate"),
                    is_unhandled=(dest is None),
                )
            )
        if edges:
            topology.append(ActionFailureTopology(action=act.name, edges=tuple(edges)))

    return tuple(topology), ()


def _resolve_failure_destination(
    failure_kind: str,
    out_transitions: list[Any],
    actions_by_name: dict[str, Any],
) -> str | None:
    """Pick the transition that would fire on this FAILURE_KIND.

    Best-effort static read. We look for a transition whose condition
    references ``_last_failed`` (any failure) or ``_last_failure_kind``
    (specific). If none match, we fall back to the default transition,
    which conventionally routes successful runs forward; in that case
    failure is unhandled and we return None.
    """
    escalate_dest: str | None = None
    default_dest: str | None = None
    for t in out_transitions:
        cond_src = _condition_source(t)
        if cond_src is None:
            default_dest = t.to.name
            continue
        if "_last_failed" in cond_src or "_last_failure_kind" in cond_src:
            # Any failure-routing transition: take the first one we find.
            return t.to.name
        if t.to.name == "escalate":
            escalate_dest = "escalate"
    if escalate_dest:
        return escalate_dest
    return default_dest if default_dest is not None else None


def _condition_source(transition: Any) -> str | None:
    """Return the source string of a transition condition, or None if default."""
    cond = getattr(transition, "condition", None)
    if cond is None:
        return None
    expr_src = getattr(cond, "expr", None) or getattr(cond, "expression", None)
    if expr_src is None:
        name = getattr(cond, "name", "")
        if name in ("default", ""):
            return None
        return name
    return str(expr_src)


# ── Markdown rendering ─────────────────────────────────────────────────────


def _render_markdown(report: InspectionReport) -> str:
    lines: list[str] = []
    lines.append(f"# philip inspect: {Path(report.playbook_path).name}")
    lines.append("")
    lines.append(f"Playbook: `{report.playbook_path}`")
    lines.append("")

    if report.unsupported_constructs:
        lines.append("## Lift refused")
        lines.append("")
        lines.extend(f"- {msg}" for msg in report.unsupported_constructs)
        lines.append("")
        return "\n".join(lines)

    # Variables
    lines.append("## Variable provenance")
    lines.append("")
    lines.append(f"Tracked: **{len(report.variables)}** variables.")
    lines.append(f"Undefined (resolved at runtime only): **{len(report.undefined_variables)}**.")
    lines.append(f"Defined but unused: **{len(report.unused_definitions)}**.")
    lines.append("")
    if report.variables:
        lines.append("| Name | Definitions | Uses | Status |")
        lines.append("|---|---|---|---|")
        for v in report.variables:
            status = []
            if v.is_undefined:
                status.append("undefined")
            if v.is_unused:
                status.append("unused")
            status_str = ", ".join(status) or "ok"
            def_summary = ", ".join(sorted({d.source for d in v.definitions})) or "(none)"
            lines.append(
                f"| `{v.name}` | {len(v.definitions)} ({def_summary}) | "
                f"{len(v.uses)} | {status_str} |"
            )
        lines.append("")

    if report.undefined_variables:
        lines.append("### Undefined references")
        lines.append("")
        for v in report.undefined_variables:
            lines.append(f"- `{v.name}` referenced at:")
            lines.extend(f"  - `{u.where}`: `{u.raw_expression}`" for u in v.uses[:5])
            if len(v.uses) > 5:
                lines.append(f"  - ... ({len(v.uses) - 5} more)")
        lines.append("")

    # Failure topology
    lines.append("## Failure topology")
    lines.append("")
    lines.append(f"Actions analysed: **{len(report.failure_topology)}**.")
    lines.append(f"Actions with a true recovery branch: **{len(report.actions_with_recovery)}**.")
    lines.append(
        f"Actions with at least one unhandled failure path: **{len(report.unhandled_failures)}**."
    )
    lines.append("")
    if report.failure_topology:
        lines.append("| Action | UNREACHABLE | AUTH_FAILED | TIMEOUT | MODULE_ERROR | Routing |")
        lines.append("|---|---|---|---|---|---|")
        for act in report.failure_topology:
            cells = {}
            for e in act.edges:
                if e.is_unhandled:
                    cells[e.failure_kind] = "**unhandled**"
                elif e.is_escalation:
                    cells[e.failure_kind] = "→ escalate"
                else:
                    cells[e.failure_kind] = f"→ {e.destination}"
            if act.has_unhandled_failure:
                routing = "unhandled"
            elif act.has_recovery_branch:
                routing = "recovery"
            else:
                routing = "escalation"
            lines.append(
                f"| `{act.action}` | "
                f"{cells.get('unreachable', '-')} | "
                f"{cells.get('auth_failed', '-')} | "
                f"{cells.get('timeout', '-')} | "
                f"{cells.get('module_error', '-')} | "
                f"{routing} |"
            )
        lines.append("")

    if report.unhandled_failures:
        lines.append("### Unhandled failures (no transition matched)")
        lines.append("")
        lines.extend(
            f"- `{act.action}` on `{e.failure_kind}`: no transition matched"
            for act in report.unhandled_failures
            for e in act.edges
            if e.is_unhandled
        )
        lines.append("")

    return "\n".join(lines)


# ── Internal helpers ───────────────────────────────────────────────────────


def _brief_repr(value: Any, limit: int = 60) -> str:
    s = repr(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."
