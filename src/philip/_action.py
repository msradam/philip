"""The ``module_action`` decorator: a Burr action backed by an Ansible module."""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from burr.core import State, action

from philip._runner import run_module

ModuleArgsBuilder = Callable[..., dict[str, Any]]
WrappedAction = Callable[..., tuple[dict[str, Any], State]]

SENTINEL_KEYS = (
    "_last_action",
    "_last_failed",
    "_last_changed",
    "_last_unreachable",
    "_last_msg",
    "_last_failure_kind",
)

# Classified failure modes philip distinguishes structurally. The labels
# are loosely aligned with the MAST taxonomy (Multi-Agent System failure
# Taxonomy, IBM Research + UC Berkeley, arXiv:2503.13657), restricted to
# the modes a deterministic FSM can detect from an Ansible module result
# without needing an LLM judge. Transitions can branch on these to take
# distinct recovery paths: retry-with-backoff for ``unreachable``, escalate
# immediately for ``auth_failed``, alternative-module fallback for
# ``module_error``, longer timeout next try for ``timeout``.
FAILURE_KIND_OK = "ok"
FAILURE_KIND_UNREACHABLE = "unreachable"
FAILURE_KIND_AUTH_FAILED = "auth_failed"
FAILURE_KIND_TIMEOUT = "timeout"
FAILURE_KIND_MODULE_ERROR = "module_error"

FAILURE_KINDS = (
    FAILURE_KIND_OK,
    FAILURE_KIND_UNREACHABLE,
    FAILURE_KIND_AUTH_FAILED,
    FAILURE_KIND_TIMEOUT,
    FAILURE_KIND_MODULE_ERROR,
)


def _classify_failure(result: Mapping[str, Any]) -> str:
    """Map an Ansible module result dict to one of :data:`FAILURE_KINDS`.

    The classification is conservative and pattern-based: ``unreachable`` and
    ``failed`` flags are trusted; the diagnostic ``msg`` is scanned for
    well-known phrases ansible-playbook emits for auth failure and timeout
    cases. Anything that failed but doesn't fit the named categories is
    ``module_error``. Anything that didn't fail is ``ok``.
    """
    if result.get("unreachable"):
        return FAILURE_KIND_UNREACHABLE
    if not result.get("failed"):
        return FAILURE_KIND_OK
    msg = str(result.get("msg") or "").lower()
    # ansible-runner's timeout path lands here via run_module's overrun fallback;
    # the message is "ansible-playbook exceeded timeout of Ns ..."
    if "exceeded timeout" in msg or msg.startswith("timed out"):
        return FAILURE_KIND_TIMEOUT
    # OpenSSH and ansible itself emit these on auth failures.
    if (
        "permission denied" in msg
        or "authentication failed" in msg
        or ("publickey" in msg and "denied" in msg)
    ):
        return FAILURE_KIND_AUTH_FAILED
    return FAILURE_KIND_MODULE_ERROR


def initial_sentinels() -> dict[str, Any]:
    """Default values for ambient sentinel keys, suitable for ``with_state(...)``.

    Pre-seeding sentinels lets transitions reference ``_last_failed`` before any
    ``@module_action`` has run yet (e.g. when the entrypoint is a plain Python
    action that branches on whether to bother calling Ansible at all).
    """
    return {
        "_last_action": "",
        "_last_failed": False,
        "_last_changed": False,
        "_last_unreachable": False,
        "_last_msg": "",
        "_last_failure_kind": FAILURE_KIND_OK,
    }


def snapshot_sentinels(*, write: str = "failure_reason", max_msg_len: int = 300) -> WrappedAction:
    """Pure-Python Burr action that copies the ambient sentinels into a durable state key.

    Sentinels are overwritten by every subsequent ``@module_action``, so any
    recovery step (rollback, cleanup) following a failure clobbers the diagnostic
    before a terminal action can show it. Insert this action between the failure
    point and the recovery step::

        ("test_config", "snapshot_failure", expr("_last_failed")),
        ("snapshot_failure", "restore_backup"),
        ("restore_backup", "escalate"),

    The terminal then reads ``write`` (default: ``failure_reason``) to surface
    *why* we escalated, even after the recovery action ran cleanly.
    """

    @action(reads=["_last_action", "_last_msg"], writes=[write])
    def _snapshot(state: State) -> State:
        reason = f"{state['_last_action']}: {state['_last_msg'][:max_msg_len]}"
        return state.update(**{write: reason})

    return _snapshot


def module_action(
    module: str,
    *,
    reads: Sequence[str] = (),
    writes: Sequence[str] | Mapping[str, str] = (),
    register: str | None = None,
    host: str = "localhost",
    connection: Mapping[str, Any] | None = None,
    become: bool = False,
    check_mode: bool = False,
    diff: bool = False,
    timeout: float | None = None,
) -> Callable[[ModuleArgsBuilder], WrappedAction]:
    """Wrap a function so it executes an Ansible module as a Burr action.

    The wrapped function receives Burr ``State`` and returns a dict of module
    arguments. After the module runs, each entry in ``writes`` projects a
    field from the module result into state. ``writes`` may be a list (each
    name is both the state key and the result key) or a dict mapping
    ``state_key -> result_key`` (useful when the same Ansible result field
    must land under different state names in different actions, e.g. shell
    ``stdout`` becoming ``uptime_stdout`` vs ``df_stdout``).

    ``register`` is the Ansible-style idiom for "capture the entire module
    result under a single state key." Equivalent to playbook
    ``register: <name>``. The full result dict (with ``changed``, ``stdout``,
    ``stderr``, all module-specific fields) lands at ``state[register]``,
    in addition to any fields explicitly projected via ``writes``.

    Every action also writes ambient sentinels: ``_last_action`` (this
    action's name), ``_last_failed``, ``_last_changed``, ``_last_unreachable``,
    and ``_last_msg`` (Ansible's diagnostic message on failure). Transitions
    can branch on these without each action having to opt-in via ``writes``.

    ``check_mode=True`` runs the module in dry-run mode: it reports what it
    would change without making changes. Pair with ``diff=True`` to also
    capture structured before/after content. Useful for plan-before-apply
    patterns.

    ``timeout`` is the per-call ansible-playbook timeout in seconds. When
    ``None`` (the default), the runner's library-level default (five
    minutes) applies. On overrun the underlying ansible-playbook subprocess
    is killed and the action's result carries ``failed=True`` plus a
    diagnostic ``msg``.

    ``connection`` is an optional dict of Ansible hostvars (``ansible_host``,
    ``ansible_port``, ``ansible_user``, ``ansible_ssh_private_key_file``,
    ``ansible_ssh_common_args``, etc.) that targets a remote host. Without it,
    actions run on the local controller.
    """
    reads_list = list(reads)
    writes_map = dict(writes) if isinstance(writes, Mapping) else {name: name for name in writes}
    user_writes_keys = list(writes_map.keys())
    all_writes_keys = user_writes_keys + [k for k in SENTINEL_KEYS if k not in writes_map]
    if register is not None and register not in all_writes_keys:
        all_writes_keys.append(register)
    if diff and "_last_diff" not in all_writes_keys:
        all_writes_keys.append("_last_diff")

    def decorator(fn: ModuleArgsBuilder) -> WrappedAction:
        @action(reads=reads_list, writes=all_writes_keys)
        @functools.wraps(fn)
        def wrapped(state: State, **inputs: Any) -> tuple[dict[str, Any], State]:
            # Forward Burr per-step inputs (declared in fn's signature beyond
            # ``state``) to the user function. ``functools.wraps`` preserves
            # fn's signature for Burr's introspection, so the inputs are
            # surfaced to MCP clients via JSON Schema.
            user_return = fn(state, **inputs)
            # Accept either a plain args dict (module args only; state is
            # populated from module result via writes_map) or a (args, overrides)
            # tuple. Overrides are useful when state fields are computed in
            # Python from inputs (e.g., "total = qty * price") rather than read
            # back from the module result, which only knows about checksums and
            # paths, not app-domain values.
            state_overrides: dict[str, Any]
            if isinstance(user_return, tuple):
                module_args, state_overrides = user_return
            else:
                module_args, state_overrides = user_return, {}
            run_kwargs: dict[str, Any] = {
                "host": host,
                "connection": connection,
                "become": become,
                "check_mode": check_mode,
                "diff": diff,
            }
            if timeout is not None:
                run_kwargs["timeout"] = timeout
            result = run_module(
                module,
                module_args,
                **run_kwargs,
            )
            update: dict[str, Any] = {
                state_key: result.get(result_key) for state_key, result_key in writes_map.items()
            }
            update.update(state_overrides)
            if register is not None:
                update[register] = result
            if diff:
                # Project ansible's structured diff into a durable state field so
                # the tracker captures before/after content and downstream Python
                # actions (plan reviewers, approval gates) can read it.
                update["_last_diff"] = result.get("diff")
            update["_last_action"] = fn.__name__
            update["_last_failed"] = bool(result.get("failed"))
            update["_last_changed"] = bool(result.get("changed"))
            update["_last_unreachable"] = bool(result.get("unreachable"))
            update["_last_msg"] = str(result.get("msg") or "")
            update["_last_failure_kind"] = _classify_failure(result)
            # Return (result, state) so Burr's tracker captures the full Ansible
            # module output (stdout/stderr/rc/diff/changed/...) alongside the state
            # snapshot. State alone would drop the rich payload: a 300-line
            # nginx -t output, or a template module's full diff, would be lost.
            return result, state.update(**update)

        return wrapped

    return decorator
