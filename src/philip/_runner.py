"""Thin wrapper around ansible-runner that invokes a single module and returns its result.

Concurrency: a single ``Application``'s actions execute serially; this module is
written to that assumption. Multiple ``run_module`` calls running concurrently
against the same target host can collide on the shared SSH ``ControlPath`` socket
and produce undefined behavior. Multi-host fan-out via parallel Applications is
fine; parallel Applications against the same host is not.

Debugging: set ``PHILIP_DEBUG=1`` in the environment to disable ansible-runner's
quiet mode. The full ansible-playbook stdout/stderr will then appear on the
caller's stdout, which is the only signal when ansible-playbook fails in a way
that doesn't make it into the event stream.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import ansible_runner  # type: ignore[import-untyped]
import yaml

# Process-level persistent private_data_dir. Created lazily on the first
# run_module call, torn down at interpreter exit. Reusing the directory
# across module calls saves ~50-100ms per action from the filesystem
# materialization overhead (mkdir + write inventory + write playbook +
# rmtree). The trade-off: a multi-host playbook can no longer assume each
# call has a pristine inventory, so the inventory file is rewritten on
# every call rather than left stale.
#
# Setting ``PHILIP_NO_REUSE_DATA_DIR=1`` falls back to per-call tempdirs,
# which is useful for debugging or when multiple Applications run concurrently
# from the same process (which is otherwise unsupported per the module
# docstring).
_PERSISTENT_DATA_DIR: Path | None = None


def _no_reuse_data_dir() -> bool:
    return os.environ.get("PHILIP_NO_REUSE_DATA_DIR", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _get_persistent_data_dir() -> Path:
    """Return the process-wide private_data_dir, creating it on first use
    and registering an atexit cleanup. Each call wipes the inventory/host_vars
    and project/playbook.yml so stale content from a previous module call
    can't leak into the next one."""
    global _PERSISTENT_DATA_DIR
    if _PERSISTENT_DATA_DIR is None:
        path = Path(tempfile.mkdtemp(prefix="philip-persistent-"))
        atexit.register(_cleanup_persistent_data_dir, path)
        _PERSISTENT_DATA_DIR = path
    # Fresh inventory + project on every call. The directory itself stays.
    inv_dir = _PERSISTENT_DATA_DIR / "inventory"
    if inv_dir.exists():
        shutil.rmtree(inv_dir)
    project_dir = _PERSISTENT_DATA_DIR / "project"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    return _PERSISTENT_DATA_DIR


def _cleanup_persistent_data_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


# Cross-process stable ControlPath so all philip-issued SSH sessions to the
# same (host, port, user) tuple share one multiplexed connection. Without this
# every @module_action does a fresh handshake; with it, only the first action
# in a sequence pays handshake cost. Subsequent calls multiplex through the
# persistent master for ``ControlPersist`` seconds.
_PHILIP_CONTROL_DIR = Path.home() / ".ssh" / ".philip-cm"
_DEFAULT_SSH_ARGS = (
    f"-C -o ControlMaster=auto -o ControlPersist=120s -o ControlPath={_PHILIP_CONTROL_DIR}/%h-%p-%r"
)

# Default envvars applied on every run_module call. ansible-runner forwards
# these to the ansible-playbook subprocess. Anything the caller wants to
# override they can do via ``ansible_ssh_common_args`` on the connection.
_DEFAULT_ENVVARS: dict[str, str] = {
    "ANSIBLE_HOST_KEY_CHECKING": "False",
    "ANSIBLE_PIPELINING": "True",  # ~30-50% fewer SSH round-trips per module
    "ANSIBLE_SSH_ARGS": _DEFAULT_SSH_ARGS,
}

# Default per-module timeout (seconds). Five minutes covers most realistic
# Ansible modules; long-running ones (package installs, image pulls) can
# override per-action via ``module_action(..., timeout=N)``.
_DEFAULT_TIMEOUT_S: float = 300.0


def _ensure_control_dir() -> None:
    """``~/.ssh/.philip-cm/`` needs to exist before OpenSSH writes a socket
    into it. Mode 0700 so other users can't hijack the multiplexed sessions."""
    _PHILIP_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_PHILIP_CONTROL_DIR, 0o700)


def _debug_enabled() -> bool:
    return os.environ.get("PHILIP_DEBUG", "").lower() in ("1", "true", "yes")


def run_module(
    module: str,
    args: dict[str, Any],
    *,
    host: str = "localhost",
    connection: Mapping[str, Any] | None = None,
    become: bool = False,
    check_mode: bool = False,
    diff: bool = False,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run an Ansible module against a host and return its result dict.

    ``connection`` is a dict of Ansible hostvars (``ansible_host``,
    ``ansible_port``, ``ansible_user``, ``ansible_ssh_private_key_file``,
    ``ansible_ssh_common_args``, etc.) written as host_vars/<host>.yml.
    When ``connection`` is None and ``host`` is localhost, ``ansible_connection=local``
    is set so no ssh round-trip is needed.

    ``check_mode=True`` makes the module report what it would change without
    making changes (equivalent to ``ansible-playbook --check``). Pair with
    ``diff=True`` to also capture structured before/after content under the
    result's ``diff`` key. Together they implement the plan-before-apply
    pattern.

    ``timeout`` is the maximum number of seconds ansible-playbook may run for
    a single module invocation; on overrun the subprocess is killed and the
    returned dict carries ``failed=True`` plus a diagnostic ``msg``. Default
    is five minutes. ``None`` disables the timeout entirely.

    Pressing Ctrl-C during a long-running call asks ansible-runner to cancel
    the subprocess cleanly via its ``cancel_callback`` hook, then re-raises
    ``KeyboardInterrupt``. Without this, ansible-playbook would orphan and
    the SSH ``ControlMaster`` socket would linger for ``ControlPersist``
    seconds.

    The result always includes Ansible's diagnostic fields on failure
    (``failed``, ``msg``, optionally ``unreachable``) so callers can branch
    on them via Burr transitions instead of catching exceptions.
    """
    # Reuse the process-wide persistent dir by default. Per-call tempdirs
    # only happen when explicitly disabled via the env var; the persistent
    # path saves ~50-100ms per call from filesystem materialization.
    if _no_reuse_data_dir():
        ctx = tempfile.TemporaryDirectory(prefix="philip-")
        tmp = ctx.__enter__()
        cleanup_ctx: Any = ctx
        tmp_path = Path(tmp)
    else:
        tmp_path = _get_persistent_data_dir()
        cleanup_ctx = None

    try:
        inv_dir = tmp_path / "inventory"
        inv_dir.mkdir()
        (inv_dir / "hosts").write_text(f"{host}\n")

        hostvars: dict[str, Any] = dict(connection) if connection else {}
        if connection is None and host in ("localhost", "127.0.0.1"):
            hostvars["ansible_connection"] = "local"
            # Default to the venv interpreter so controller-side collections
            # (community.docker, community.crypto, ...) see the same packages
            # the user installed via uv/pip. Otherwise Ansible's auto-discovery
            # picks the first python on PATH, which is usually the system Python
            # without philip's dependencies.
            hostvars.setdefault("ansible_python_interpreter", sys.executable)
        if hostvars:
            host_vars_dir = inv_dir / "host_vars"
            host_vars_dir.mkdir()
            (host_vars_dir / f"{host}.yml").write_text(yaml.safe_dump(hostvars))

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        task: dict[str, Any] = {"name": "philip", module: args}
        if check_mode:
            task["check_mode"] = True
        if diff:
            task["diff"] = True
        play: list[dict[str, Any]] = [
            {
                "hosts": host,
                "gather_facts": False,
                "become": become,
                "tasks": [task],
            }
        ]
        (project_dir / "playbook.yml").write_text(yaml.safe_dump(play))

        _ensure_control_dir()

        # Track Ctrl-C via a closure that ansible-runner can poll. Restore the
        # previous SIGINT handler on the way out. ``signal.signal`` only works
        # in the main thread; from any other thread the install raises
        # ``ValueError`` and we fall through to no-cancel-support, which is
        # an acceptable degradation.
        cancelled = {"flag": False}

        def _on_sigint(_signum: int, _frame: Any) -> None:
            cancelled["flag"] = True

        previous_handler: Any = None
        try:
            previous_handler = signal.signal(signal.SIGINT, _on_sigint)
        except ValueError:
            previous_handler = None

        runner_kwargs: dict[str, Any] = {
            "private_data_dir": str(tmp_path),
            "playbook": "playbook.yml",
            "quiet": not _debug_enabled(),
            "envvars": _DEFAULT_ENVVARS,
            "cancel_callback": lambda: cancelled["flag"],
        }
        if timeout is not None:
            runner_kwargs["timeout"] = timeout
        if diff:
            # ansible-runner forwards --diff via the cmdline flag rather than via
            # the play structure; the task-level ``diff: true`` covers the play
            # side but the runner also needs to pass --diff for the cli switch.
            runner_kwargs["cmdline"] = "--diff"

        try:
            runner = ansible_runner.run(**runner_kwargs)
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGINT, previous_handler)

        if cancelled["flag"]:
            # ansible-runner returned because the cancel_callback fired. Re-raise
            # the interrupt so the caller's stack unwinds normally instead of
            # leaving the FSM thinking the module returned a result.
            raise KeyboardInterrupt

        result = _extract_result(runner)
        if runner.status == "timeout":
            # Overwrite (don't just setdefault) so the classifier downstream can
            # match the well-known "exceeded timeout" phrase regardless of what
            # ansible-playbook itself emitted on its way out.
            result["failed"] = True
            result["msg"] = f"ansible-playbook exceeded timeout of {timeout}s and was terminated"
        return result
    finally:
        if cleanup_ctx is not None:
            cleanup_ctx.__exit__(None, None, None)


def _extract_result(runner: Any) -> dict[str, Any]:
    last_result: dict[str, Any] = {}
    for event in runner.events:
        ev = event.get("event", "")
        if ev not in ("runner_on_ok", "runner_on_failed", "runner_on_unreachable"):
            continue
        res = event.get("event_data", {}).get("res", {})
        if not isinstance(res, dict):
            continue
        last_result = dict(res)
        if ev == "runner_on_failed":
            last_result.setdefault("failed", True)
        elif ev == "runner_on_unreachable":
            last_result.setdefault("unreachable", True)
            last_result.setdefault("failed", True)

    if not last_result:
        return {
            "failed": True,
            "msg": f"ansible-runner produced no module result; status={runner.status}",
        }
    return last_result
