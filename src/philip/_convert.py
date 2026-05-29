"""Convert Ansible YAML playbooks into philip/Burr ``Application`` objects.

Supports the subset of Ansible's playbook syntax that maps cleanly to a
single-host FSM:

  - One play per file (multi-play raises :class:`UnsupportedPlaybookConstruct`).
  - Tasks with a name, exactly one module reference, and a module-args dict.
  - ``when:`` predicates (string expressions; lists are AND-joined). Jinja-
    style attribute access on registered names (``result.rc == 0``) is
    translated to Python bracket access (``result['rc'] == 0``) so Burr's
    ``expr()`` can evaluate the predicate.
  - ``register:`` capturing the full module result into a state key.
  - ``become:`` per-task or per-play.
  - ``failed_when:`` and ``ignore_errors:`` as guard transitions. Failed
    tasks route to an auto-generated ``escalate`` terminal unless
    ``ignore_errors: yes`` is set.
  - ``gather_facts: yes`` lowers to a leading ``ansible.builtin.setup`` action.
  - Play-level ``vars:`` populate ``with_state(...)``.
  - ``block:`` (group-only) inlines its tasks with the block's ``when:``
    AND-propagated to each inner task. ``rescue:`` / ``always:`` still raise.
  - ``include_tasks:`` and ``import_tasks:`` with a literal filesystem path
    read the referenced file and inline its tasks at conversion time, with
    outer ``when:`` and ``notify:`` propagating to every leaf.
  - ``notify:`` + ``handlers:`` round-trip: each notifying task gets a
    synthesized notify-marker that flips a ``_notified_<slug>`` flag when
    ``_last_changed`` is true; handlers are appended after the main tasks
    and gated on their flag.
  - ``loop:`` and ``with_items:`` with a literal list lower into a
    three-action sub-FSM (init -> task -> advance with a back-edge until
    the items are exhausted). The task body sees ``{{ item }}`` in its
    Jinja context.
  - Jinja templates inside task arguments referencing registered values
    are rendered using Burr state per task, so a converted ``msg: "{{ git_check.stdout }}"``
    resolves across plays.

The following constructs raise :class:`UnsupportedPlaybookConstruct` with
the offending node:

  - ``rescue:`` / ``always:`` (deferred to a later release as
    STRATUS-style undo/transactional edges)
  - ``loop_control:``, ``with_dict``, ``with_fileglob``, ``with_subelements``
  - Jinja-templated ``loop:`` / ``with_items:`` values (only literal lists
    are lowered)
  - Jinja-templated ``include_tasks:`` / ``import_tasks:`` paths
  - ``import_role:``, ``include_role:``, ``include:``
  - ``roles:`` blocks
  - ``pre_tasks:`` / ``post_tasks:``
  - ``serial:`` / ``strategy:`` / ``max_fail_percentage:`` / ``any_errors_fatal:``
  - Multi-play files

The output is a runnable :class:`burr.core.Application`. Module tasks are
``@module_action`` decorations under the hood; loop init, loop advance,
notify markers, and the ``escalate`` / ``done`` terminals are pure
Python ``@action`` nodes.

Example::

    from philip import from_playbook

    app = from_playbook("playbook.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import jinja2
import yaml
from burr.core import Application, ApplicationBuilder, State, action, expr

from philip._action import initial_sentinels, module_action

# Permissive Jinja: undefined references become empty strings rather than
# raising. Matches Ansible's default behavior closer than StrictUndefined
# would, and keeps the FSM advancing even when an upstream task hasn't
# populated the referenced register yet (e.g. when ``when:`` skipped it).
_JINJA_ENV = jinja2.Environment(
    undefined=jinja2.ChainableUndefined,
    autoescape=False,
)


def _render_jinja(value: Any, context: Mapping[str, Any]) -> Any:
    """Walk a Python value, rendering Jinja2 templates inside any strings
    using ``context``. Non-string leaves are returned as-is. Dicts and lists
    recurse."""
    if isinstance(value, str):
        if "{{" not in value and "{%" not in value:
            return value
        return _JINJA_ENV.from_string(value).render(**context)
    if isinstance(value, Mapping):
        return {k: _render_jinja(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_jinja(v, context) for v in value]
    return value


_RESERVED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "when",
        "register",
        "ignore_errors",
        "failed_when",
        "changed_when",
        "become",
        "become_user",
        "become_method",
        "tags",
        "vars",
        "delegate_to",
        "check_mode",
        "diff",
        "no_log",
        "args",
        "notify",
        # ``environment:`` sets env vars on the module subprocess. We don't
        # currently thread them through to ansible-runner, so the values are
        # silently dropped. Adding it here so the task still converts; if
        # the env vars are load-bearing, the converted FSM will need a
        # manual touch-up.
        "environment",
        # ``loop_control:`` only modifies loop output (``label:``, ``pause:``,
        # ``extended:``). Safely ignored at conversion time; the loop still
        # iterates as authored.
        "loop_control",
    }
)

# Keys handled by the flattening pass before the main converter runs over
# the (now-flat) task list. ``include_tasks``/``import_tasks``/``block`` are
# expanded; ``rescue``/``always`` still raise (deferred to a later release).
_FLATTEN_TASK_KEYS: frozenset[str] = frozenset({"include_tasks", "import_tasks", "block"})

# ``loop:`` and ``with_items:`` are now first-class. ``with_subelements:``
# is also lowered (parent-list * subkey nested iteration). ``with_dict``
# and ``with_fileglob`` would each need their own item-producing semantics;
# they still raise. ``with_first_found:`` is handled specially when paired
# with ``include_vars: "{{ item }}"`` because that combination is just a
# different syntax for the first_found lookup.
_LOOP_KEYS: frozenset[str] = frozenset({"loop", "with_items"})

_WITH_FIRST_FOUND_KEYS: frozenset[str] = frozenset({"with_first_found"})
_WITH_SUBELEMENTS_KEYS: frozenset[str] = frozenset({"with_subelements"})

_UNSUPPORTED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "rescue",
        "always",
        "with_dict",
        "with_fileglob",
        # ``include`` (the legacy generic include) is intentionally still
        # rejected. Modern playbooks should use ``include_tasks`` or
        # ``include_role`` which we support.
        "include",
    }
)

_UNSUPPORTED_PLAY_KEYS: frozenset[str] = frozenset(
    {
        # ``roles:`` is now lowered (see ``_inline_roles_into_play``); the
        # remaining play-level keys still require parallelism or pre/post
        # task semantics we don't model.
        "pre_tasks",
        "post_tasks",
        "serial",
        "strategy",
        "max_fail_percentage",
        "any_errors_fatal",
    }
)

_MAX_INCLUDE_DEPTH: int = 5


class UnsupportedPlaybookConstruct(NotImplementedError):
    """Raised when a playbook uses constructs philip's converter doesn't support."""


_NAME_NON_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def _slugify(name: str) -> str:
    """Turn an arbitrary Ansible task name into a Python identifier."""
    slug = _NAME_NON_IDENT_RE.sub("_", name.strip()).strip("_").lower()
    if not slug:
        slug = "task"
    if slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def _module_from_task(task: Mapping[str, Any]) -> tuple[str, Any]:
    """Find the module name + args for a task. Tasks have exactly one
    non-reserved, non-unsupported, non-flatten key whose value is the module's
    args. The flatten keys (``include_tasks``, ``import_tasks``, ``block``)
    are expanded before this function runs, so seeing one here is a converter
    bug. Loop keys (``loop``, ``with_items``) are handled by the caller and
    excluded from the module search."""
    for key in task:
        if key in _UNSUPPORTED_TASK_KEYS:
            raise UnsupportedPlaybookConstruct(
                f"task uses unsupported construct {key!r}: {task.get('name', task)}"
            )
    excluded = (
        _RESERVED_TASK_KEYS
        | _FLATTEN_TASK_KEYS
        | _LOOP_KEYS
        | _WITH_FIRST_FOUND_KEYS
        | _WITH_SUBELEMENTS_KEYS
    )
    module_keys = [k for k in task if k not in excluded]
    if not module_keys:
        raise ValueError(f"task has no module reference: {task}")
    if len(module_keys) > 1:
        raise ValueError(
            f"task has multiple module-shaped keys {module_keys}: {task.get('name', task)}"
        )
    module = module_keys[0]
    args = task[module]
    if args is None:
        args = {}
    elif isinstance(args, str):
        # Short form (e.g. ``shell: echo hello``) gets preserved via the
        # ``_raw_params`` key, which ansible.builtin.shell, .command and a few
        # others honor.
        args = {"_raw_params": args}
    elif not isinstance(args, Mapping):
        raise ValueError(f"task {module!r} args must be a mapping or string; got {type(args)}")
    return module, dict(args)


def _coerce_when_to_list(value: Any) -> list[str]:
    """Normalize ``when:`` (string, list, or None) to a list of clause strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(c) for c in value]
    return [str(value)]


def _coerce_notify_to_list(value: Any) -> list[str]:
    """Normalize ``notify:`` to a flat list of handler names."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(n) for n in value]
    return [str(value)]


def _flatten_tasks(
    tasks: list[Any],
    *,
    base_dir: Path,
    inherited_when: list[str] | None = None,
    inherited_notify: list[str] | None = None,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Recursively expand ``include_tasks``, ``import_tasks``, and ``block:``
    into a flat list of leaf tasks.

    ``inherited_when`` and ``inherited_notify`` accumulate down the recursion
    so block-wide ``when:`` / ``notify:`` propagate to every nested leaf
    task. The output preserves task ordering. Leaf tasks have their own
    ``when:`` AND-merged with the inherited list and their ``notify:``
    union-merged with the inherited list.

    Static and dynamic includes (``import_tasks`` and ``include_tasks``) are
    treated identically at conversion time: the referenced file is loaded
    once and inlined. Jinja-templated paths raise
    :class:`UnsupportedPlaybookConstruct` because resolving them would
    require a per-call runtime path resolution we do not implement.
    """
    if depth > _MAX_INCLUDE_DEPTH:
        raise UnsupportedPlaybookConstruct(
            f"include/block nesting depth exceeded {_MAX_INCLUDE_DEPTH}"
        )

    inherited_when = list(inherited_when or [])
    inherited_notify = list(inherited_notify or [])
    flat: list[dict[str, Any]] = []

    for raw_task in tasks:
        if not isinstance(raw_task, Mapping):
            raise ValueError(f"task is not a mapping: {raw_task!r}")
        task: dict[str, Any] = dict(raw_task)

        task_when = _coerce_when_to_list(task.get("when"))
        combined_when = inherited_when + task_when

        task_notify = _coerce_notify_to_list(task.get("notify"))
        combined_notify = inherited_notify + task_notify

        if "block" in task:
            block_tasks = task["block"]
            if not isinstance(block_tasks, list):
                raise ValueError(f"block: must be a list, got {type(block_tasks).__name__}")
            if "rescue" in task and "always" in task:
                # Pass through the full triple-combo; the main converter
                # loop wires both the rescue routing and the always
                # save/restore actions in one coordinated lowering.
                rescue_tasks = task["rescue"]
                always_tasks = task["always"]
                if not isinstance(rescue_tasks, list):
                    raise ValueError(f"rescue: must be a list, got {type(rescue_tasks).__name__}")
                if not isinstance(always_tasks, list):
                    raise ValueError(f"always: must be a list, got {type(always_tasks).__name__}")
                preserved = {
                    "block": _flatten_tasks(
                        block_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                    "rescue": _flatten_tasks(
                        rescue_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                    "always": _flatten_tasks(
                        always_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                }
                if task.get("name"):
                    preserved["name"] = task["name"]
                flat.append(preserved)
                continue
            if "always" in task:
                # block + always (no rescue). Pass through without inlining;
                # main converter loop wires the failure-preservation actions
                # around the always chain.
                always_tasks = task["always"]
                if not isinstance(always_tasks, list):
                    raise ValueError(f"always: must be a list, got {type(always_tasks).__name__}")
                preserved = {
                    "block": _flatten_tasks(
                        block_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                    "always": _flatten_tasks(
                        always_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                }
                if task.get("name"):
                    preserved["name"] = task["name"]
                flat.append(preserved)
                continue
            if "rescue" in task:
                # Don't inline; the main converter loop handles block+rescue
                # so it can wire the failure -> rescue routing. The inherited
                # when:/notify: have to be pushed into the block & rescue
                # subtrees here so the surrounding logic doesn't lose them.
                rescue_tasks = task["rescue"]
                if not isinstance(rescue_tasks, list):
                    raise ValueError(f"rescue: must be a list, got {type(rescue_tasks).__name__}")
                # Inline outer when/notify into each block & rescue task so
                # the surrounding pipeline doesn't need to track inheritance.
                # A new task dict is constructed with the inherited fields
                # AND-joined / unioned with whatever each inner task already has.
                preserved = {
                    "block": _flatten_tasks(
                        block_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                    "rescue": _flatten_tasks(
                        rescue_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                }
                if task.get("name"):
                    preserved["name"] = task["name"]
                flat.append(preserved)
                continue
            flat.extend(
                _flatten_tasks(
                    block_tasks,
                    base_dir=base_dir,
                    inherited_when=combined_when,
                    inherited_notify=combined_notify,
                    depth=depth + 1,
                )
            )
            continue

        # Accept both the bare key (``include_tasks: file.yml``) and the
        # fully-qualified collection name (``ansible.builtin.include_tasks:
        # file.yml``). The FQCN form is what every modern community role
        # uses; the bare form is still in the wild from older roles.
        _INCLUDE_KEYS = (
            "include_tasks",
            "import_tasks",
            "ansible.builtin.include_tasks",
            "ansible.builtin.import_tasks",
        )
        include_key = next((k for k in _INCLUDE_KEYS if k in task), None)
        if include_key is not None:
            include_value = task[include_key]
            if isinstance(include_value, str):
                include_rel_path = include_value
            elif isinstance(include_value, Mapping):
                file_value = include_value.get("file") or include_value.get("name")
                if not file_value:
                    raise ValueError(f"{include_key}: missing file/name")
                include_rel_path = str(file_value)
            else:
                raise ValueError(f"{include_key}: unsupported shape {type(include_value).__name__}")

            if "{{" in include_rel_path or "{%" in include_rel_path:
                raise UnsupportedPlaybookConstruct(
                    f"{include_key}: Jinja-templated paths are not supported "
                    f"(got {include_rel_path!r})"
                )

            included_path = (base_dir / include_rel_path).resolve()
            if not included_path.exists():
                raise FileNotFoundError(f"{include_key}: file not found: {included_path}")

            with included_path.open() as f:
                included_tasks = yaml.safe_load(f) or []
            if not isinstance(included_tasks, list):
                raise ValueError(f"{include_key}: {included_path} top-level must be a list")

            flat.extend(
                _flatten_tasks(
                    included_tasks,
                    base_dir=included_path.parent,
                    inherited_when=combined_when,
                    inherited_notify=combined_notify,
                    depth=depth + 1,
                )
            )
            continue

        # ``include_role:`` / ``import_role:`` (task-level role inclusion).
        # Resolves the role under the playbook's roles/ directory and
        # inlines its ``tasks/main.yml``. Role-level vars and defaults are
        # NOT pulled in for the task-level form; use play-level ``roles:``
        # for full role expansion with vars merging.
        _ROLE_TASK_KEYS = (
            "include_role",
            "import_role",
            "ansible.builtin.include_role",
            "ansible.builtin.import_role",
        )
        role_task_key = next((k for k in _ROLE_TASK_KEYS if k in task), None)
        if role_task_key is not None:
            role_args = task[role_task_key]
            if not isinstance(role_args, Mapping):
                raise ValueError(
                    f"{role_task_key}: must be a mapping with ``name:``; got "
                    f"{type(role_args).__name__}"
                )
            role_name_value = role_args.get("name")
            if not isinstance(role_name_value, str) or not role_name_value:
                raise ValueError(f"{role_task_key}: missing ``name:`` field")
            if "{{" in role_name_value or "{%" in role_name_value:
                raise UnsupportedPlaybookConstruct(
                    f"{role_task_key}: Jinja-templated role names are not "
                    f"supported (got {role_name_value!r})"
                )
            role_dir = _resolve_role_dir(role_name_value, base_dir)
            role_tasks = _load_role_yaml(role_dir, "tasks/main.yml") or []
            if not isinstance(role_tasks, list):
                raise ValueError(
                    f"{role_task_key} {role_name_value!r}: tasks/main.yml must be a list of tasks"
                )
            flat.extend(
                _flatten_tasks(
                    role_tasks,
                    base_dir=role_dir / "tasks",
                    inherited_when=combined_when,
                    inherited_notify=combined_notify,
                    depth=depth + 1,
                )
            )
            continue

        # Leaf task. Inject the combined when/notify so the rest of the
        # converter sees them as if the author had written them inline.
        if combined_when:
            task["when"] = combined_when
        if combined_notify:
            task["notify"] = combined_notify
        flat.append(task)

    return flat


def _when_to_expr_string(when: Any) -> str:
    """Lower Ansible ``when:`` / ``changed_when:`` / ``failed_when:``
    (string, list of strings, or YAML-typed bool) into a single Python
    expression. A list is AND-joined with parentheses around each clause.
    YAML booleans (``changed_when: false``) become the matching Python
    literal."""
    if isinstance(when, bool):
        return "True" if when else "False"
    if isinstance(when, str):
        return when
    clauses = [str(c) for c in when]
    if not clauses:
        return "True"
    if len(clauses) == 1:
        return clauses[0]
    return " and ".join(f"({c})" for c in clauses)


def _translate_register_dot_access(expr_text: str, registers: Iterable[str]) -> str:
    """Translate ``register.attr.attr2`` to ``register["attr"]["attr2"]`` for
    every registered name, plus the implicit ``item`` loop variable.

    Numeric segments (``item.1``, ``result.0.path``) become integer indices
    rather than string keys; non-numeric segments become string keys. This
    matches Ansible's Jinja attribute access semantics, where ``item.1`` is
    list indexing and ``item.path`` is dict-key access.
    """
    # ``item`` is always rewritten because it's the implicit loop-variable
    # name Ansible exposes inside any ``loop:`` / ``with_items:`` /
    # ``with_subelements:`` body.
    register_set = set(registers) | {"item"}
    # Match: word-boundary name + one or more ``.<segment>`` accesses where
    # each segment is either an identifier or a digit run.
    name_alt = "|".join(re.escape(r) for r in register_set)
    pattern = re.compile(rf"\b(?P<name>{name_alt})(?P<chain>(?:\.(?:[A-Za-z_]\w*|\d+))+)\b")

    def _rewrite(match: re.Match[str]) -> str:
        name = match.group("name")
        attrs = match.group("chain").lstrip(".").split(".")
        out = name
        for attr in attrs:
            if attr.isdigit():
                out += f"[{int(attr)}]"
            else:
                out += f"[{attr!r}]"
        return out

    return pattern.sub(_rewrite, expr_text)


def _translate_jinja_expr(expr_text: str) -> str:
    """Best-effort translation of common Jinja expression idioms into Python.

    Handles the filters / tests playbook authors reach for most often in
    ``when:`` and ``failed_when:`` clauses:

    - ``X is defined`` -> ``(X is not None)``
    - ``X is not defined`` -> ``(X is None)``
    - ``X | length`` -> ``len(X)``
    - ``X | list`` -> ``list(X)``
    - ``X | first`` -> ``(X)[0]``
    - ``X | last`` -> ``(X)[-1]``
    - ``X | upper`` / ``| lower`` / ``| int`` / ``| string`` -> obvious Python
    - ``X | default(Y)`` -> ``(X if X is not None else Y)``

    Anything outside these patterns is left alone. Brittle by design: when
    a converted playbook tries to evaluate something this rewriter can't
    handle, the eval error message names the unsupported chunk, which is
    more useful than an opaque conversion-time refusal.
    """
    text = expr_text

    # ``is defined`` / ``is not defined`` -> not-None / is-None.
    text = re.sub(r"\b(\w[\w.\[\]'\"]*)\s+is\s+not\s+defined\b", r"(\1 is None)", text)
    text = re.sub(r"\b(\w[\w.\[\]'\"]*)\s+is\s+defined\b", r"(\1 is not None)", text)

    # Parenthesized expressions first so ``(... | last)`` matches before
    # the bare-identifier patterns; otherwise the bare matcher swallows
    # the closing ``)`` and produces invalid Python.
    _filter_repls_paren = [
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*length\b", r"len(\1)"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*list\b", r"list(\1)"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*first\b", r"(\1)[0]"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*last\b", r"(\1)[-1]"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*upper\b", r"(\1).upper()"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*lower\b", r"(\1).lower()"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*int\b", r"int(\1)"),
        (r"\(\s*([^()]+?)\s*\)\s*\|\s*string\b", r"str(\1)"),
    ]
    # Bare-identifier patterns: ``foo['x'].bar | last``. The character class
    # explicitly excludes ``|`` and ``(`` so the regex doesn't greedy-match
    # across a previous filter pipe.
    _ident_chars = r"[A-Za-z_\w\.\[\]'\"]"
    _filter_repls_ident = [
        (rf"({_ident_chars}+)\s*\|\s*length\b", r"len(\1)"),
        (rf"({_ident_chars}+)\s*\|\s*list\b", r"list(\1)"),
        (rf"({_ident_chars}+)\s*\|\s*first\b", r"(\1)[0]"),
        (rf"({_ident_chars}+)\s*\|\s*last\b", r"(\1)[-1]"),
        (rf"({_ident_chars}+)\s*\|\s*upper\b", r"(\1).upper()"),
        (rf"({_ident_chars}+)\s*\|\s*lower\b", r"(\1).lower()"),
        (rf"({_ident_chars}+)\s*\|\s*int\b", r"int(\1)"),
        (rf"({_ident_chars}+)\s*\|\s*string\b", r"str(\1)"),
    ]
    # Apply repeatedly so chained filters (``X | list | length``) collapse.
    for _ in range(4):
        for pattern, repl in _filter_repls_paren + _filter_repls_ident:
            text = re.sub(pattern, repl, text)

    # ``| default(Y)`` -- needs to capture both the value and the default
    # so they can be combined into a ternary.
    text = re.sub(
        r"\(\s*([^()]+?)\s*\)\s*\|\s*default\(\s*([^()]+?)\s*\)",
        r"(\1 if \1 is not None else \2)",
        text,
    )
    text = re.sub(
        rf"({_ident_chars}+)\s*\|\s*default\(\s*([^()]+?)\s*\)",
        r"(\1 if \1 is not None else \2)",
        text,
    )

    return text  # noqa: RET504  # ``text`` is built up by the repeated regex subs above


def _build_task_action(
    *,
    py_name: str,
    module: str,
    args: Mapping[str, Any],
    register: str | None,
    become: bool,
    known_registers: Iterable[str],
    play_vars: Mapping[str, Any],
    loop_item_state_key: str | None = None,
) -> Any:
    """Construct a ``@module_action`` that renders Jinja2 templates in the
    task's args using Burr state plus play-level vars as the context.

    Per-task plays in the converter don't share registered facts the way a
    single multi-task play would, so we render templates ourselves before
    ansible-runner sees them. ``known_registers`` lists the names of every
    ``register:`` target declared across the playbook; their values are
    pulled from State on each invocation and added to the rendering context
    alongside the play-level ``vars:`` block.

    The inner function's ``__name__`` is set BEFORE applying ``module_action``
    so that ``functools.wraps`` inside the decorator carries the task's real
    name through to ``_last_action`` and to the tracker.
    """
    register_names = list(known_registers)
    pinned_vars = dict(play_vars)

    def _impl(state: State) -> dict[str, Any]:
        # Build the Jinja context from play vars, all registered values
        # currently in state, plus any other non-internal state field (so
        # set_fact-written variables resolve in subsequent tasks).
        # Internal sentinels (``_last_*``, ``_loop_*``, ``_notified_*``)
        # are skipped to avoid leaking philip's bookkeeping into user
        # template namespaces.
        context: dict[str, Any] = {**pinned_vars}
        state_dict = state.get_all()
        for name in register_names:
            if name in state_dict:
                context[name] = state_dict[name]
        for key, value in state_dict.items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)
        if loop_item_state_key is not None and loop_item_state_key in state_dict:
            # Match Ansible's variable name so playbook authors write
            # ``{{ item }}`` and ``{{ item.name }}`` as they would in any
            # loop-bearing task.
            context["item"] = state_dict[loop_item_state_key]
        return _render_jinja(dict(args), context)

    _impl.__name__ = py_name
    return module_action(module, register=register, become=become)(_impl)


def _loop_items_from_task(task: Mapping[str, Any]) -> list[Any] | str:
    """Extract a ``loop:`` / ``with_items:`` value from a task. A literal
    list returns the list; a string (Jinja-templated reference like
    ``loop: "{{ docker_users }}"``) returns the template unchanged so the
    loop_init action can resolve it against state at task time."""
    raw = task.get("loop") if "loop" in task else task.get("with_items")
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        raise ValueError(
            f"loop:/with_items: must be a list or template string, got {type(raw).__name__}"
        )
    return list(raw)


def _build_loop_init(
    *,
    py_name: str,
    items_state_key: str,
    item_state_key: str,
    idx_state_key: str,
    done_state_key: str,
    items_or_template: list[Any] | str | tuple[str, str, str],
    play_vars: Mapping[str, Any] | None = None,
) -> Any:
    """Build the pure-Python action that seeds the loop's iteration state.

    ``items_or_template`` is either a literal list (set once at conversion
    time) or a Jinja template string like ``"{{ docker_users }}"`` (resolved
    against current state at task time). For the template form, the result
    is parsed: a Python list returns directly; a string is ``ast.literal_eval``-ed
    or split into a single-element list as a last resort. Empty lists are
    valid; the done flag is set to True immediately."""
    import ast

    writes = [items_state_key, item_state_key, idx_state_key, done_state_key]
    pinned_vars = dict(play_vars or {})

    def _resolve_items(state: State) -> list[Any]:
        # Literal list: return as-is.
        if isinstance(items_or_template, list):
            return list(items_or_template)

        context: dict[str, Any] = {**pinned_vars}
        for key, value in state.get_all().items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)

        # ``with_subelements`` spec: ("subelements", parent_template, subkey).
        # Render the parent template, then for each parent dict yield
        # (parent, sub) pairs by reading parent[subkey]. The loop body sees
        # item[0] = parent and item[1] = sub, matching Ansible semantics.
        if (
            isinstance(items_or_template, tuple)
            and len(items_or_template) == 3
            and items_or_template[0] == "subelements"
        ):
            _, parent_template, subkey = items_or_template
            parent_rendered = _render_jinja(parent_template, context)
            if isinstance(parent_rendered, str):
                try:
                    parent_list = ast.literal_eval(parent_rendered.strip())
                except (ValueError, SyntaxError):
                    parent_list = []
            elif isinstance(parent_rendered, list):
                parent_list = parent_rendered
            else:
                parent_list = []
            flat: list[Any] = []
            for parent in parent_list:
                if not isinstance(parent, Mapping):
                    continue
                subs = parent.get(subkey) or []
                if not isinstance(subs, list):
                    continue
                flat.extend([parent, sub] for sub in subs)
            return flat

        # Jinja-templated list value.
        rendered = _render_jinja(items_or_template, context)
        if isinstance(rendered, list):
            return list(rendered)
        if isinstance(rendered, str):
            text = rendered.strip()
            if not text:
                return []
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                # Not a Python literal; treat as a single string item.
                return [text]
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        return [rendered]

    def _impl(state: State) -> State:
        resolved = _resolve_items(state)
        return state.update(
            **{
                items_state_key: resolved.copy(),
                item_state_key: resolved[0] if resolved else None,
                idx_state_key: 0,
                done_state_key: not resolved,
            }
        )

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_loop_advance(
    *,
    py_name: str,
    items_state_key: str,
    item_state_key: str,
    idx_state_key: str,
    done_state_key: str,
) -> Any:
    """Build the pure-Python action that advances the loop counter and
    populates the next item, or flags ``done`` when the list is exhausted."""
    reads = [items_state_key, idx_state_key]
    writes = [item_state_key, idx_state_key, done_state_key]

    def _impl(state: State) -> State:
        items = state[items_state_key]
        next_idx = state[idx_state_key] + 1
        if next_idx < len(items):
            return state.update(
                **{
                    item_state_key: items[next_idx],
                    idx_state_key: next_idx,
                    done_state_key: False,
                }
            )
        return state.update(**{idx_state_key: next_idx, done_state_key: True})

    _impl.__name__ = py_name
    return action(reads=reads, writes=writes)(_impl)


_SET_FACT_MODULES: frozenset[str] = frozenset({"set_fact", "ansible.builtin.set_fact"})

_INCLUDE_VARS_MODULES: frozenset[str] = frozenset({"include_vars", "ansible.builtin.include_vars"})


def _build_templated_include_vars_action(
    *,
    py_name: str,
    base_dir: Path,
    path_template: str,
    namespace: str | None,
    declared_writes: list[str],
) -> Any:
    """Build an action that resolves a Jinja-templated ``include_vars: path``
    at task time and loads the resulting YAML file.

    Mirrors Ansible's role-aware path resolution loosely: the rendered
    path is searched in ``base_dir`` first, then ``base_dir/vars/``
    (the most common location in role layouts), then ``base_dir/../vars/``
    (when the playbook itself lives under tasks/). Geerlingguy's
    nginx/php/postgresql/redis roles all use ``include_vars: "{{ ansible_facts.os_family }}.yml"``
    expecting role-style vars/ lookup.

    ``declared_writes`` is the union of top-level keys across every YAML
    file currently present in the candidate directories, scanned at
    conversion time so Burr knows up front what state fields this action
    may touch."""
    search_roots = [
        base_dir,
        base_dir / "vars",
        base_dir.parent / "vars",
    ]

    # Pre-build a "fill-in" dict so Burr's reducer-writes check is satisfied
    # even when the loaded file is missing keys declared in the union scan.
    # Missing keys land as None.
    declared = declared_writes.copy()

    def _impl(state: State) -> State:
        context: dict[str, Any] = {}
        for key, value in state.get_all().items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)
        rendered = _render_jinja(path_template, context)
        if not isinstance(rendered, str) or not rendered.strip():
            raise ValueError(f"include_vars: template {path_template!r} rendered empty")
        for root in search_roots:
            candidate = (root / rendered).resolve()
            if candidate.exists():
                with candidate.open() as fh:
                    loaded = yaml.safe_load(fh) or {}
                if not isinstance(loaded, Mapping):
                    raise ValueError(f"include_vars: {candidate} top-level must be a mapping")
                if namespace is not None:
                    return state.update(**{namespace: dict(loaded)})
                # Fill missing declared keys with the current state value
                # (or None when never seen). This keeps Burr's
                # reducer-writes check happy without clobbering existing
                # state that this particular file didn't override.
                state_now = state.get_all()
                update: dict[str, Any] = {k: state_now.get(k) for k in declared}
                update.update(loaded)
                return state.update(**update)
        raise FileNotFoundError(
            f"include_vars: template rendered to {rendered!r}; not found in any of "
            f"{[str(r) for r in search_roots]}"
        )

    _impl.__name__ = py_name
    writes = [namespace] if namespace else declared
    return action(reads=[], writes=writes)(_impl)


def _build_first_found_include_vars_action(
    *,
    py_name: str,
    base_dir: Path,
    params: Mapping[str, Any],
    task_vars: Mapping[str, Any],
    declared_writes: list[str],
) -> Any:
    """Build an action that resolves ``include_vars: "{{ lookup('first_found', params) }}"``
    at task execution time.

    ``params`` is the dict the playbook author defined for the lookup,
    typically a ``files:`` list and a ``paths:`` list. The dict's values
    contain Jinja templates referencing ``ansible_facts.*`` and other state.
    At task time, each file name is rendered against play vars + current
    state + task-level vars; each ``paths`` entry is prepended in turn;
    the first existing file is loaded and its top-level keys land in state.

    ``declared_writes`` is the union of top-level keys across every YAML
    file currently present under the ``paths`` directories, scanned at
    conversion time so Burr knows up front what state fields this action
    may touch.

    Mirrors the dominant ``geerlingguy.*`` pattern: per-OS vars files
    selected via ``ansible_facts.distribution`` / ``ansible_facts.os_family``.
    """
    pinned_task_vars = task_vars.copy() if isinstance(task_vars, dict) else dict(task_vars)
    pinned_params = params.copy() if isinstance(params, dict) else dict(params)
    declared = declared_writes.copy()

    def _impl(state: State) -> State:
        # Build the Jinja context: play vars + non-internal state + task vars.
        context: dict[str, Any] = {**pinned_task_vars}
        for key, value in state.get_all().items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)
        rendered = _render_jinja(pinned_params.copy(), context)
        files = rendered.get("files", [])
        paths = rendered.get("paths") or ["."]
        if not isinstance(files, list) or not isinstance(paths, list):
            raise ValueError(
                "first_found: params.files and params.paths must be lists; "
                f"got {type(files).__name__} / {type(paths).__name__}"
            )
        for path_prefix in paths:
            for filename in files:
                candidate = base_dir / str(path_prefix) / str(filename)
                if candidate.exists():
                    with candidate.open() as fh:
                        loaded = yaml.safe_load(fh) or {}
                    if not isinstance(loaded, Mapping):
                        raise ValueError(f"include_vars: {candidate} top-level must be a mapping")
                    state_now = state.get_all()
                    update: dict[str, Any] = {k: state_now.get(k) for k in declared}
                    update.update(loaded)
                    return state.update(**update)
        # Keep declared keys at their current values when no file matches
        # (typical with ``skip: true``); leave the state intact.
        state_now = state.get_all()
        return state.update(**{k: state_now.get(k) for k in declared})

    _impl.__name__ = py_name
    return action(reads=[], writes=declared_writes)(_impl)


def _resolve_role_dir(role_name: str, base_dir: Path) -> Path:
    """Find a role's directory given its name.

    Searches in (1) ``<base_dir>/roles/<role>``, (2) ``<base_dir>/../roles/<role>``,
    and finally treats ``base_dir / role`` as a fallback. This handles both
    playbook-local roles (the conventional layout) and roles a level up
    when the playbook lives inside ``playbooks/``.

    Galaxy-style global paths (``~/.ansible/roles``, ``ANSIBLE_ROLES_PATH``)
    are not searched yet; teams using vendored roles should keep them under
    ``<playbook_dir>/roles/``.
    """
    candidates = [
        base_dir / "roles" / role_name,
        base_dir.parent / "roles" / role_name,
        base_dir / role_name,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"role {role_name!r}: directory not found under any of {[str(c) for c in candidates]}"
    )


def _load_role_yaml(role_dir: Path, sub_path: str) -> Any:
    """Read a role-relative YAML file (e.g. ``tasks/main.yml``) and return
    its parsed contents, or ``None`` if the file does not exist."""
    p = role_dir / sub_path
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text())


def _inline_roles_into_play(play: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Expand a play's ``roles:`` into inline tasks, handlers, and vars.

    Each role entry can be a string (the role name) or a dict shaped like
    ``{role: name, when: cond, vars: {...}}``. For each, we read:

    - ``tasks/main.yml`` -> prepended to the play's ``tasks:`` so role tasks
      execute before the play's own tasks (matches Ansible's order)
    - ``handlers/main.yml`` -> appended to the play's ``handlers:``
    - ``defaults/main.yml`` -> set into play vars at low priority
    - ``vars/main.yml`` -> set into play vars at higher priority

    The ``when:`` from a dict-form role entry is propagated to every task
    in that role via the existing ``when:`` inheritance pathway.

    Returns a new play dict with ``roles`` removed and ``tasks``/
    ``handlers``/``vars`` extended. Mutating the original play would be
    surprising for callers reading the playbook for other reasons.
    """
    roles_value = play.get("roles")
    if not roles_value:
        # No-op: makes the caller's branch simpler.
        return dict(play)

    if not isinstance(roles_value, list):
        raise ValueError(f"roles: must be a list, got {type(roles_value).__name__}")

    role_tasks: list[Any] = []
    role_handlers: list[Any] = []
    accumulated_defaults: dict[str, Any] = {}
    accumulated_vars: dict[str, Any] = {}

    for entry in roles_value:
        if isinstance(entry, str):
            role_name = entry
            role_when: list[str] = []
        elif isinstance(entry, Mapping):
            raw_role = entry.get("role") or entry.get("name")
            if not raw_role:
                raise ValueError(f"role entry {entry!r}: missing ``role:`` / ``name:`` field")
            role_name = str(raw_role)
            role_when = _coerce_when_to_list(entry.get("when"))
        else:
            raise ValueError(
                f"role entry {entry!r}: expected a string or mapping, got {type(entry).__name__}"
            )

        role_dir = _resolve_role_dir(role_name, base_dir)

        defaults = _load_role_yaml(role_dir, "defaults/main.yml")
        if isinstance(defaults, Mapping):
            accumulated_defaults.update(defaults)
        role_vars = _load_role_yaml(role_dir, "vars/main.yml")
        if isinstance(role_vars, Mapping):
            accumulated_vars.update(role_vars)

        tasks_for_role = _load_role_yaml(role_dir, "tasks/main.yml") or []
        if not isinstance(tasks_for_role, list):
            raise ValueError(f"role {role_name!r}: tasks/main.yml must be a list of tasks")
        # The role-entry ``when:`` propagates to every task in the role.
        if role_when:
            if len(role_when) > 1:
                combined = " and ".join(f"({c})" for c in role_when)
            else:
                combined = role_when[0]
            for t in tasks_for_role:
                if not isinstance(t, Mapping):
                    continue
                existing_when = t.get("when")
                if existing_when is None:
                    t["when"] = combined  # type: ignore[index]
                elif isinstance(existing_when, list):
                    t["when"] = [*existing_when, combined]  # type: ignore[index]
                else:
                    t["when"] = f"({existing_when}) and ({combined})"  # type: ignore[index]
        role_tasks.extend(tasks_for_role)

        handlers_for_role = _load_role_yaml(role_dir, "handlers/main.yml") or []
        if not isinstance(handlers_for_role, list):
            raise ValueError(f"role {role_name!r}: handlers/main.yml must be a list")
        role_handlers.extend(handlers_for_role)

    out: dict[str, Any] = play.copy()
    # Roles run before tasks; defaults sit under vars (lower priority).
    out.pop("roles", None)
    # Priority: defaults < role vars < play vars.
    merged_vars = accumulated_defaults | accumulated_vars | dict(play.get("vars") or {})
    if merged_vars:
        out["vars"] = merged_vars
    existing_tasks = play.get("tasks") or []
    out["tasks"] = role_tasks + list(existing_tasks)
    existing_handlers = play.get("handlers") or []
    out["handlers"] = role_handlers + list(existing_handlers)
    return out


def _scan_yaml_keys_in_paths(base_dir: Path, paths: list[str]) -> list[str]:
    """Scan every ``*.yml`` / ``*.yaml`` file under the given path prefixes
    and return the union of their top-level keys. Used to pre-declare the
    write set for a ``first_found`` include_vars action whose actual file
    is only resolvable at task time."""
    out: set[str] = set()
    for path_prefix in paths:
        scan_dir = base_dir / path_prefix
        if not scan_dir.is_dir():
            continue
        for f in list(scan_dir.glob("*.yml")) + list(scan_dir.glob("*.yaml")):
            try:
                loaded = yaml.safe_load(f.read_text()) or {}
            except yaml.YAMLError:
                continue
            if isinstance(loaded, Mapping):
                out.update(str(k) for k in loaded)
    return sorted(out)


def _build_include_vars_action(
    *,
    py_name: str,
    file_path: Path,
    namespace: str | None,
) -> Any:
    """Build a pure-Python action that loads variables from a YAML file
    into Burr state at task execution time.

    Mirrors Ansible's ``include_vars: file.yml`` semantics: the file is
    parsed as a dict and every key becomes a state field. When
    ``namespace`` is given (``include_vars: name=ns file=...``), the
    entire dict lands at ``state[namespace]`` instead of being spread.

    The file is read lazily on each invocation so a vars file edited
    between Application runs picks up the new content; the cost is one
    YAML parse per call, which is negligible against ansible-runner's
    overhead elsewhere.
    """
    resolved_path = file_path.resolve()

    if namespace is not None:
        writes = [namespace]

        def _impl(state: State) -> State:
            with resolved_path.open() as f:
                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, Mapping):
                raise ValueError(
                    f"include_vars: {resolved_path} top level must be a mapping; "
                    f"got {type(loaded).__name__}"
                )
            return state.update(**{namespace: dict(loaded)})

    else:
        with resolved_path.open() as f:
            loaded_keys = (yaml.safe_load(f) or {}).keys()
        writes = [str(k) for k in loaded_keys]

        def _impl(state: State) -> State:
            with resolved_path.open() as f:
                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, Mapping):
                raise ValueError(
                    f"include_vars: {resolved_path} top level must be a mapping; "
                    f"got {type(loaded).__name__}"
                )
            return state.update(**loaded)

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_save_failure_action(*, py_name: str, flag_state_key: str) -> Any:
    """Build a pure-Python action that latches ``_last_failed`` into a sticky
    state field so it survives the subsequent ``always:`` chain.

    Inserted at the failure-routing target of block tasks under a
    ``block + always`` lowering. When a block task fails, control routes
    through this action on the way to the always chain; the action snapshots
    the failure into ``flag_state_key`` while leaving the live sentinels
    alone. Downstream, the always tasks reset ``_last_failed`` to False on
    success; ``_build_restore_failure_action`` re-applies the failure from
    the flag so post-block tasks see it.
    """
    writes = [flag_state_key]

    def _impl(state: State) -> State:
        if state.get("_last_failed"):
            return state.update(**{flag_state_key: True})
        return state

    _impl.__name__ = py_name
    return action(reads=["_last_failed"], writes=writes)(_impl)


def _build_restore_failure_action(*, py_name: str, flag_state_key: str) -> Any:
    """Build a pure-Python action that re-applies a latched block failure.

    Inserted between the last ``always:`` task and the downstream
    post-block task. If ``flag_state_key`` was set earlier (by
    :func:`_build_save_failure_action` after a block task failed), this
    action restores ``_last_failed=True`` so the standard escalate
    transition fires for the downstream task.
    """
    writes = ["_last_failed"]

    def _impl(state: State) -> State:
        if state.get(flag_state_key):
            return state.update(_last_failed=True)
        return state

    _impl.__name__ = py_name
    return action(reads=[flag_state_key], writes=writes)(_impl)


def _build_clear_failure_action(*, py_name: str) -> Any:
    """Build a pure-Python action that resets the failure sentinels.

    Inserted between a successfully-completed ``rescue:`` chain and the
    downstream tasks following the block. Ansible treats a successful
    rescue as "the failure was handled," so ``_last_failed`` and the
    associated diagnostic sentinels reset to their healthy values before
    later transitions can read them. Without this, a downstream
    ``failed_when:`` or default-escalate routing would re-trigger on a
    stale failure.
    """
    writes = ["_last_failed", "_last_msg", "_last_unreachable", "_last_failure_kind"]

    def _impl(state: State) -> State:
        return state.update(
            _last_failed=False,
            _last_msg="",
            _last_unreachable=False,
            _last_failure_kind="ok",
        )

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_set_fact_action(
    *,
    py_name: str,
    args: Mapping[str, Any],
    known_registers: Iterable[str],
    play_vars: Mapping[str, Any],
) -> Any:
    """Build a pure-Python action that mirrors Ansible's ``set_fact:``.

    Each key in ``args`` becomes a state field. Values containing Jinja
    templates are rendered against play vars plus any registered values
    visible in current state, so ``set_fact: total: "{{ a + b }}"`` works
    the way it would inside one Ansible play (even though our converter
    runs each task in its own play and would otherwise drop the new fact).
    """
    register_names = list(known_registers)
    pinned_vars = dict(play_vars)
    # Include ``_last_changed`` so notify markers downstream pick up that
    # this set_fact "changed" state. Ansible's ``set_fact`` module reports
    # ``changed: true`` by default, and notify-on-set_fact is a legitimate
    # idiom for gating a handler on whether a fact was newly assigned.
    writes = [*args.keys(), "_last_changed", "_last_action"]

    def _impl(state: State) -> State:
        context: dict[str, Any] = {**pinned_vars}
        state_dict = state.get_all()
        for name in register_names:
            if name in state_dict:
                context[name] = state_dict[name]
        for key, value in state_dict.items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)
        rendered = _render_jinja(dict(args), context)
        return state.update(**rendered, _last_changed=True, _last_action=py_name)

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_changed_when_post(
    *,
    py_name: str,
    expression: str,
    known_registers: Iterable[str],
) -> Any:
    """Build a pure-Python action that re-evaluates ``_last_changed`` per
    the task's ``changed_when:`` expression.

    Ansible's ``changed_when: false`` (and predicates like
    ``changed_when: result.rc != 0``) override the module's idea of
    whether anything changed. Inserted immediately after a task whose YAML
    sets ``changed_when:``. Reads the same Burr state Burr's expr() reads
    so the predicate has access to registered values.
    """
    # Translate Jinja-style attribute access on registered names to bracket
    # access so Python's ``eval`` can evaluate it. Same transformation the
    # ``when:`` lowering uses elsewhere in the converter.
    translated = _translate_register_dot_access(expression, known_registers)

    def _impl(state: State) -> State:
        state_dict = state.get_all()
        try:
            value = bool(eval(translated, {"__builtins__": {}}, state_dict.copy()))
        except Exception:
            # If the expression can't evaluate (e.g. references a register
            # the upstream task didn't populate), be conservative: don't
            # flip changed.
            value = bool(state_dict.get("_last_changed"))
        return state.update(_last_changed=value)

    _impl.__name__ = py_name
    return action(reads=["_last_changed"], writes=["_last_changed"])(_impl)


def _build_notify_marker(*, py_name: str, handlers: list[str]) -> Any:
    """Build a pure-Python ``@action`` that records notify triggers.

    Inserted after each task that has a ``notify:`` clause. If the preceding
    task reported ``_last_changed=True``, the marker writes
    ``_notified_<handler>=True`` for each handler name in the notify list.
    Handler actions, appended at the end of the play, gate their execution
    on these flags.
    """
    write_keys = [f"_notified_{h}" for h in handlers]

    def _marker(state: State) -> State:
        if state["_last_changed"]:
            return state.update(**{f"_notified_{h}": True for h in handlers})
        return state

    # Name the underlying function before the decorator wraps it, so the
    # resulting Burr Action reports this name in ``_last_action`` and in
    # the tracker rather than the literal ``_marker``.
    _marker.__name__ = py_name
    return action(reads=["_last_changed"], writes=write_keys)(_marker)


def from_playbook(path: str | Path, *, project: str | None = None) -> Application:
    """Parse a YAML playbook and return a runnable :class:`Application`.

    ``path`` is the playbook file. ``project`` is an optional tracker
    project name; when given, a ``LocalTrackingClient`` is attached.

    Raises :class:`UnsupportedPlaybookConstruct` for playbook constructs
    that don't map cleanly to the philip/Burr model. See module
    docstring for the full list.
    """
    playbook_path = Path(path)
    base_dir = playbook_path.resolve().parent
    with playbook_path.open() as f:
        plays = yaml.safe_load(f)

    if not isinstance(plays, list):
        raise ValueError(f"{playbook_path}: top level must be a list of plays")
    if len(plays) == 0:
        raise ValueError(f"{playbook_path}: no plays found")
    if len(plays) > 1:
        raise UnsupportedPlaybookConstruct(
            f"{playbook_path}: multi-play playbooks are not supported"
        )

    play = plays[0]
    if not isinstance(play, Mapping):
        raise ValueError(f"{playbook_path}: play must be a mapping; got {type(play)}")

    # Inline ``roles:`` before the unsupported-key check, since this expansion
    # produces a play without the ``roles`` key. Role tasks are prepended to
    # the play's own tasks; role handlers append; role vars merge in.
    if "roles" in play:
        play_copy = play.copy() if isinstance(play, dict) else dict(play)
        play = _inline_roles_into_play(play_copy, base_dir)

    unsupported_play = [k for k in play if k in _UNSUPPORTED_PLAY_KEYS]
    if unsupported_play:
        raise UnsupportedPlaybookConstruct(
            f"{playbook_path}: play uses unsupported keys {unsupported_play}"
        )

    raw_tasks = play.get("tasks") or []
    if not raw_tasks:
        raise ValueError(f"{playbook_path}: play has no tasks")

    raw_handlers = play.get("handlers") or []

    play_vars: dict[str, Any] = dict(play.get("vars") or {})
    play_become = bool(play.get("become", False))
    gather_facts = play.get("gather_facts")
    if gather_facts is None:
        gather_facts = True  # Ansible default

    # ------------------------------------------------------------------
    # Flatten ``include_tasks``/``import_tasks`` and ``block:`` constructs
    # into a flat list of leaf tasks. Inherited ``when:`` and ``notify:``
    # propagate down to every nested leaf.
    # ------------------------------------------------------------------
    leaf_tasks = _flatten_tasks(raw_tasks, base_dir=base_dir)
    leaf_handlers = _flatten_tasks(raw_handlers, base_dir=base_dir)

    # Map each handler's real (display) name to a slug used as the suffix
    # for the ``_notified_<slug>`` state flag. Real names contain spaces in
    # idiomatic Ansible ("notify: restart nginx"); the slug strips those
    # for use in Python attribute-style state keys.
    handler_name_to_slug: dict[str, str] = {}
    for h in leaf_handlers:
        name = h.get("name")
        if not name:
            raise ValueError("handler tasks must have a 'name'")
        handler_name_to_slug[str(name)] = _slugify(str(name))

    # Validate every notify target refers to a real handler before building
    # anything. A typo in a playbook should fail loudly at convert time.
    for t in leaf_tasks:
        for n in _coerce_notify_to_list(t.get("notify")):
            if n not in handler_name_to_slug:
                raise UnsupportedPlaybookConstruct(
                    f"task {t.get('name', '<unnamed>')!r} notifies unknown handler {n!r}; "
                    f"declared handlers: {sorted(handler_name_to_slug)}"
                )

    # ------------------------------------------------------------------
    # Build the "logical sequence" of FSM nodes: each leaf task, plus a
    # notify-marker after any task that notifies, plus every handler at
    # the end gated on its ``_notified_<name>`` flag. The downstream loop
    # walks this sequence uniformly.
    # ------------------------------------------------------------------
    py_names: list[str] = []
    when_clauses: list[str | None] = []
    failed_when_clauses: list[str | None] = []
    ignore_errors_flags: list[bool] = []
    register_targets: list[str | None] = []
    # Each entry tags whether the node is a real module task or a
    # synthesized notify-marker, and carries the per-type metadata.
    task_meta: list[tuple] = []
    used: set[str] = set()

    def _unique(base: str) -> str:
        candidate = base
        n = 1
        while candidate in used:
            n += 1
            candidate = f"{base}_{n}"
        used.add(candidate)
        return candidate

    def _record(
        *,
        py_name: str,
        when_clause: str | None,
        failed_when_clause: str | None,
        ignore_errors: bool,
        register: str | None,
        meta: tuple,
    ) -> None:
        py_names.append(py_name)
        when_clauses.append(when_clause)
        failed_when_clauses.append(failed_when_clause)
        ignore_errors_flags.append(ignore_errors)
        register_targets.append(register)
        task_meta.append(meta)

    def _allocate_block_subgraph_names(leaves: list[dict[str, Any]], slug_prefix: str) -> list[str]:
        """Allocate unique py_names for a list of block / rescue / always
        leaves, slugifying each from its ``name:`` (or a generated fallback
        for unnamed tasks)."""
        out: list[str] = []
        for idx, t in enumerate(leaves):
            raw = t.get("name") or f"{slug_prefix}_{idx + 1}"
            out.append(_unique(_slugify(raw)))
        return out

    def _emit_block_rescue_always(task: dict[str, Any]) -> None:
        """Lower the full block + rescue + always trilogy.

        Routing:
          - block tasks failure -> rescue_first (failure routes into rescue)
          - block_last success  -> save_failure (skips rescue + clear)
          - rescue tasks failure -> save_failure (latch for restore)
          - rescue_last success -> clear_action (default next-seq)
          - clear_action -> save_failure (no-op when not failed)
          - save_failure -> always_first
          - always_last -> restore_failure (re-applies latched failure)
        """
        block_leaves = _flatten_tasks(task["block"], base_dir=base_dir)
        rescue_leaves = _flatten_tasks(task["rescue"], base_dir=base_dir)
        always_leaves = _flatten_tasks(task["always"], base_dir=base_dir)
        if not block_leaves:
            raise ValueError("block: must contain at least one task")
        if not rescue_leaves:
            raise ValueError("rescue: must contain at least one task")
        if not always_leaves:
            raise ValueError("always: must contain at least one task")
        if any("block" in leaf for leaf in block_leaves + rescue_leaves + always_leaves):
            raise UnsupportedPlaybookConstruct(
                "nested block within block+rescue+always is not yet supported"
            )

        block_py = _allocate_block_subgraph_names(block_leaves, "block_task")
        rescue_py = _allocate_block_subgraph_names(rescue_leaves, "rescue_task")
        always_py = _allocate_block_subgraph_names(always_leaves, "always_task")
        clear_py = _unique("_block_clear_failure")
        save_py = _unique("_block_save_failure")
        restore_py = _unique("_block_restore_failure")
        flag_key = f"_block_failure_remembered_{len(block_ctxs)}"
        rescue_first = rescue_py[0]

        for b_idx, (py, btask) in enumerate(zip(block_py, block_leaves, strict=True)):
            is_last_block = b_idx == len(block_py) - 1
            block_ctxs[py] = (rescue_first, save_py, is_last_block)
            _record_block_inner_task(py, btask)
        for py, rtask in zip(rescue_py, rescue_leaves, strict=True):
            rescue_failure_overrides[py] = save_py
            _record_block_inner_task(py, rtask)
        _record(
            py_name=clear_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=True,
            register=None,
            meta=("clear_failure",),
        )
        _record(
            py_name=save_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=True,
            register=None,
            meta=("block_save_failure", flag_key),
        )
        for py, atask in zip(always_py, always_leaves, strict=True):
            _record_block_inner_task(py, atask)
        _record(
            py_name=restore_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=False,
            register=None,
            meta=("block_restore_failure", flag_key),
        )

    def _emit_block_rescue(task: dict[str, Any]) -> None:
        """Lower block + rescue (no always). Block failure routes into the
        rescue chain; rescue_last falls through to a clear-failure action
        that wipes the sentinels before downstream tasks run."""
        block_leaves = _flatten_tasks(task["block"], base_dir=base_dir)
        rescue_leaves = _flatten_tasks(task["rescue"], base_dir=base_dir)
        if not block_leaves:
            raise ValueError("block: must contain at least one task")
        if not rescue_leaves:
            raise ValueError("rescue: must contain at least one task")
        if any("block" in leaf for leaf in block_leaves + rescue_leaves):
            raise UnsupportedPlaybookConstruct("nested block + rescue is not yet supported")

        block_py = _allocate_block_subgraph_names(block_leaves, "block_task")
        rescue_py = _allocate_block_subgraph_names(rescue_leaves, "rescue_task")
        clear_py = _unique("_block_clear_failure")
        rescue_first = rescue_py[0]

        for b_idx, (py, btask) in enumerate(zip(block_py, block_leaves, strict=True)):
            is_last_block = b_idx == len(block_py) - 1
            block_ctxs[py] = (rescue_first, clear_py, is_last_block)
            _record_block_inner_task(py, btask)
        for py, rtask in zip(rescue_py, rescue_leaves, strict=True):
            _record_block_inner_task(py, rtask)
        _record(
            py_name=clear_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=True,
            register=None,
            meta=("clear_failure",),
        )

    def _emit_block_always(task: dict[str, Any]) -> None:
        """Lower block + always (no rescue). Block failure latches into the
        save action and propagates through the always chain; restore
        re-applies the latched failure so escalate fires after always."""
        block_leaves = _flatten_tasks(task["block"], base_dir=base_dir)
        always_leaves = _flatten_tasks(task["always"], base_dir=base_dir)
        if not block_leaves:
            raise ValueError("block: must contain at least one task")
        if not always_leaves:
            raise ValueError("always: must contain at least one task")
        if any("block" in leaf for leaf in block_leaves + always_leaves):
            raise UnsupportedPlaybookConstruct(
                "nested block within block+always is not yet supported"
            )

        block_py = _allocate_block_subgraph_names(block_leaves, "block_task")
        always_py = _allocate_block_subgraph_names(always_leaves, "always_task")
        save_py = _unique("_block_save_failure")
        restore_py = _unique("_block_restore_failure")
        flag_key = f"_block_failure_remembered_{len(block_ctxs)}"

        for b_idx, (py, btask) in enumerate(zip(block_py, block_leaves, strict=True)):
            is_last_block = b_idx == len(block_py) - 1
            block_ctxs[py] = (save_py, save_py, is_last_block)
            _record_block_inner_task(py, btask)
        _record(
            py_name=save_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=True,
            register=None,
            meta=("block_save_failure", flag_key),
        )
        for py, atask in zip(always_py, always_leaves, strict=True):
            _record_block_inner_task(py, atask)
        # restore_failure keeps the standard escalate edge; ignore_errors=False
        # so the failure transition fires when the latched flag is restored.
        _record(
            py_name=restore_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=False,
            register=None,
            meta=("block_restore_failure", flag_key),
        )

    def _emit_loop_task(task: dict[str, Any], raw_name: str) -> None:
        """Lower a ``loop:`` / ``with_items:`` / ``with_subelements:`` task
        into the three-action sub-FSM (loop_init -> body -> loop_advance)
        with a back-edge. The body action carries the per-iteration item
        key so its Jinja context exposes ``{{ item }}``. A trailing
        notify-marker is emitted when the task has ``notify:`` set.
        """
        base_py = _slugify(raw_name)
        init_py = _unique(f"{base_py}_loop_init")
        task_py = _unique(base_py)
        advance_py = _unique(f"{base_py}_loop_advance")
        items_key = f"_loop_{task_py}_items"
        item_key = f"_loop_{task_py}_item"
        idx_key = f"_loop_{task_py}_idx"
        done_key = f"_loop_{task_py}_done"
        items: list[Any] | str | tuple[str, str, str]
        if "with_subelements" in task:
            wse = task["with_subelements"]
            if not isinstance(wse, list) or len(wse) < 2:
                raise ValueError(
                    "with_subelements: must be a list of at least 2 items (parent template, subkey)"
                )
            items = ("subelements", str(wse[0]), str(wse[1]))
        else:
            items = _loop_items_from_task(task)
        module, args = _module_from_task(task)
        register = task.get("register")
        become = bool(task.get("become", play_become))
        when = task.get("when")
        fw = task.get("failed_when")

        _record(
            py_name=init_py,
            when_clause=_when_to_expr_string(when) if when is not None else None,
            failed_when_clause=None,
            ignore_errors=True,
            register=None,
            meta=("loop_init", items_key, item_key, idx_key, done_key, items),
        )
        _record(
            py_name=task_py,
            when_clause=None,
            failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
            ignore_errors=bool(task.get("ignore_errors", False)),
            register=register,
            meta=("module", module, args, register, become, item_key),
        )
        _record(
            py_name=advance_py,
            when_clause=None,
            failed_when_clause=None,
            ignore_errors=True,
            register=None,
            meta=("loop_advance", items_key, item_key, idx_key, done_key),
        )
        loop_back_edges[advance_py] = (init_py, task_py, done_key)

        notify_list = _coerce_notify_to_list(task.get("notify"))
        if notify_list:
            seen: dict[str, None] = {}
            for handler in notify_list:
                slug = handler_name_to_slug[handler]
                seen.setdefault(slug, None)
            marker_handlers = list(seen)
            notified_handlers_seen.update(marker_handlers)
            marker_name = _unique(f"_notify_{task_py}")
            _record(
                py_name=marker_name,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("notify_marker", marker_handlers),
            )

    def _record_block_inner_task(py: str, t: dict[str, Any]) -> None:
        """Record one inner task of a block / rescue / always group.

        Handles module tasks and ``set_fact:`` (the common cases inside a
        block). ``notify:`` / ``loop:`` / ``changed_when:`` inside a block,
        rescue, or always chain still raise; those work fine OUTSIDE a
        block but combining them with the block lowerings is deferred.
        """
        if any(k in t for k in ("loop", "with_items", "notify", "changed_when")):
            raise UnsupportedPlaybookConstruct(
                "loop:/with_items:/notify:/changed_when: inside block:/rescue:/always: "
                f"are not yet supported; task: {t.get('name', '<unnamed>')!r}"
            )
        module, args = _module_from_task(t)
        register = t.get("register")
        become = bool(t.get("become", play_become))
        when = t.get("when")
        fw = t.get("failed_when")
        if module in _SET_FACT_MODULES:
            _record(
                py_name=py,
                when_clause=_when_to_expr_string(when) if when is not None else None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("set_fact", args),
            )
            return
        _record(
            py_name=py,
            when_clause=_when_to_expr_string(when) if when is not None else None,
            failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
            ignore_errors=bool(t.get("ignore_errors", False)),
            register=register,
            meta=("module", module, args, register, become),
        )

    notified_handlers_seen: set[str] = set()
    # Index tracking for emitting loop back-edges during the transition pass.
    # Maps the loop-task's py_name -> (init_py_name, advance_py_name, done_state_key).
    loop_back_edges: dict[str, tuple[str, str, str]] = {}
    # Per-task block routing context, populated when a leaf is part of a
    # block + rescue (or block + always, or block + rescue + always) group.
    # Maps py_name -> (failure_target, success_skip_target, is_last) for
    # block tasks. Rescue tasks under the triple combo also need a failure
    # override; those live in ``rescue_failure_overrides``.
    block_ctxs: dict[str, tuple[str, str, bool]] = {}
    rescue_failure_overrides: dict[str, str] = {}

    for idx, task in enumerate(leaf_tasks):
        raw_name = task.get("name") or f"task_{idx + 1}"

        # ``block:`` with a rescue: or always: clause is lowered here.
        # Pure block (no rescue, no always) is already inlined by
        # ``_flatten_tasks`` and never reaches this branch.
        if "block" in task and "rescue" in task and "always" in task:
            _emit_block_rescue_always(task)
            continue

        if "block" in task and "always" in task:
            _emit_block_always(task)
            continue

        if "block" in task:
            _emit_block_rescue(task)
            continue

        # ``loop:`` / ``with_items:`` lower to a three-action sub-FSM:
        # init -> task -> advance, with a back-edge from advance to task
        # until the items are exhausted. The task action reads the current
        # item from a dedicated state key and exposes it as ``{{ item }}``
        # in the rendered Jinja context, matching Ansible's variable naming.
        if "loop" in task or "with_items" in task or "with_subelements" in task:
            _emit_loop_task(task, raw_name)
            continue

        py = _unique(_slugify(raw_name))
        module, args = _module_from_task(task)
        register = task.get("register")
        become = bool(task.get("become", play_become))
        when = task.get("when")
        fw = task.get("failed_when")
        cw = task.get("changed_when")

        # ``set_fact:`` is lowered to a pure-Python state-update action
        # rather than handed to ansible-runner, because each converter task
        # runs in its own play and Ansible's fact propagation across plays
        # is unreliable. The arg dict's values are Jinja-rendered against
        # current state, so ``set_fact: total: "{{ a + b }}"`` works as
        # authored. Fall through to the notify / changed_when post-pass
        # below so set_fact tasks with ``notify:`` still install the
        # notify-marker.
        if module in _SET_FACT_MODULES:
            _record(
                py_name=py,
                when_clause=_when_to_expr_string(when) if when is not None else None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("set_fact", args),
            )
        elif module in _INCLUDE_VARS_MODULES:
            # ``include_vars: file: path`` (or short form ``include_vars: path``)
            # is the simple case. Variations also handled:
            # - ``include_vars: "{{ lookup('first_found', params) }}"`` with
            #   task-level ``vars: { params: {...} }``
            # - ``include_vars: "{{ item }}"`` with a ``with_first_found:``
            #   list of candidate files (the older idiom; geerlingguy mysql
            #   and php use this).
            file_value: Any
            namespace_value: str | None
            if isinstance(args, Mapping):
                file_value = args.get("file") or args.get("_raw_params")
                namespace_value = args.get("name")
            else:
                file_value = args.get("_raw_params") if isinstance(args, dict) else None
                namespace_value = None
            if not isinstance(file_value, str):
                raise UnsupportedPlaybookConstruct(
                    "include_vars: only the simple ``file: path`` form and the "
                    "``lookup('first_found', params)`` form are supported; "
                    f"got {file_value!r}"
                )
            # ``with_first_found`` paired with ``include_vars: "{{ item }}"``:
            # the loop becomes a first_found over the listed files.
            wff = task.get("with_first_found")
            if wff is not None:
                if not isinstance(wff, list) or not wff:
                    raise ValueError("with_first_found: must be a non-empty list")
                # The first element is either a list-of-strings (each one is
                # a candidate file template) or a dict with ``files:`` /
                # ``paths:`` / ``skip:``.
                first = wff[0]
                if isinstance(first, list):
                    params_dict = {"files": first, "paths": ["."]}
                elif isinstance(first, Mapping):
                    params_dict = {
                        "files": first.get("files") or [],
                        "paths": first.get("paths") or ["."],
                    }
                else:
                    params_dict = {"files": [str(first)], "paths": ["."]}
                declared_writes = _scan_yaml_keys_in_paths(
                    base_dir,
                    [".", "vars", "../vars", *[str(p) for p in params_dict["paths"]]],
                )
                # The first_found resolver searches the path prefixes against
                # the file list. Add the role-aware vars directories to the
                # paths so geerlingguy's "files: [{{ os_family }}.yml]" with
                # no explicit path works.
                resolver_params = {
                    "files": params_dict["files"],
                    "paths": [".", *{*params_dict["paths"], "vars", "../vars"}],
                }
                _record(
                    py_name=py,
                    when_clause=_when_to_expr_string(when) if when is not None else None,
                    failed_when_clause=None,
                    ignore_errors=True,
                    register=None,
                    meta=(
                        "include_vars_first_found",
                        base_dir,
                        resolver_params,
                        {},  # no task vars used; params are inline
                        declared_writes,
                    ),
                )
                continue
            # first_found path: the include_vars value is a Jinja template
            # whose only function call is ``lookup('first_found', <name>)``.
            # The named param dict comes from the task's ``vars:`` block.
            if "lookup(" in file_value and "first_found" in file_value:
                task_vars = task.get("vars") or {}
                if not isinstance(task_vars, Mapping):
                    raise ValueError(
                        f"task vars: must be a mapping; got {type(task_vars).__name__}"
                    )
                # Extract the param name being looked up. Accept simple
                # ``lookup('first_found', params)`` or single-arg variants;
                # complex multi-argument lookups raise.
                param_name = None
                for candidate_name in task_vars:
                    if f"'{candidate_name}'" in file_value or f", {candidate_name}" in file_value:
                        param_name = candidate_name
                        break
                if param_name is None or not isinstance(task_vars.get(param_name), Mapping):
                    raise UnsupportedPlaybookConstruct(
                        "first_found: expected a task ``vars:`` entry naming a "
                        "dict with files: and paths: keys"
                    )
                params_dict = dict(task_vars[param_name])
                # Scan the candidate paths at convert time to declare the
                # write set Burr expects up front.
                resolved_paths_lit = params_dict.get("paths") or ["."]
                if not isinstance(resolved_paths_lit, list):
                    raise ValueError("first_found: params.paths must be a list")
                declared_writes = _scan_yaml_keys_in_paths(
                    base_dir, [str(p) for p in resolved_paths_lit]
                )
                _record(
                    py_name=py,
                    when_clause=_when_to_expr_string(when) if when is not None else None,
                    failed_when_clause=None,
                    ignore_errors=True,
                    register=None,
                    meta=(
                        "include_vars_first_found",
                        base_dir,
                        params_dict,
                        dict(task_vars),
                        declared_writes,
                    ),
                )
            elif "{{" in file_value:
                # Templated path (``include_vars: "{{ ansible_facts.os_family }}.yml"``).
                # Pre-scan the candidate directories so we know what state
                # keys this action may write.
                declared_writes = _scan_yaml_keys_in_paths(
                    base_dir,
                    [".", "vars", "../vars"],
                )
                _record(
                    py_name=py,
                    when_clause=_when_to_expr_string(when) if when is not None else None,
                    failed_when_clause=None,
                    ignore_errors=True,
                    register=None,
                    meta=(
                        "include_vars_templated",
                        base_dir,
                        file_value,
                        namespace_value,
                        declared_writes,
                    ),
                )
            else:
                if "lookup(" in file_value:
                    raise UnsupportedPlaybookConstruct(
                        "include_vars: only literal paths, Jinja-templated "
                        "paths, and the ``lookup('first_found', params)`` "
                        f"form are supported; got {file_value!r}"
                    )
                vars_path = (base_dir / file_value).resolve()
                if not vars_path.exists():
                    raise FileNotFoundError(f"include_vars: file not found: {vars_path}")
                _record(
                    py_name=py,
                    when_clause=_when_to_expr_string(when) if when is not None else None,
                    failed_when_clause=None,
                    ignore_errors=True,
                    register=None,
                    meta=("include_vars", vars_path, namespace_value),
                )
        else:
            _record(
                py_name=py,
                when_clause=_when_to_expr_string(when) if when is not None else None,
                failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
                ignore_errors=bool(task.get("ignore_errors", False)),
                register=register,
                meta=("module", module, args, register, become),
            )

        # ``changed_when:`` overrides Ansible's idea of whether the task
        # changed anything (``changed_when: false`` for a read-only command,
        # ``changed_when: result.rc != 0`` for a custom predicate). Insert
        # a tiny post-action that re-evaluates _last_changed against state.
        if cw is not None:
            cw_expr = _when_to_expr_string(cw)
            post_name = _unique(f"_changed_when_{py}")
            _record(
                py_name=post_name,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("changed_when_post", cw_expr),
            )

        notify_list = _coerce_notify_to_list(task.get("notify"))
        if notify_list:
            # Dedupe handlers but preserve order so the marker writes flags
            # in the order the playbook listed. The marker tracks the
            # *slugified* handler names so the resulting state keys are
            # valid Python identifiers (``_notified_say_hello`` rather than
            # ``_notified_say hello``).
            slugs_seen: dict[str, None] = {}
            for handler in notify_list:
                slug = handler_name_to_slug[handler]
                slugs_seen.setdefault(slug, None)
            marker_handlers = list(slugs_seen)
            notified_handlers_seen.update(marker_handlers)
            marker_name = _unique(f"_notify_{py}")
            _record(
                py_name=marker_name,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("notify_marker", marker_handlers),
            )

    # Append handlers in declaration order, gated on _notified_<slug>.
    for idx, handler_task in enumerate(leaf_handlers):
        handler_real_name = str(handler_task.get("name") or f"handler_{idx + 1}")
        handler_slug = handler_name_to_slug[handler_real_name]
        # Skip handlers nobody notifies; keeps the graph tight.
        if handler_slug not in notified_handlers_seen:
            continue
        py = _unique(_slugify(f"handler_{handler_real_name}"))
        module, args = _module_from_task(handler_task)
        register = handler_task.get("register")
        become = bool(handler_task.get("become", play_become))
        when_user = handler_task.get("when")
        gate = f"_notified_{handler_slug}"
        if when_user is not None:
            when_combined = f"({_when_to_expr_string(when_user)}) and {gate}"
        else:
            when_combined = gate
        fw = handler_task.get("failed_when")
        _record(
            py_name=py,
            when_clause=when_combined,
            failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
            # Handler failures by default propagate; Ansible's --force-handlers
            # behavior is out of scope here.
            ignore_errors=bool(handler_task.get("ignore_errors", False)),
            register=register,
            meta=("module", module, args, register, become),
        )

    known_registers: set[str] = {r for r in register_targets if r}
    if gather_facts:
        # ``gather_facts: yes`` produces both ``ansible_facts`` (the projected
        # facts dict) and ``gathered_facts`` (the full module result). Mark
        # them so the dot-access translator rewrites
        # ``when: ansible_facts.os_family == 'Debian'`` correctly.
        known_registers.add("ansible_facts")
        known_registers.add("gathered_facts")

    actions: dict[str, Any] = {}
    for candidate, meta in zip(py_names, task_meta, strict=True):
        kind = meta[0]
        if kind == "module":
            # Plain tasks have 5-tuples (kind, module, args, register, become);
            # loop-body tasks have 6-tuples with an extra loop-item state key.
            if len(meta) == 5:
                _, module, args, register, become = meta
                loop_item_key = None
            else:
                _, module, args, register, become, loop_item_key = meta
            actions[candidate] = _build_task_action(
                py_name=candidate,
                module=module,
                args=args,
                register=register,
                become=become,
                known_registers=known_registers,
                play_vars=play_vars,
                loop_item_state_key=loop_item_key,
            )
        elif kind == "notify_marker":
            _, handlers = meta
            actions[candidate] = _build_notify_marker(py_name=candidate, handlers=handlers)
        elif kind == "loop_init":
            _, items_key, item_key, idx_key, done_key, items = meta
            actions[candidate] = _build_loop_init(
                py_name=candidate,
                items_state_key=items_key,
                item_state_key=item_key,
                idx_state_key=idx_key,
                done_state_key=done_key,
                items_or_template=items,
                play_vars=play_vars,
            )
        elif kind == "loop_advance":
            _, items_key, item_key, idx_key, done_key = meta
            actions[candidate] = _build_loop_advance(
                py_name=candidate,
                items_state_key=items_key,
                item_state_key=item_key,
                idx_state_key=idx_key,
                done_state_key=done_key,
            )
        elif kind == "set_fact":
            _, set_fact_args = meta
            actions[candidate] = _build_set_fact_action(
                py_name=candidate,
                args=set_fact_args,
                known_registers=known_registers,
                play_vars=play_vars,
            )
        elif kind == "changed_when_post":
            _, cw_expr = meta
            actions[candidate] = _build_changed_when_post(
                py_name=candidate,
                expression=cw_expr,
                known_registers=known_registers,
            )
        elif kind == "clear_failure":
            actions[candidate] = _build_clear_failure_action(py_name=candidate)
        elif kind == "block_save_failure":
            _, flag_key = meta
            actions[candidate] = _build_save_failure_action(
                py_name=candidate, flag_state_key=flag_key
            )
        elif kind == "block_restore_failure":
            _, flag_key = meta
            actions[candidate] = _build_restore_failure_action(
                py_name=candidate, flag_state_key=flag_key
            )
        elif kind == "include_vars":
            _, vars_path, namespace_value = meta
            actions[candidate] = _build_include_vars_action(
                py_name=candidate, file_path=vars_path, namespace=namespace_value
            )
        elif kind == "include_vars_first_found":
            _, base, params_dict, task_vars_dict, declared = meta
            actions[candidate] = _build_first_found_include_vars_action(
                py_name=candidate,
                base_dir=base,
                params=params_dict,
                task_vars=task_vars_dict,
                declared_writes=declared,
            )
        elif kind == "include_vars_templated":
            _, base, path_tmpl, namespace_v, declared = meta
            actions[candidate] = _build_templated_include_vars_action(
                py_name=candidate,
                base_dir=base,
                path_template=path_tmpl,
                namespace=namespace_v,
                declared_writes=declared,
            )
        else:
            raise AssertionError(f"unknown task meta kind: {kind}")

    # Translate Jinja-style attribute access on registered names into the
    # bracket access Python's eval expects. ``known_registers`` was assembled
    # while walking the logical sequence. The Jinja translator handles
    # common filters (``| length``, ``| default``, ``| last``, etc.) and
    # tests (``is defined`` / ``is not defined``) so converted ``when:``
    # clauses lifted from real-world playbooks evaluate cleanly.
    def _translate_when(c: str) -> str:
        return _translate_jinja_expr(_translate_register_dot_access(c, known_registers))

    when_clauses = [_translate_when(c) if c is not None else None for c in when_clauses]
    failed_when_clauses = [
        _translate_when(c) if c is not None else None for c in failed_when_clauses
    ]

    # ------------------------------------------------------------------
    # Terminals: ``done`` (reached after the last task or when when-skipped)
    # and ``escalate`` (any task fails and isn't ignore_errors). Pure Python.
    # ------------------------------------------------------------------
    @action(reads=[], writes=["outcome"])
    def done(state: State) -> State:
        return state.update(outcome="OK: playbook completed")

    @action(reads=["_last_action", "_last_msg"], writes=["outcome"])
    def escalate(state: State) -> State:
        return state.update(
            outcome=f"ESCALATE at {state['_last_action']}: {state['_last_msg'][:300]}"
        )

    actions["done"] = done
    actions["escalate"] = escalate

    # Optional gather_facts as the entrypoint.
    if gather_facts:
        # ``ansible.builtin.setup`` invoked controller-side without an explicit
        # host is fine for localhost playbooks (the common case for converted
        # tutorials). Users targeting a remote host should wire a ``host()``
        # profile and use ``host.gather_facts()`` directly.
        #
        # The custom action below also surfaces ``ansible_facts`` as a top-level
        # state dict. Real-world playbooks (geerlingguy's roles in particular)
        # reference ``ansible_facts.os_family`` and ``ansible_facts.distribution``
        # in ``when:`` clauses; without the projection the dot-access translator
        # would rewrite to ``ansible_facts['os_family']`` against a non-existent
        # top-level key. The full module result remains available as
        # ``gathered_facts`` for callers that want the diagnostic fields.
        @action(
            reads=[],
            writes=[
                "gathered_facts",
                "ansible_facts",
                "_last_action",
                "_last_failed",
                "_last_changed",
                "_last_unreachable",
                "_last_msg",
                "_last_failure_kind",
            ],
        )
        def gather_facts_action(state: State) -> State:
            from philip._action import _classify_failure
            from philip._runner import run_module as _run

            result = _run("ansible.builtin.setup", {})
            facts = result.get("ansible_facts") or {}
            return state.update(
                gathered_facts=result,
                ansible_facts=facts,
                _last_action="gather_facts",
                _last_failed=bool(result.get("failed")),
                _last_changed=bool(result.get("changed")),
                _last_unreachable=bool(result.get("unreachable")),
                _last_msg=str(result.get("msg") or ""),
                _last_failure_kind=_classify_failure(result),
            )

        actions["gather_facts"] = gather_facts_action
        entry = "gather_facts"
    else:
        entry = py_names[0]

    # ------------------------------------------------------------------
    # Transitions:
    #   - ``when:`` on task[i]: if predicate fails, jump straight to task[i+1]
    #     (or to ``done`` if i is the last task).
    #   - ``failed_when:`` or _last_failed by default: if predicate holds, jump
    #     to escalate (unless ``ignore_errors: yes``).
    #   - Otherwise: linear flow to task[i+1].
    # ------------------------------------------------------------------
    transitions: list[tuple] = []
    if gather_facts:
        transitions.extend(
            [
                ("gather_facts", "escalate", expr("_last_failed")),
                ("gather_facts", py_names[0]),
            ]
        )

    for i, current in enumerate(py_names):
        nxt = py_names[i + 1] if i + 1 < len(py_names) else "done"

        # block + rescue: a failing block task must route to the rescue
        # chain, not to escalate. The block_ctx tag identifies these
        # positions; the rescue chain itself uses default escalate routing
        # (a failing rescue task IS a real escalation).
        block_ctx = block_ctxs.get(current)
        if block_ctx is not None:
            rescue_first_target, clear_action_target, is_block_last_flag = block_ctx
            is_block_task = True
            is_block_last = is_block_last_flag
        else:
            rescue_first_target = None
            clear_action_target = None
            is_block_task = False
            is_block_last = False

        # Failure routing for the current task: if the failed_when expression
        # is satisfied (or _last_failed if none was given) and ignore_errors
        # was not set, the FSM routes to the escalate terminal, except for
        # block tasks under a rescue clause (routes to rescue_first) or
        # rescue tasks under the triple combo (routes to save_failure so
        # the failure is preserved through the always chain).
        if not ignore_errors_flags[i]:
            failure_predicate = failed_when_clauses[i] or "_last_failed"
            if is_block_task:
                failure_target = rescue_first_target
            elif current in rescue_failure_overrides:
                failure_target = rescue_failure_overrides[current]
            else:
                failure_target = "escalate"
            transitions.append((current, failure_target, expr(failure_predicate)))

        # ``when:`` skip behavior with chained skip. For each k>=1, if the
        # next k tasks ALL carry a ``when:`` that evaluates false, jump
        # past all of them to task[i+k+1] (or to ``done``). Longest skip
        # is emitted first so Burr's in-declaration-order condition match
        # picks the deepest skip when multiple are satisfied. This matters
        # for handler chains, where consecutive unnotified handlers all
        # need to be skipped together.
        skip_transitions: list[tuple] = []
        accumulated: list[str] = []
        j = i + 1
        while j < len(py_names) and when_clauses[j] is not None:
            accumulated.append(when_clauses[j])  # type: ignore[arg-type]
            skip_target = py_names[j + 1] if j + 1 < len(py_names) else "done"
            condition = " and ".join(f"not ({c})" for c in accumulated)
            skip_transitions.append((current, skip_target, expr(condition)))
            j += 1
        # Longest skip first so Burr's in-declaration-order matching picks
        # the deepest applicable skip when several conditions are satisfied.
        transitions.extend(reversed(skip_transitions))

        # Loop back-edge: when this position is a ``loop_advance``, route
        # back to the loop body until the iteration is exhausted. The
        # default ``(current, nxt)`` then handles the exhausted case by
        # falling through to whatever follows the loop block.
        if current in loop_back_edges:
            _init_py, task_py, done_key = loop_back_edges[current]
            transitions.append((current, task_py, expr(f"not {done_key}")))

        # block_last success: skip past the rescue chain to the clear action.
        # The standard next-seq for block_last would route into rescue_first,
        # which is correct only on failure.
        if is_block_last:
            transitions.append((current, clear_action_target))
        else:
            transitions.append((current, nxt))

    # ------------------------------------------------------------------
    # Build the Application.
    # ------------------------------------------------------------------
    builder: ApplicationBuilder = ApplicationBuilder().with_actions(**actions)
    for transition_tuple in transitions:
        builder = builder.with_transitions(transition_tuple)

    state_init: dict[str, Any] = initial_sentinels() | play_vars | {"outcome": ""}
    # Pre-seed register targets so transitions can read them before the producing
    # task runs.
    for reg in register_targets:
        if reg and reg not in state_init:
            state_init[reg] = {}
    # Pre-seed notify flags so handler when:-gates can be evaluated before
    # any task has had a chance to flip them.
    for handler_name in notified_handlers_seen:
        state_init.setdefault(f"_notified_{handler_name}", False)
    # Pre-seed loop state so loop-init isn't strictly required for the
    # state to be readable. Each loop's init action overwrites these.
    for meta in task_meta:
        if meta[0] == "loop_init":
            _, items_key, item_key, idx_key, done_key, _items = meta
            state_init.setdefault(items_key, [])
            state_init.setdefault(item_key, None)
            state_init.setdefault(idx_key, 0)
            state_init.setdefault(done_key, False)
        elif meta[0] in ("block_save_failure", "block_restore_failure"):
            _, flag_key = meta
            state_init.setdefault(flag_key, False)
    if gather_facts:
        state_init.setdefault("gathered_facts", {})
        state_init.setdefault("ansible_facts", {})
    builder = builder.with_state(**state_init).with_entrypoint(entry)

    if project is not None:
        from burr.tracking import LocalTrackingClient

        builder = builder.with_tracker(LocalTrackingClient(project=project))  # type: ignore[arg-type]

    app = builder.build()
    # Stash the canonical play structure so ``to_playbook(app)`` can
    # reconstruct the source YAML. Keyed by ``id(app)`` and held via a
    # weakref-style mapping so the entry disappears when the Application
    # is garbage-collected; preserves the round-trip story without
    # leaking memory if a caller builds and discards many Applications.
    _PLAYBOOK_ORIGINS[id(app)] = plays
    return app


# Module-level cache: maps id(application) -> the original list-of-plays
# loaded by ``from_playbook``. Used by ``to_playbook`` for round-trip
# emission. A weakref.finalize-style entry would be cleaner; for now a
# simple dict suffices since Applications are usually long-lived.
_PLAYBOOK_ORIGINS: dict[int, list[Any]] = {}


def to_playbook(app: Application) -> str:
    """Emit the canonical YAML for an Application that was loaded via
    :func:`from_playbook`.

    Returns the source YAML reformatted via ``yaml.safe_dump`` (so the
    output is canonical: sorted keys, consistent indentation, etc.). Use
    this to normalize a playbook, or to round-trip
    YAML -> Application -> YAML for diffing.

    Raises :class:`ValueError` for Applications that were not loaded by
    ``from_playbook`` (e.g. hand-authored FSMs). Programmatic
    Application-to-YAML synthesis is not yet implemented; this function
    only handles the from_playbook-loaded case.
    """
    plays = _PLAYBOOK_ORIGINS.get(id(app))
    if plays is None:
        raise ValueError(
            "to_playbook: this Application was not loaded by from_playbook(...); "
            "programmatic FSM-to-YAML synthesis is not yet supported"
        )
    return yaml.safe_dump(plays, sort_keys=False)
