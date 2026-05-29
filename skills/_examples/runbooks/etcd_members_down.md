# Source: openshift/runbooks etcdMembersDown.md (paraphrased)

Original at: https://github.com/openshift/runbooks/blob/master/alerts/cluster-etcd-operator/etcdMembersDown.md

## Overview
Activates when etcd members become unavailable. Frequently occurs during cluster upgrades requiring master node reboots.

## Key Impact
"In etcd a majority of (n/2)+1 has to agree on membership changes" to prevent split-brain. A 3-member cluster tolerates one member being down, but loses consensus with additional failures.

## Diagnostic Steps
1. Check master node status: `oc get nodes -l node-role.kubernetes.io/master=`
2. Verify upgrade status: `oc adm upgrade`
3. Inspect machine configuration changes (MCO activity)
4. Test etcd health: `oc rsh -c etcdctl -n openshift-etcd <pod>` then `etcdctl endpoint health -w table`

## Resolution Approach (PROSE-BRANCHED — this is the FSM-justifying section)
Ongoing upgrades typically self-resolve when nodes rejoin. For scenarios without active
upgrades, examine whether master instances are operational through your cloud provider's
console. AWS environments may require manual node restarts due to instance retirement policies.

# Why this benefits from FSM (score: 5/7)
- Branches: 3 distinct mitigations gated on diagnosis output (ongoing_upgrade / cloud_console_inspection / manual_restart)
- Approval gate: manual restart is destructive (could break quorum further); needs explicit gate
- Verification: source has NONE; converter adds re-check after mitigation
- Failure modes: distinct paths for ongoing-upgrade-stuck vs cloud-instance-retired vs unknown
- Retry/polling: NO
- Non-Ansible composition: YES — etcdctl is structurally different from oc/kubectl
- Counterfactual replay: YES — "should we have waited longer instead of restarting?"
