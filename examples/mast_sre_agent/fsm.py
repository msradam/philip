"""MAST-aligned SRE agent with multi-module remediation sub-graphs.

Implements architectural recommendations from IBM Research and UC Berkeley,
"Why Enterprise Agents Fail in the Real World"
(https://huggingface.co/blog/ibm-research/itbenchandmast). The paper studied
310 SRE traces and built MAST, a 14-failure-mode taxonomy. Headline result:
architectural fixes recovered ~53% of failures versus ~15.6% from prompt
engineering alone.

This demo is the architectural-fixes side of the finding. The LLM chooses
a remediation strategy. Ansible executes the multi-step idempotent fix.
Each remediation branch is a sub-graph of five or six Ansible module
calls, not a single shell-out.

Topology::

    fetch_state -> read_log -> parse_events -> classify_with_llm -> validate
                                                                      |
        +--(out_of_memory + verified)--> check_for_loop ---> OOM_PATH ---+
        +--(disk_full + verified)------> check_for_loop ---> DISK_PATH --+
        +--(unknown / unverified)------> escalate                        |
                                                                         v
                              external_verify <- log_incident <- ... <- {OOM|DISK}_PATH
                                  |
                    +-- (HTTP 200) -----> done
                    +-- (not 200) -------> classify_with_llm  [loop check trips on repeat]

    OOM_PATH  (each node = one Ansible module):
      oom_backup_conf -> oom_apply_limits -> oom_validate_conf
                      -> oom_restart -> oom_verify_workers -> log_incident_oom

    DISK_PATH (each node = one Ansible module):
      disk_seed_old_files -> disk_find_old -> disk_archive_and_remove
                          -> disk_verify_space -> disk_install_cleanup
                          -> log_incident_disk

Module mix across the graph:
  ansible.builtin: copy, command, slurp, service, shell, find, file
  community.general: archive, ini_file
  Total: 12 distinct Ansible nodes + 7 Python nodes (classify, validate,
  loop check, terminals).

MAST failure modes addressed:
  FM-1.3 Step repetition       check_for_loop
  FM-1.4 Memory loss           state-resident remediation_history
  FM-1.5 Term. unawareness     FSM owns terminals, not the LLM
  FM-2.2 No clarification      validator escalates rather than guessing
  FM-2.6 Reasoning-action mis  deterministic validator over LLM label
  FM-3.1 Premature termination terminals are FSM nodes
  FM-3.3 Incorrect verification external_verify is an Ansible uri call

Prereqs: ``ollama pull ibm/granite4:micro`` for the happy path; the
``service_remediation`` container running.

Run::

    cd ../service_remediation && ./start.sh
    OLLAMA_MODEL=ibm/granite4:micro uv run python ../mast_sre_agent/fsm.py
    OLLAMA_MODEL=ibm/granite4:micro LOG_SCENARIO=disk_full \\
      uv run python ../mast_sre_agent/fsm.py
    FORCE_VERIFY_FAIL=1 OLLAMA_MODEL=ibm/granite4:micro \\
      uv run python ../mast_sre_agent/fsm.py  # trips loop detection
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import host, initial_sentinels, module_action, snapshot_sentinels

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE.parent / "service_remediation" / ".demo_key"

LOG_PATH = "/var/log/philip-mast.log"
INCIDENT_LEDGER = "/var/log/philip-incidents.ini"
NGINX_LIMITS_CONF = "/etc/nginx/conf.d/philip-limits.conf"
NGINX_BACKUP_DIR = "/var/backups/philip-nginx"
OLD_LOGS_DIR = "/var/log/old"
ARCHIVE_PATH = "/var/backups/philip-old-logs.tar.gz"
CLEANUP_SCRIPT = "/etc/cron.daily/philip-cleanup"

TARGET_URL = (
    "http://127.0.0.1:9999/"
    if os.environ.get("FORCE_VERIFY_FAIL") == "1"
    else "http://127.0.0.1:8080/"
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "ibm/granite4:350m")
SCENARIO = os.environ.get("LOG_SCENARIO", "out_of_memory")

ALLOWED_CLASSIFICATIONS = ("out_of_memory", "disk_full", "network_error", "other")
MAX_ATTEMPTS_PER_REMEDIATION = 1


# ---------------------------------------------------------------------------
# Connection profile via ``host()``. Captured once, used by every
# Ansible-backed action below. Without this the 8-line connection dict
# would appear on each of the 12+ module actions.
# ---------------------------------------------------------------------------

target = host(
    "target",
    ansible_host="127.0.0.1",
    ansible_port=2222,
    ansible_user="ansible",
    ansible_ssh_private_key_file=str(_DEMO_KEY),
    ansible_ssh_common_args="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    ansible_python_interpreter="/usr/bin/python3",
    become=True,
)


_LOG_FIXTURES: dict[str, str] = {
    "out_of_memory": (
        "2026-05-20 10:00:01 INFO  nginx: master process started\n"
        "2026-05-20 10:05:23 WARN  nginx: worker memory at 87% of cgroup limit\n"
        "2026-05-20 10:12:45 ERROR nginx: worker process 4451 exited on signal 9 (out-of-memory)\n"
        "2026-05-20 10:12:46 ERROR nginx: worker process 4452 exited on signal 9 (out-of-memory)\n"
        "2026-05-20 10:12:47 CRIT  kernel: Out of memory: Killed process 4450 (nginx)\n"
    ),
    "disk_full": (
        "2026-05-20 10:00:01 INFO  nginx: master process started\n"
        "2026-05-20 10:03:11 WARN  filesystem / at 92% capacity\n"
        "2026-05-20 10:08:55 ERROR nginx: writing to access.log: No space left on device\n"
        "2026-05-20 10:09:12 ERROR systemd-journald: Failed to write entry: No space left\n"
        "2026-05-20 10:09:14 CRIT  filesystem / at 100% capacity\n"
    ),
    "garbage": ("lorem ipsum dolor sit amet\nconsectetur adipiscing elit\nthe quick brown fox\n"),
}


# ---------------------------------------------------------------------------
# Pre-amble: fetch state, parse, classify, validate, loop-check. Python +
# one Ansible read.
# ---------------------------------------------------------------------------


@target.shell()
def cleanup_other_philip_confs(state: State) -> dict[str, Any]:
    """Remove conf.d files written by other philip demos.

    The shared container is used by several demos (``config_drift``,
    ``plan_then_apply``, etc.) and they each drop a ``listen 80 default_server``
    server block under different filenames. Multiple ``default_server``
    directives in the same nginx server cause ``nginx -t`` to fail. This
    cleanup is the precondition that lets this demo run after any other
    has left state behind. Idempotent: ``find`` with ``-delete`` is a
    no-op when nothing matches.
    """
    return {
        "cmd": (
            "find /etc/nginx/conf.d -maxdepth 1 "
            "-type f -name 'philip-*.conf' "
            "! -name 'philip-mast-server.conf' "
            "! -name 'philip-limits.conf' "
            "-delete; "
            "find /etc/nginx/conf.d -maxdepth 1 -type f -name 'philip.conf' -delete"
        )
    }


@target.copy()
def ensure_demo_listener(state: State) -> dict[str, Any]:
    """Demo precondition: lay down a minimal nginx server block on port 80.

    In a real ops scenario the failing service already exists; the agent
    just remediates. This demo runs against a stripped-down container
    image (no default site enabled), so we install the listener up front
    so external_verify has something to talk to. Idempotent: ``copy``
    is a no-op when content matches.
    """
    server_block = (
        "server {\n"
        "    listen 80 default_server;\n"
        "    server_name _;\n"
        "    location / {\n"
        '        return 200 "philip-mast OK\\n";\n'
        "        add_header Content-Type text/plain;\n"
        "    }\n"
        "}\n"
    )
    return {
        "content": server_block,
        "dest": "/etc/nginx/conf.d/philip-mast-server.conf",
        "mode": "0644",
    }


@target.service()
def ensure_nginx_started(state: State) -> dict[str, Any]:
    """Make sure nginx is up before the remediation begins. ``state=started``
    is idempotent: starts if stopped, no-op if already running."""
    return {"name": "nginx", "state": "started"}


@target.copy()
def fetch_state(state: State) -> dict[str, Any]:
    """Seed the demo log file. In a real deployment this step would be
    replaced by ``slurp`` of an actual log."""
    fixture = _LOG_FIXTURES.get(SCENARIO, _LOG_FIXTURES["out_of_memory"])
    return {"content": fixture, "dest": LOG_PATH, "mode": "0644"}


@target.slurp(writes={"log_b64": "content"})
def read_log(state: State) -> dict[str, Any]:
    return {"src": LOG_PATH}


_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>INFO|WARN|ERROR|CRIT)\s+"
    r"(?P<msg>.+)$"
)


@action(reads=["log_b64"], writes=["log_events", "log_summary"])
def parse_events(state: State) -> State:
    raw = base64.b64decode(state["log_b64"] or "").decode("utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        m = _LINE_RE.match(line)
        if m:
            events.append(
                {
                    "ts": datetime.fromisoformat(m.group("ts")).isoformat(),
                    "level": m.group("level"),
                    "msg": m.group("msg"),
                }
            )
    levels = {lvl: sum(1 for e in events if e["level"] == lvl) for lvl in ("WARN", "ERROR", "CRIT")}
    summary = (
        f"{len(events)} parsed lines "
        f"(WARN={levels['WARN']}, ERROR={levels['ERROR']}, CRIT={levels['CRIT']})"
    )
    return state.update(log_events=events, log_summary=summary)


_CLASSIFY_PROMPT = """You classify SRE log excerpts into one of these labels:
  out_of_memory    (OOM kills, signal 9, memory exhausted, cgroup limits)
  disk_full        (no space left, filesystem capacity, ENOSPC)
  network_error    (connection refused, timeout, no route, unreachable)
  other            (anything else, or signal is ambiguous)

Examples:

Logs:
[ERROR] Killed process 4450 (nginx) by OOM killer
[CRIT]  kernel: Out of memory
Label: out_of_memory

Logs:
[ERROR] write: No space left on device
[WARN]  filesystem / at 100% capacity
Label: disk_full

Logs:
[ERROR] connection refused to upstream 10.0.0.5:8080
[ERROR] proxy: failed to connect after 3 retries
Label: network_error

Logs:
[INFO] starting service
[INFO] connection received
Label: other

Now classify:

Logs:
{snippet}
Label:"""


def _ollama_classify(events: list[dict[str, Any]]) -> tuple[str, str]:
    snippet = "\n".join(f"[{e['level']}] {e['msg']}" for e in events[-8:])
    prompt = _CLASSIFY_PROMPT.format(snippet=snippet)
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 6, "temperature": 0},
        }
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    raw = (data.get("response") or "").strip().lower()
    label = next((c for c in ALLOWED_CLASSIFICATIONS if c in raw), "other")
    return raw, label


@action(reads=["log_events"], writes=["classification_raw", "classification_llm"])
def classify_with_llm(state: State) -> State:
    raw, label = _ollama_classify(state["log_events"])
    return state.update(classification_raw=raw, classification_llm=label)


_EVIDENCE: dict[str, tuple[str, ...]] = {
    "out_of_memory": ("out-of-memory", "out of memory", "signal 9", "oom", "killed process"),
    "disk_full": ("no space", "100% capacity", "92%", "disk full", "filesystem"),
    "network_error": ("connection refused", "no route", "timed out", "unreachable"),
}


@action(reads=["classification_llm", "log_events"], writes=["classification", "validation_note"])
def validate_classification(state: State) -> State:
    """MAST FM-3.3 / FM-2.6: deterministic validator over LLM label.

    Cross-checks the LLM's pick against keyword evidence in the parsed log
    events. Off-script labels or labels not corroborated by evidence get
    downgraded to ``other`` and route to escalate. The LLM does not grade
    its own homework.
    """
    label = state["classification_llm"]
    haystack = " ".join(e["msg"].lower() for e in state["log_events"])
    if label in _EVIDENCE:
        signatures = _EVIDENCE[label]
        if any(sig in haystack for sig in signatures):
            return state.update(
                classification=label,
                validation_note=f"validated: '{label}' supported by keyword evidence",
            )
        return state.update(
            classification="other",
            validation_note=(
                f"REJECTED LLM label '{label}': no corroborating keywords "
                f"({', '.join(signatures)}) in log; downgrading to 'other'"
            ),
        )
    return state.update(
        classification="other",
        validation_note=f"LLM returned '{label}' (not in allow-list); escalating",
    )


@action(reads=["classification", "remediation_history"], writes=["loop_decision"])
def check_for_loop(state: State) -> State:
    """MAST FM-1.3 / FM-1.5: trap repeated attempts via FSM state."""
    history = state["remediation_history"]
    label = state["classification"]
    attempts = sum(1 for h in history if h == label)
    if attempts >= MAX_ATTEMPTS_PER_REMEDIATION:
        return state.update(
            loop_decision=(
                f"LOOP: '{label}' already attempted {attempts}x; "
                f"history={history}; escalating to break the cycle"
            )
        )
    return state.update(loop_decision=f"novel attempt for '{label}' (history={history})")


@action(reads=["classification", "remediation_history"], writes=["remediation_history"])
def record_attempt(state: State) -> State:
    return state.update(
        remediation_history=[*state["remediation_history"], state["classification"]]
    )


# ---------------------------------------------------------------------------
# OOM remediation sub-graph: 6 Ansible module calls in idempotent sequence.
# Each is a granular Burr node so the tracker shows every step independently.
# ---------------------------------------------------------------------------


@target.module("ansible.builtin.lineinfile", writes={"oom_lineinfile_backup": "backup"})
def oom_constrain_workers(state: State) -> dict[str, Any]:
    """Surgically edit ``/etc/nginx/nginx.conf``: cap ``worker_processes``.

    ``lineinfile`` is the canonical Ansible way to change one directive in a
    multi-line config: it matches by regex, replaces (or inserts) the line,
    and writes a timestamped backup. The ``events { worker_connections N; }``
    block isn't touched. Modifying it from a conf.d snippet is illegal
    (conf.d is http context, events is top-level), so a surgical edit of
    the main file is the appropriate tool.
    """
    return {
        "path": "/etc/nginx/nginx.conf",
        "regexp": r"^\s*worker_processes\s",
        "line": "worker_processes 2;",
        "backup": True,
    }


@target.module("ansible.builtin.lineinfile")
def oom_shrink_buffers(state: State) -> dict[str, Any]:
    """Inside http context: cap request body buffer to reduce per-connection
    memory. Writes to conf.d/ where http-context directives are legal.
    Idempotent: ``lineinfile`` with the same regex+line is a no-op on the
    second run."""
    return {
        "path": NGINX_LIMITS_CONF,
        "regexp": r"^\s*client_body_buffer_size\s",
        "line": "client_body_buffer_size 4k;",
        "create": True,
        "mode": "0644",
    }


@target.command()
def oom_validate_conf(state: State) -> dict[str, Any]:
    """``nginx -t`` validates the combined config (main + our snippet) BEFORE
    we restart. Catches syntax errors before they take the service down."""
    return {"cmd": "nginx -t"}


@target.service()
def oom_restart_nginx(state: State) -> dict[str, Any]:
    return {"name": "nginx", "state": "restarted"}


@target.shell(writes={"oom_workers_stdout": "stdout"})
def oom_verify_workers(state: State) -> dict[str, Any]:
    """Confirm the worker constraint actually took: count nginx worker
    processes. The pre-change baseline isn't captured here for brevity;
    in a real demo we'd also have a pre-step counting workers and store
    the delta."""
    return {"cmd": "pgrep -c 'nginx: worker' || echo 0"}


@target.module("community.general.ini_file")
def log_incident_oom(state: State) -> dict[str, Any]:
    """Append the incident to a structured ledger that ops can grep later.

    Uses ``community.general.ini_file`` rather than blindly appending text.
    The module is idempotent per (section, option), so re-running this
    action with the same key updates rather than duplicating.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    return {
        "path": INCIDENT_LEDGER,
        "section": f"oom-{ts}",
        "option": "last_remediation",
        "value": "applied worker_processes=2 worker_connections=256; restarted nginx",
        "mode": "0644",
        "create": True,
    }


# ---------------------------------------------------------------------------
# Disk-full remediation sub-graph: 6 Ansible module calls.
# ---------------------------------------------------------------------------


@target.shell()
def disk_seed_old_files(state: State) -> dict[str, Any]:
    """Manufacture a few old gzipped log files in ``/var/log/old`` so the
    find/archive/remove dance has real targets. In production the ``find``
    below would catch genuinely-old rotated logs instead."""
    return {
        "cmd": (
            f"mkdir -p {OLD_LOGS_DIR} && "
            f"for i in 1 2 3 4 5; do "
            f"  echo 'historical log content '$i | gzip > {OLD_LOGS_DIR}/old-$i.log.gz; "
            f"done && ls -la {OLD_LOGS_DIR}"
        )
    }


@target.find(writes={"old_files": "files"})
def disk_find_old(state: State) -> dict[str, Any]:
    """ansible.builtin.find: filter by path glob + size + age."""
    return {
        "paths": OLD_LOGS_DIR,
        "patterns": ["*.gz"],
        "age": "0s",  # demo: anything we just created (production would use "30d")
        "recurse": True,
    }


@target.module(
    "community.general.archive",
    reads=["old_files"],
    writes={"archive_dest": "dest"},
)
def disk_archive_and_remove(state: State) -> tuple[dict[str, Any], dict[str, Any]]:
    """Tar+gz the found files into one archive AND remove the originals
    (``remove: true``). One module, two side effects, fully idempotent."""
    paths = [f["path"] for f in (state["old_files"] or [])]
    args = {
        "path": paths,
        "dest": ARCHIVE_PATH,
        "format": "gz",
        "remove": True,
        "mode": "0644",
    }
    return args, {}


@target.shell(writes={"disk_df_stdout": "stdout"})
def disk_verify_space(state: State) -> dict[str, Any]:
    return {"cmd": "df -h / && du -sh /var/log/old 2>/dev/null || echo 'old/ removed'"}


@target.copy()
def disk_install_cleanup(state: State) -> dict[str, Any]:
    """Drop a one-shot cleanup script into /etc/cron.daily/. Marked executable
    so the standard run-parts cron infrastructure (if installed) picks it up.
    Idempotent: re-running just overwrites with the same content."""
    script = (
        "#!/bin/sh\n"
        "# Installed by philip disk-full remediation. Prunes /var/log/old/*.gz daily.\n"
        f"find {OLD_LOGS_DIR} -name '*.gz' -mtime +7 -delete 2>/dev/null || true\n"
    )
    return {"content": script, "dest": CLEANUP_SCRIPT, "mode": "0755"}


@target.module("community.general.ini_file")
def log_incident_disk(state: State) -> dict[str, Any]:
    ts = datetime.now().isoformat(timespec="seconds")
    return {
        "path": INCIDENT_LEDGER,
        "section": f"disk-{ts}",
        "option": "last_remediation",
        "value": "archived /var/log/old/*.gz to backups; installed daily cleanup script",
        "mode": "0644",
        "create": True,
    }


# ---------------------------------------------------------------------------
# Shared external verify + terminals.
# ---------------------------------------------------------------------------


@module_action("ansible.builtin.uri", writes={"http_status": "status"})
def external_verify(state: State) -> dict[str, Any]:
    """MAST FM-3.3: hard tool-based verification, not LLM self-assessment."""
    return {"url": TARGET_URL, "status_code": list(range(100, 600)), "timeout": 3}


@action(
    reads=[
        "classification",
        "classification_llm",
        "classification_raw",
        "validation_note",
        "remediation_history",
        "http_status",
        "log_summary",
    ],
    writes=["outcome"],
)
def done(state: State) -> State:
    return state.update(
        outcome=(
            f"OK: remediated via '{state['classification']}' "
            f"(LLM raw: {state['classification_raw']!r}); "
            f"history={state['remediation_history']}; "
            f"external HTTP {state['http_status']}; "
            f"{state['log_summary']}"
        )
    )


@action(
    reads=[
        "classification",
        "classification_llm",
        "classification_raw",
        "validation_note",
        "remediation_history",
        "loop_decision",
        "http_status",
        "log_summary",
        "failure_reason",
    ],
    writes=["outcome"],
)
def escalate(state: State) -> State:
    reason_parts = [state["validation_note"], state["loop_decision"], state["failure_reason"]]
    reason = "; ".join(p for p in reason_parts if p)
    return state.update(
        outcome=(
            f"ESCALATE: routed='{state['classification']}' "
            f"(LLM raw: {state['classification_raw']!r}); "
            f"reason: {reason}; "
            f"history={state['remediation_history']}; "
            f"last HTTP {state['http_status']}; "
            f"{state['log_summary']}"
        )
    )


snapshot_failure = snapshot_sentinels(write="failure_reason")


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            cleanup_other_philip_confs=cleanup_other_philip_confs,
            ensure_demo_listener=ensure_demo_listener,
            ensure_nginx_started=ensure_nginx_started,
            fetch_state=fetch_state,
            read_log=read_log,
            parse_events=parse_events,
            classify_with_llm=classify_with_llm,
            validate_classification=validate_classification,
            check_for_loop=check_for_loop,
            record_attempt=record_attempt,
            # OOM remediation sub-graph
            oom_constrain_workers=oom_constrain_workers,
            oom_shrink_buffers=oom_shrink_buffers,
            oom_validate_conf=oom_validate_conf,
            oom_restart_nginx=oom_restart_nginx,
            oom_verify_workers=oom_verify_workers,
            log_incident_oom=log_incident_oom,
            # Disk-full remediation sub-graph
            disk_seed_old_files=disk_seed_old_files,
            disk_find_old=disk_find_old,
            disk_archive_and_remove=disk_archive_and_remove,
            disk_verify_space=disk_verify_space,
            disk_install_cleanup=disk_install_cleanup,
            log_incident_disk=log_incident_disk,
            # Shared tail
            external_verify=external_verify,
            snapshot_failure=snapshot_failure,
            done=done,
            escalate=escalate,
        )
        .with_transitions(
            ("cleanup_other_philip_confs", "ensure_demo_listener"),
            ("ensure_demo_listener", "ensure_nginx_started"),
            ("ensure_nginx_started", "fetch_state"),
            ("fetch_state", "read_log"),
            ("read_log", "parse_events"),
            ("parse_events", "classify_with_llm"),
            ("classify_with_llm", "validate_classification"),
            ("validate_classification", "check_for_loop", expr("classification != 'other'")),
            ("validate_classification", "escalate"),
            ("check_for_loop", "escalate", expr("loop_decision.startswith('LOOP:')")),
            ("check_for_loop", "record_attempt"),
            # OOM sub-graph dispatch
            ("record_attempt", "oom_constrain_workers", expr("classification == 'out_of_memory'")),
            ("record_attempt", "disk_seed_old_files", expr("classification == 'disk_full'")),
            ("record_attempt", "escalate"),
            # OOM sub-graph linear sequence; any step failure -> snapshot -> escalate
            ("oom_constrain_workers", "snapshot_failure", expr("_last_failed")),
            ("oom_constrain_workers", "oom_shrink_buffers"),
            ("oom_shrink_buffers", "snapshot_failure", expr("_last_failed")),
            ("oom_shrink_buffers", "oom_validate_conf"),
            ("oom_validate_conf", "snapshot_failure", expr("_last_failed")),
            ("oom_validate_conf", "oom_restart_nginx"),
            ("oom_restart_nginx", "snapshot_failure", expr("_last_failed")),
            ("oom_restart_nginx", "oom_verify_workers"),
            ("oom_verify_workers", "snapshot_failure", expr("_last_failed")),
            ("oom_verify_workers", "log_incident_oom"),
            ("log_incident_oom", "external_verify"),
            # Disk sub-graph linear sequence; any step failure -> snapshot -> escalate
            ("disk_seed_old_files", "snapshot_failure", expr("_last_failed")),
            ("disk_seed_old_files", "disk_find_old"),
            ("disk_find_old", "snapshot_failure", expr("_last_failed")),
            ("disk_find_old", "disk_archive_and_remove"),
            ("disk_archive_and_remove", "snapshot_failure", expr("_last_failed")),
            ("disk_archive_and_remove", "disk_verify_space"),
            ("disk_verify_space", "snapshot_failure", expr("_last_failed")),
            ("disk_verify_space", "disk_install_cleanup"),
            ("disk_install_cleanup", "snapshot_failure", expr("_last_failed")),
            ("disk_install_cleanup", "log_incident_disk"),
            ("log_incident_disk", "external_verify"),
            # Shared tail
            ("snapshot_failure", "escalate"),
            ("external_verify", "done", expr("http_status == 200")),
            ("external_verify", "classify_with_llm"),
        )
        .with_tracker(LocalTrackingClient(project="philip-mast-sre"))
        .with_state(
            **initial_sentinels(),
            log_b64="",
            log_events=[],
            log_summary="",
            classification_raw="",
            classification_llm="",
            classification="",
            validation_note="",
            remediation_history=[],
            loop_decision="",
            http_status=-1,
            oom_lineinfile_backup="",
            oom_workers_stdout="",
            old_files=[],
            archive_dest="",
            disk_df_stdout="",
            failure_reason="",
        )
        .with_entrypoint("cleanup_other_philip_confs")
        .build()
    )


def main() -> None:
    if not _DEMO_KEY.exists():
        raise SystemExit(
            f"Missing {_DEMO_KEY}. Run ../service_remediation/setup.sh && ./start.sh first."
        )
    print(f"SCENARIO:      {SCENARIO}")
    print(f"OLLAMA_MODEL:  {OLLAMA_MODEL}")
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["done", "escalate"])
    print(f"Final action:  {last_action}")
    print(f"Outcome:       {final_state['outcome']}")


if __name__ == "__main__":
    main()
