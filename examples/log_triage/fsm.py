"""Log triage FSM with two non-Ansible decision nodes between Ansible I/O.

Non-Ansible actions interleaved with Ansible-backed I/O in one Burr graph:

1. ``parse_events`` is a plain Python ``@action`` that does regex and
   datetime work to turn raw log text into structured records. The
   equivalent in a playbook would be Jinja2 ``regex_findall`` plus
   ``selectattr``, or a shelled-out ``awk`` pipeline.

2. ``classify_with_llm`` is a plain Python ``@action`` that calls IBM
   Granite 4 350m over the Ollama HTTP API to classify the dominant
   failure signature. The model is 350M parameters and runs locally
   in roughly one second.

Both sit between Ansible-backed I/O (``slurp`` to read the log, ``service``
and ``shell`` to remediate, ``uri`` to verify). The graph::

    seed_log -> fetch_logs -> parse_events -> classify_with_llm -> validate
                                                                       |
        +--(out_of_memory)--> restart_service ---+
        +--(disk_full)------> clear_temp_files --+--> verify_endpoint
        +--(other/unknown)--------------------------> escalate

The LLM picks one label from a fixed allow-list. ``validate`` is
deterministic and routes off-list responses to escalate. The LLM never
generates shell and never chooses an unbounded action; it picks a label,
and philip maps labels to pre-defined Ansible-backed nodes.

Container: reuses ``examples/service_remediation`` setup. Prereq:
``ollama pull ibm/granite4:350m`` plus ``ollama serve`` running.

Run::

    cd ../service_remediation && ./start.sh
    uv run python ../log_triage/fsm.py
    LOG_SCENARIO=disk_full uv run python ../log_triage/fsm.py
    LOG_SCENARIO=garbage   uv run python ../log_triage/fsm.py
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

from philip import initial_sentinels, module_action

_HERE = Path(__file__).resolve().parent
_DEMO_KEY = _HERE.parent / "service_remediation" / ".demo_key"

LOG_PATH = "/var/log/philip-demo.log"
TARGET_URL = "http://127.0.0.1:8080/"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "ibm/granite4:350m")
SCENARIO = os.environ.get("LOG_SCENARIO", "out_of_memory")

ALLOWED_CLASSIFICATIONS = ("out_of_memory", "disk_full", "network_error", "other")

# Three synthetic log shapes the demo can seed onto the container. Each
# is realistic-looking nginx/systemd noise with one clear signature.
_LOG_FIXTURES: dict[str, str] = {
    "out_of_memory": (
        "2026-05-20 10:00:01 INFO  nginx: master process /usr/sbin/nginx started\n"
        "2026-05-20 10:05:23 WARN  nginx: worker memory at 87% of cgroup limit\n"
        "2026-05-20 10:12:45 ERROR nginx: worker process 4451 exited on signal 9 (out-of-memory)\n"
        "2026-05-20 10:12:46 ERROR nginx: worker process 4452 exited on signal 9 (out-of-memory)\n"
        "2026-05-20 10:12:47 CRIT  kernel: Out of memory: Killed process 4450 (nginx)\n"
    ),
    "disk_full": (
        "2026-05-20 10:00:01 INFO  nginx: master process /usr/sbin/nginx started\n"
        "2026-05-20 10:03:11 WARN  filesystem / at 92% capacity\n"
        "2026-05-20 10:08:55 ERROR nginx: writing to access.log: No space left on device\n"
        "2026-05-20 10:09:12 ERROR systemd-journald: Failed to write entry: No space left\n"
        "2026-05-20 10:09:14 CRIT  filesystem / at 100% capacity; service degrading\n"
    ),
    "garbage": (
        "lorem ipsum dolor sit amet\n"
        "consectetur adipiscing elit\n"
        "the quick brown fox jumps over the lazy dog\n"
    ),
}

TARGET_HOST = "target"
TARGET_CONN: dict[str, Any] = {
    "ansible_host": "127.0.0.1",
    "ansible_port": 2222,
    "ansible_user": "ansible",
    "ansible_ssh_private_key_file": str(_DEMO_KEY),
    "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    "ansible_python_interpreter": "/usr/bin/python3",
}


@module_action(
    "ansible.builtin.copy",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def seed_log(state: State) -> dict[str, Any]:
    """Seed the demo log file with a fixture matching LOG_SCENARIO."""
    fixture = _LOG_FIXTURES.get(SCENARIO, _LOG_FIXTURES["out_of_memory"])
    return {"content": fixture, "dest": LOG_PATH, "mode": "0644"}


@module_action(
    "ansible.builtin.slurp",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    writes={"log_b64": "content"},
)
def fetch_logs(state: State) -> dict[str, Any]:
    """Read the log file from the target. slurp returns base64-encoded content."""
    return {"src": LOG_PATH}


_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>INFO|WARN|ERROR|CRIT)\s+"
    r"(?P<msg>.+)$"
)


@action(reads=["log_b64"], writes=["log_events", "log_summary"])
def parse_events(state: State) -> State:
    """Decode base64, regex-parse each line into a structured event.

    The equivalent in a playbook is Jinja2 ``regex_findall`` plus
    ``selectattr`` plus ``map(attribute=...)`` chains, or a shelled-out
    ``awk`` or ``grep`` pipeline that re-parses the captured stdout.
    """
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


def _ollama_classify(events: list[dict[str, Any]]) -> tuple[str, str]:
    """Ask Granite 4 350m to pick one label from ALLOWED_CLASSIFICATIONS.

    Returns (raw_response, normalized_label). The caller validates that
    normalized_label is in the allow-list; off-script LLM output lands
    as "other" and routes to escalate.
    """
    snippet = "\n".join(f"[{e['level']}] {e['msg']}" for e in events[-8:])
    prompt = (
        "You are an SRE log classifier. Read the log lines below and pick "
        "exactly one label from this list:\n"
        "  out_of_memory\n  disk_full\n  network_error\n  other\n\n"
        "Respond with only the label, no explanation.\n\n"
        f"Logs:\n{snippet}\n\nLabel:"
    )
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


@action(
    reads=["log_events"],
    writes=["classification_raw", "classification_llm"],
)
def classify_with_llm(state: State) -> State:
    """Hand the structured log events to Granite for classification.

    The LLM only picks a label; routing is deterministic downstream. We
    store the LLM's pick under ``classification_llm`` so the validator
    can override it before any Ansible action runs.
    """
    raw, label = _ollama_classify(state["log_events"])
    return state.update(classification_raw=raw, classification_llm=label)


# Keyword signatures that must appear in the log evidence to corroborate
# the LLM's classification. The 350m model on this dataset routinely picks
# the wrong label or invents one; this validator is the safety net that
# stops a wrong classification from triggering a real Ansible remediation.
_EVIDENCE: dict[str, tuple[str, ...]] = {
    "out_of_memory": ("out-of-memory", "out of memory", "signal 9", "oom"),
    "disk_full": ("no space", "100% capacity", "92%", "disk full"),
    "network_error": ("connection refused", "no route to host", "timed out", "unreachable"),
}


@action(
    reads=["classification_llm", "log_events"],
    writes=["classification", "validation_note"],
)
def validate_classification(state: State) -> State:
    """Verify the LLM's label against keyword evidence in the actual log lines.

    If the label is in the allow-list AND the log text contains corroborating
    keywords, accept it. Otherwise downgrade to "other", which routes to
    escalate. The point of this step: an LLM that's confidently wrong about
    a small fixture (the 350m model frequently is) cannot cause us to run
    a remediation against the wrong fault signature.
    """
    label = state["classification_llm"]
    haystack = " ".join(e["msg"].lower() for e in state["log_events"])
    if label in _EVIDENCE:
        signatures = _EVIDENCE[label]
        if any(sig in haystack for sig in signatures):
            note = f"validated: '{label}' supported by keyword evidence"
            return state.update(classification=label, validation_note=note)
        note = (
            f"REJECTED LLM label '{label}': no corroborating keywords "
            f"({', '.join(signatures)}) in log; downgrading to 'other'"
        )
        return state.update(classification="other", validation_note=note)
    note = f"LLM returned '{label}' (not in remediation allow-list); routing to escalate"
    return state.update(classification="other", validation_note=note)


@module_action(
    "ansible.builtin.service",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
)
def restart_service(state: State) -> dict[str, Any]:
    """Remediation for out_of_memory: restart nginx so the kernel reclaims OOM-killed workers."""
    return {"name": "nginx", "state": "restarted"}


@module_action(
    "ansible.builtin.shell",
    host=TARGET_HOST,
    connection=TARGET_CONN,
    become=True,
    writes={"cleanup_stdout": "stdout"},
)
def clear_temp_files(state: State) -> dict[str, Any]:
    """Remediation for disk_full: prune /tmp and /var/log/*.gz."""
    return {
        "cmd": "rm -rf /tmp/*.tmp /var/log/*.gz 2>/dev/null; df -h /",
    }


@module_action(
    "ansible.builtin.uri",
    writes={"http_status": "status"},
)
def verify_endpoint(state: State) -> dict[str, Any]:
    return {
        "url": TARGET_URL,
        "status_code": list(range(100, 600)),
        "timeout": 3,
    }


@action(
    reads=[
        "classification",
        "classification_llm",
        "classification_raw",
        "validation_note",
        "log_summary",
        "http_status",
    ],
    writes=["outcome"],
)
def done(state: State) -> State:
    return state.update(
        outcome=(
            f"OK: routed='{state['classification']}' "
            f"(LLM picked '{state['classification_llm']}', raw {state['classification_raw']!r}); "
            f"{state['validation_note']}; "
            f"{state['log_summary']}; post-remediation HTTP {state['http_status']}"
        )
    )


@action(
    reads=[
        "classification",
        "classification_llm",
        "classification_raw",
        "validation_note",
        "log_summary",
    ],
    writes=["outcome"],
)
def escalate(state: State) -> State:
    return state.update(
        outcome=(
            f"ESCALATE: routed='{state['classification']}' "
            f"(LLM picked '{state['classification_llm']}', raw {state['classification_raw']!r}); "
            f"{state['validation_note']}; "
            f"{state['log_summary']}"
        )
    )


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            seed_log=seed_log,
            fetch_logs=fetch_logs,
            parse_events=parse_events,
            classify_with_llm=classify_with_llm,
            validate_classification=validate_classification,
            restart_service=restart_service,
            clear_temp_files=clear_temp_files,
            verify_endpoint=verify_endpoint,
            done=done,
            escalate=escalate,
        )
        .with_transitions(
            ("seed_log", "fetch_logs"),
            ("fetch_logs", "parse_events"),
            ("parse_events", "classify_with_llm"),
            ("classify_with_llm", "validate_classification"),
            (
                "validate_classification",
                "restart_service",
                expr("classification == 'out_of_memory'"),
            ),
            ("validate_classification", "clear_temp_files", expr("classification == 'disk_full'")),
            ("validate_classification", "escalate"),
            ("restart_service", "verify_endpoint"),
            ("clear_temp_files", "verify_endpoint"),
            ("verify_endpoint", "done"),
        )
        .with_tracker(LocalTrackingClient(project="philip-log-triage"))
        .with_state(
            **initial_sentinels(),
            log_b64="",
            log_events=[],
            log_summary="",
            classification_raw="",
            classification_llm="",
            classification="",
            validation_note="",
            http_status=-1,
            cleanup_stdout="",
        )
        .with_entrypoint("seed_log")
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
