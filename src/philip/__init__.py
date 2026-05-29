"""philip: Ansible-module-backed Burr actions.

Ansible's vars, facts, and registered results form its state model. Burr's
``State`` is also a state model. philip exposes the alignment: Ansible
modules become Burr actions, ``gather_facts`` becomes state expansion,
``register:`` becomes a state-key projection, ``when:`` becomes a transition
predicate.

Mapping from Ansible-playbook idioms to philip / Burr idioms::

    Ansible playbook                  philip / Burr
    ------------------------------    -------------------------------------------
    gather_facts: yes                 host.gather_facts() flattens ansible_facts
                                      into top-level State keys
    vars: foo: bar                    ApplicationBuilder().with_state(foo="bar")
    host_vars/<host>.yml              fields on host() (connection vars today;
                                      domain vars on roadmap)
    set_fact: foo: bar                @action def f(state): return state.update(foo="bar")
    register: result_name             @module_action(register="result_name") or
                                      target.shell(register="...") etc.
    when: condition                   transition predicate: expr("condition")
    failed_when: X                    guard transition on expr("_last_failed") plus
                                      conditions on result fields written via writes=
    changed_when: X                   guard on expr("_last_changed") plus computed
                                      result fields
    block / rescue / always           guarded transition sub-graph; ``rescue:`` is an
                                      edge guarded by expr("_last_failed")
    notify: handler                   transition guarded by expr("_last_changed")
                                      to the handler action
    loop: items                       FSM iteration via state counter and back-edge
                                      (see examples/user_provisioning/)
    block validate-then-apply         see examples/config_drift/ (render with
                                      backup, validate via nginx -t, reload-if-ok,
                                      snapshot-on-failure rollback)

Two rows above required new philip primitives: ``gather_facts()`` for state
expansion and ``register=`` for full-result capture. The rest is supported by
Burr directly (``with_state``, ``@action``, ``expr``).
"""

from importlib.metadata import PackageNotFoundError, version

from philip._action import (
    FAILURE_KIND_AUTH_FAILED,
    FAILURE_KIND_MODULE_ERROR,
    FAILURE_KIND_OK,
    FAILURE_KIND_TIMEOUT,
    FAILURE_KIND_UNREACHABLE,
    FAILURE_KINDS,
    SENTINEL_KEYS,
    initial_sentinels,
    module_action,
    snapshot_sentinels,
)
from philip._convert import UnsupportedPlaybookConstruct, from_playbook, to_playbook
from philip._host import DEFAULT_FACT_KEYS, Host, host
from philip._inspect import (
    ActionFailureTopology,
    FailureEdge,
    InspectionReport,
    VariableDefinition,
    VariableProvenance,
    VariableUse,
    inspect,
)
from philip._lifters import MermaidLiftError, from_mermaid, from_mermaid_text
from philip._runner import run_module
from philip._wait import WaitGraph, wait_until

try:
    __version__ = version("philip-machine")
except PackageNotFoundError:
    # Running from a source checkout without an installed dist (e.g. tests in
    # CI before ``uv sync``). Fall back to a sentinel that's obviously not a
    # released version.
    __version__ = "0+unknown"

__all__ = [
    "DEFAULT_FACT_KEYS",
    "FAILURE_KINDS",
    "FAILURE_KIND_AUTH_FAILED",
    "FAILURE_KIND_MODULE_ERROR",
    "FAILURE_KIND_OK",
    "FAILURE_KIND_TIMEOUT",
    "FAILURE_KIND_UNREACHABLE",
    "SENTINEL_KEYS",
    "ActionFailureTopology",
    "FailureEdge",
    "Host",
    "InspectionReport",
    "MermaidLiftError",
    "UnsupportedPlaybookConstruct",
    "VariableDefinition",
    "VariableProvenance",
    "VariableUse",
    "WaitGraph",
    "__version__",
    "from_mermaid",
    "from_mermaid_text",
    "from_playbook",
    "host",
    "initial_sentinels",
    "inspect",
    "module_action",
    "run_module",
    "snapshot_sentinels",
    "to_playbook",
    "wait_until",
]
