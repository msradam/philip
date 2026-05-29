"""etcdMembersDown — Burr FSM lifted from openshift/runbooks markdown.

This FSM is a hand-written exemplar of what `theodosia/skills/runbook-to-fsm.md`
should produce when applied to the source runbook (SOURCE.md). It demonstrates
the four structural elements that justify the FSM over running shell commands
by hand:

1. **Diagnosis phase as gated sequence.** Master-nodes, upgrade-status, MCO,
   and etcd-health checks are individual states; the agent (model or human)
   cannot skip ahead to mitigation without all four registering in state.

2. **Mitigation-branch selection as a typed transition.** The source prose has
   three remediation paths intermixed in one paragraph; the FSM hoists them
   into named transitions gated on a `selected_mitigation` state field the
   agent must set explicitly. Refusal is a fourth transition: "I'm not sure
   which path applies" routes to escalation, not to a guess.

3. **Approval gate on the destructive path.** Manual node restart can break
   quorum further if etcd is already at 2/3. The FSM inserts an explicit
   `approve_destructive` state that the agent must transition through with a
   recorded justification. Other mitigations (wait, inspect cloud console)
   skip the gate.

4. **Verification phase the source runbook lacks.** The runbook ends at
   "mitigation applied." The FSM adds re-checking etcdctl health; declares
   `resolved` only on quorum restored; otherwise routes back to
   classify_situation or escalates.

When mounted via `theodosia.mount(build_application(alert=...))`, the model
calls `step("check_master_nodes")`, etc. Each call returns the structured
result and the valid next actions. The model cannot call `mitigation_manual_restart`
without first passing through `approve_destructive`. Postmortem replay via
`fork_at(sequence_id)` lets you ask "what if we'd selected wait_for_upgrade
instead of manual_restart at step 7?"
"""

from __future__ import annotations

import subprocess
from typing import Any

from burr.core import ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient


# ── Section: Overview / Impact (informational, embedded as docstring) ─────


@action(reads=["alert"], writes=["namespace", "cluster_ctx", "phase"])
def acknowledge(state: State) -> State:
    """Extract context from the alert payload."""
    alert = state["alert"]
    return state.update(
        namespace=alert.get("labels", {}).get("namespace", "openshift-etcd"),
        cluster_ctx=alert.get("annotations", {}).get("cluster", ""),
        phase="diagnosis",
    )


# ── Diagnosis (sequential; cannot skip) ───────────────────────────────────


@action(reads=[], writes=["master_nodes_raw"])
def check_master_nodes(state: State) -> State:
    """`oc get nodes -l node-role.kubernetes.io/master=`"""
    out = _shell("oc get nodes -l node-role.kubernetes.io/master=")
    return state.update(master_nodes_raw=out)


@action(reads=[], writes=["upgrade_status_raw"])
def check_upgrade_status(state: State) -> State:
    """`oc adm upgrade` — primary signal for branch selection."""
    out = _shell("oc adm upgrade")
    return state.update(upgrade_status_raw=out)


@action(reads=[], writes=["mco_state_raw"])
def check_mco_state(state: State) -> State:
    """Inspect MCO activity via node template annotation."""
    out = _shell(
        "oc get nodes -l node-role.kubernetes.io/master= "
        "-o jsonpath='{.items[*].metadata.annotations.machineconfiguration\\.openshift\\.io/state}'"
    )
    return state.update(mco_state_raw=out)


@action(reads=["namespace"], writes=["etcd_health_raw", "quorum_member_count"])
def check_etcd_health(state: State) -> State:
    """`etcdctl endpoint health -w table` via oc rsh into the etcdctl container."""
    ns = state["namespace"]
    pod_lookup = _shell(
        f"oc get pod -l app=etcd -oname -n {ns} | awk -F/ 'NR==1{{print $2}}'"
    )
    pod = pod_lookup.strip()
    if not pod:
        return state.update(etcd_health_raw="ERROR: no etcd pod found", quorum_member_count=0)
    out = _shell(
        f"oc rsh -c etcdctl -n {ns} {pod} etcdctl endpoint health -w table"
    )
    healthy_count = out.lower().count("true")
    return state.update(etcd_health_raw=out, quorum_member_count=healthy_count)


# ── Classification gate (model fills `selected_mitigation`) ───────────────


@action(
    reads=["upgrade_status_raw", "mco_state_raw", "etcd_health_raw", "master_nodes_raw"],
    writes=["selected_mitigation", "rationale", "phase"],
)
def classify_situation(
    state: State, selected_mitigation: str, rationale: str
) -> State:
    """Agent records its read of the situation.

    Valid values for ``selected_mitigation``:

    - ``wait_for_upgrade``: an upgrade is in progress; expect self-resolution
      as nodes rejoin. No destructive action.
    - ``inspect_cloud_console``: no active upgrade; master instances may be
      down at the cloud provider level. External (manual) inspection.
    - ``manual_restart``: AWS instance retirement or equivalent; node must be
      restarted by hand. **Destructive.** Routes through approve_destructive.
    - ``unsure``: agent cannot confidently classify. Routes to escalation.

    The ``rationale`` is recorded for the audit trail and postmortem.
    """
    if selected_mitigation not in {
        "wait_for_upgrade",
        "inspect_cloud_console",
        "manual_restart",
        "unsure",
    }:
        raise ValueError(
            f"invalid selected_mitigation={selected_mitigation!r}; "
            f"must be one of: wait_for_upgrade, inspect_cloud_console, "
            f"manual_restart, unsure"
        )
    next_phase = {
        "wait_for_upgrade": "mitigation",
        "inspect_cloud_console": "mitigation",
        "manual_restart": "approval",
        "unsure": "escalation",
    }[selected_mitigation]
    return state.update(
        selected_mitigation=selected_mitigation,
        rationale=rationale,
        phase=next_phase,
    )


# ── Approval gate for the destructive path ───────────────────────────────


@action(
    reads=["selected_mitigation", "rationale", "quorum_member_count"],
    writes=["approval_decision", "approver_note", "phase"],
)
def approve_destructive(
    state: State, approval_decision: str, approver_note: str
) -> State:
    """Explicit gate before manual_restart.

    A manual node restart with quorum at 2/3 risks dropping to 1/3 and losing
    cluster availability entirely. The approver (human via elicit, or model
    under a stricter persona) must explicitly approve with a recorded note.
    """
    if approval_decision not in {"approved", "rejected"}:
        raise ValueError(
            f"invalid approval_decision={approval_decision!r}; "
            f"must be 'approved' or 'rejected'"
        )
    next_phase = "mitigation" if approval_decision == "approved" else "escalation"
    return state.update(
        approval_decision=approval_decision,
        approver_note=approver_note,
        phase=next_phase,
    )


# ── Mitigation branches (named per the prose paragraph) ──────────────────


@action(reads=["rationale"], writes=["mitigation_action_taken"])
def mitigation_wait_for_upgrade(state: State) -> State:
    """Record the decision to wait. No execution; the cluster self-resolves."""
    return state.update(mitigation_action_taken="waiting_for_upgrade_completion")


@action(reads=["rationale"], writes=["mitigation_action_taken"])
def mitigation_inspect_cloud_console(state: State) -> State:
    """Record the directive to inspect cloud provider console. External action."""
    return state.update(
        mitigation_action_taken="cloud_console_inspection_required"
    )


@action(
    reads=["approval_decision", "approver_note"],
    writes=["mitigation_action_taken", "restart_target"],
)
def mitigation_manual_restart(state: State, target_node: str) -> State:
    """Execute the destructive restart only if approval was recorded.

    Burr's transition guard already enforces approval; this body re-checks
    defensively in case state is forked-and-resumed.
    """
    if state["approval_decision"] != "approved":
        raise RuntimeError(
            "mitigation_manual_restart reached without approval recorded; "
            "transition guard failed"
        )
    _shell(f"oc adm cordon {target_node}")
    _shell(f"oc debug node/{target_node} -- chroot /host systemctl reboot")
    return state.update(
        mitigation_action_taken=f"manual_restart_executed:{target_node}",
        restart_target=target_node,
    )


# ── Verification (ADDED by converter; source has none) ───────────────────


@action(
    reads=["namespace"],
    writes=["verification_quorum", "phase"],
)
def verify_quorum_restored(state: State) -> State:
    """Re-check etcdctl health; declare resolved iff full quorum returned."""
    ns = state["namespace"]
    out = _shell(
        f"oc rsh -c etcdctl -n {ns} $(oc get pod -l app=etcd -oname -n {ns} | "
        f"awk -F/ 'NR==1{{print $2}}') etcdctl endpoint health -w table"
    )
    healthy = out.lower().count("true")
    resolved = healthy >= 3
    return state.update(
        verification_quorum=healthy,
        phase="done" if resolved else "escalation",
    )


# ── Terminals ────────────────────────────────────────────────────────────


@action(reads=["mitigation_action_taken", "verification_quorum"], writes=["resolution"])
def resolved(state: State) -> State:
    return state.update(
        resolution=f"recovered_via:{state['mitigation_action_taken']}"
    )


@action(
    reads=["selected_mitigation", "rationale", "approval_decision", "verification_quorum"],
    writes=["resolution"],
)
def escalate(state: State) -> State:
    return state.update(
        resolution=(
            f"escalated:reason="
            f"{state.get('selected_mitigation', 'pre_classification')};"
            f"approval={state.get('approval_decision', 'n/a')};"
            f"quorum_after={state.get('verification_quorum', 'unknown')}"
        )
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _shell(cmd: str) -> str:
    proc = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout
    return f"ERROR rc={proc.returncode}: {proc.stderr.strip()}"


# ── Builder ──────────────────────────────────────────────────────────────


def build_application(alert: dict[str, Any] | None = None):
    initial: dict[str, Any] = {
        "alert": alert or {},
        "namespace": "openshift-etcd",
        "cluster_ctx": "",
        "phase": "acknowledge",
        "master_nodes_raw": "",
        "upgrade_status_raw": "",
        "mco_state_raw": "",
        "etcd_health_raw": "",
        "quorum_member_count": 0,
        "selected_mitigation": "",
        "rationale": "",
        "approval_decision": "",
        "approver_note": "",
        "mitigation_action_taken": "",
        "restart_target": "",
        "verification_quorum": 0,
        "resolution": "",
    }
    return (
        ApplicationBuilder()
        .with_actions(
            acknowledge,
            check_master_nodes,
            check_upgrade_status,
            check_mco_state,
            check_etcd_health,
            classify_situation,
            approve_destructive,
            mitigation_wait_for_upgrade,
            mitigation_inspect_cloud_console,
            mitigation_manual_restart,
            verify_quorum_restored,
            resolved,
            escalate,
        )
        .with_transitions(
            ("acknowledge", "check_master_nodes"),
            ("check_master_nodes", "check_upgrade_status"),
            ("check_upgrade_status", "check_mco_state"),
            ("check_mco_state", "check_etcd_health"),
            ("check_etcd_health", "classify_situation"),
            # Classification routes to one of three phases via `phase` field
            (
                "classify_situation",
                "mitigation_wait_for_upgrade",
                expr("selected_mitigation == 'wait_for_upgrade'"),
            ),
            (
                "classify_situation",
                "mitigation_inspect_cloud_console",
                expr("selected_mitigation == 'inspect_cloud_console'"),
            ),
            (
                "classify_situation",
                "approve_destructive",
                expr("selected_mitigation == 'manual_restart'"),
            ),
            (
                "classify_situation",
                "escalate",
                expr("selected_mitigation == 'unsure'"),
            ),
            # Approval gate
            (
                "approve_destructive",
                "mitigation_manual_restart",
                expr("approval_decision == 'approved'"),
            ),
            (
                "approve_destructive",
                "escalate",
                expr("approval_decision == 'rejected'"),
            ),
            # All non-escalation mitigation paths converge on verification
            ("mitigation_wait_for_upgrade", "verify_quorum_restored"),
            ("mitigation_inspect_cloud_console", "verify_quorum_restored"),
            ("mitigation_manual_restart", "verify_quorum_restored"),
            # Verification branches
            ("verify_quorum_restored", "resolved", expr("phase == 'done'")),
            ("verify_quorum_restored", "escalate", expr("phase == 'escalation'")),
        )
        .with_state(**initial)
        .with_entrypoint("acknowledge")
        .with_tracker(LocalTrackingClient(project="etcd-members-down-runbook"))
        .build()
    )
