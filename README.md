# Philip

Lift your Ansible playbook into a Burr state machine with audit, replay,
and structural introspection. Drive it under FSM gating with an MCP-mounted
agent via [Theodosia](https://github.com/msradam/theodosia), or run it
locally as a regular Python application.

```python
import philip

app = philip.from_playbook("site.yml")
last, _, state = app.run(halt_after=["done", "escalate"])

report = philip.inspect("site.yml")
print(report.rendered_markdown())
```

## Why

Today there are two choices for AI-operated infrastructure. Run a playbook
unattended (rigid, no judgment, can't adapt to context). Or let a free-form
agent run shell commands (no constraint, no audit). Both extremes are wrong
for most real ops work.

A third option exists. The playbook author writes the contract: which
steps are reachable, in what order, where verification must happen, which
failures route to recovery. The model (or human) operates within that
contract. It picks, interprets logs, refuses when unsure, composes with
other tools. It cannot invent steps, skip verification, or escape the
procedure.

Three independent sources validate this architecture:

- [STRATUS](https://arxiv.org/abs/2506.02009) (IBM Research, NeurIPS 2025)
  demonstrated FSM-organized SRE agents beat free-form ones by at least
  1.5x on AIOpsLab and ITBench.
- [ITBench](https://github.com/itbench-hub/ITBench) (IBM Research,
  Apache 2.0) is the open benchmark suite the STRATUS work was evaluated on.
- [Wikimedia's Spicerack](https://github.com/wikimedia/operations-software-spicerack)
  is the operational existence proof. The most transparent SRE organization
  in the public world looked at Ansible in 2014, decided it was not
  controllable enough for production response, and built a Python framework
  with explicit phases and structural error handling from scratch.

Burr is the structured-Python substrate Spicerack would have used if it
existed in 2014. Philip lifts your existing Ansible playbooks onto that
substrate without rewriting anything.

## Install

```bash
uv add philip-machine
```

Pulls `ansible-core` and `ansible-runner` transitively. Install Ansible
collections via `ansible-galaxy` for your modules of choice:

```bash
ansible-galaxy collection install community.general community.docker ansible.posix
```

## What you get over `ansible-playbook`

| Situation | `ansible-playbook` | Philip + Burr |
|---|---|---|
| Mid-procedure decisions on runtime context | `when:` over already-known vars | Explicit transitions; an agent or human picks based on full state |
| Resume from arbitrary point | `--start-at-task` hack; registers lost | Burr persister rebuilds full state including registers |
| Approval gates | `pause:` is stdin-bound; AWX bolt-on | First-class states; approver acts through the same surface as any actor |
| Auditable rationale | Task logs only | Every transition logged with actor's choice and state snapshot |
| Counterfactual replay | None | `fork_at(sequence_id)` walks the alternate path |
| Composition with non-Ansible work | Shell out and pray | Coordinate Ansible steps alongside any Python in the same audit surface |
| Refusal as a structural action | Run or fail | First-class refusal transitions; "I don't know, escalating" routes structurally |

`ansible-playbook` remains correct for deterministic, unattended, batch
runs. Philip is the right tool when the playbook has decision points,
needs verification gates, must be auditable for postmortem, or composes
with non-Ansible work.

## Supported subset

Philip lifts a defined subset of Ansible playbook syntax. Supported:

- One play per file (multi-play raises `UnsupportedPlaybookConstruct`).
- Tasks with `name`, exactly one module reference, and a module-args dict.
- `when:` predicates (string expressions; lists are AND-joined).
- `register:` capturing the full module result into a state key.
- `become:` per-task or per-play.
- `failed_when:` and `ignore_errors:` as guard transitions.
- `gather_facts: yes` lowers to a leading `ansible.builtin.setup` action.
- Play-level `vars:` populate `with_state(...)`.
- `block:` (group-only) inlines its tasks with the block's `when:`
  AND-propagated to each inner task.
- `include_tasks:` and `import_tasks:` with literal filesystem paths.
- `notify:` and `handlers:` with a deferred handler gated on `_last_changed`.
- `loop:` and `with_items:` with literal list values.
- `set_fact:` (bare and FQCN).

The following raise `UnsupportedPlaybookConstruct`:

- `rescue:` and `always:` (deferred to a later release).
- `loop_control:`, `with_dict:`, `with_fileglob:`, `with_subelements:`.
- Jinja-templated `loop:` values.
- Jinja-templated `include_tasks:` paths.
- `import_role:`, `include_role:`, `include:`.
- `roles:` blocks, `pre_tasks:`, `post_tasks:`.
- `serial:`, `strategy:`, `max_fail_percentage:`, `any_errors_fatal:`.
- Multi-play files.

The supported subset covers the common single-play remediation and day-2
procedures the FSM lift adds value to.

## Compose with Theodosia

[Theodosia](https://github.com/msradam/theodosia) mounts a Burr
`Application` as an MCP server. Philip lifts an Ansible playbook to a
Burr `Application`. The composition is two lines:

```python
import philip
import theodosia

theodosia.mount(philip.from_playbook("site.yml"), name="nginx-deploy").run()
```

Now any MCP client (Claude Code, Cursor, fast-agent, mcphost, a custom
agent built on the Agent SDK) can drive the playbook step by step. The
FSM enforces structural constraints. The model picks transitions at
branch points. Every step is audit-trailed. Forking at any sequence_id
gives you counterfactual replay for postmortem.

Theodosia and Philip are independent packages. Philip does not depend
on Theodosia.

## CLI

```bash
philip run     <playbook.yml>                 # execute end to end
philip inspect <playbook.yml>                 # static report: variables + failure topology
philip inspect <playbook.yml> --format json
philip graph   <playbook.yml> [--format mermaid|dot|text]
philip lint    <playbook.yml>                 # dry-convert with structural summary
philip emit    <playbook.yml>                 # round-trip lift -> emit canonical YAML
```

## API

```python
import philip

# Lift
app = philip.from_playbook("site.yml")
yaml_text = philip.to_playbook(app)

# Static introspection
report = philip.inspect("site.yml")
report.variables                 # variable provenance DAG
report.undefined_variables       # references with no defining site
report.unused_definitions        # bound but never referenced
report.failure_topology          # per-action FAILURE_KIND routing
report.actions_with_recovery     # actions with a true recovery branch
report.unhandled_failures        # actions where a failure has no transition
report.rendered_markdown()       # human-readable report

# Hand-write actions backed by Ansible modules
@philip.module_action("ansible.builtin.command", reads=["target"], writes=["uptime"])
def get_uptime(state):
    return {"cmd": "uptime"}

# Or call modules directly
result = philip.run_module("ansible.builtin.ping", {}, host="myhost")

# Connection management
host = philip.host(ansible_host="example.com", ansible_user="deploy")

# Polling sub-graphs
wait = philip.wait_until(
    "ansible.builtin.wait_for",
    args={"port": 8080, "timeout": 1},
    max_attempts=30,
)
```

## Failure classification

Every module-backed action writes structural failure sentinels into state
on each step:

- `_last_action`
- `_last_failed`
- `_last_changed`
- `_last_unreachable`
- `_last_msg`
- `_last_failure_kind` (one of `ok`, `unreachable`, `auth_failed`,
  `timeout`, `module_error`)

Transitions branch on these without each action having to opt in via
`writes=`. The classification is conservative and pattern-based: the
`unreachable` and `failed` result flags are trusted; the diagnostic `msg`
is scanned for known phrases that indicate auth and timeout. The labels
are loosely aligned with the
[MAST](https://arxiv.org/abs/2503.13657) taxonomy.

## License

Apache 2.0.

## Development

LLM-assisted development was used during construction.
