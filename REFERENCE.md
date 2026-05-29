# philip reference

## CLI

```
philip run    <path> [--halt-after ACTION ...]
philip graph  <path> [--format {mermaid,dot,text}]
philip lint   <path>
philip emit   <path>
```

`<path>` is either a `.yml`/`.yaml` playbook (lifted via `from_playbook`) or a `.py` file that exposes either a module-level `app` Application or a `build_application()` callable. `run` halts on `done` or `escalate` by default. `graph` defaults to mermaid output. `lint` operates only on YAML playbooks; it dry-runs `from_playbook` and prints a structural summary (action counts, per-bucket breakdown of what got lowered) without executing anything. Exit code 0 on clean conversion, 1 on `UnsupportedPlaybookConstruct`, 2 on other errors.

## Library

- `module_action(module, reads, writes, register, host, connection, become, check_mode, diff, timeout)`. The core decorator. Wraps a function returning module args into a Burr `@action` that invokes the module via ansible-runner. `timeout` caps the per-call ansible-playbook runtime (default 300s).
- `host(name, **hostvars)`. Connection profile. Captures Ansible hostvars plus `become` once; exposes `.module(fqcn, ...)` and shorthands (`.service`, `.copy`, `.shell`, `.command`, `.template`, `.file`, `.find`, `.slurp`, `.uri`, `.systemd`).
- `host.gather_facts()`. Runs `ansible.builtin.setup`. Flattens facts into top-level State keys; the full dict lands at `state['facts']`.
- `host.initial_facts()`. Placeholder seeds for `with_state(...)`, so transitions can read fact keys before the gather has executed.
- `initial_sentinels()`. Placeholder seeds for the ambient `_last_*` keys (`_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`, `_last_failure_kind`).
- `snapshot_sentinels(write="failure_reason")`. Pure-Python `@action` that persists the current sentinels into a durable state key. Used when a recovery action would otherwise overwrite the original failure diagnostic.
- `wait_until(name, check, condition_expr, max_attempts, interval_s, on_success, on_timeout)`. Polling sub-graph builder. Returns a `WaitGraph(actions, transitions, entry, initial_state)` to merge into an `ApplicationBuilder`. Each attempt is one Burr step.
- `from_playbook(path, *, project=None)`. Parses a YAML playbook and returns a runnable `Application`. See the supported / rejected lists below.
- `run_module(module, args, host, connection, become, check_mode, diff, timeout)`. The underlying runner, exposed for callers that want the raw ansible-runner call without the decorator.
- `FAILURE_KIND_OK`, `FAILURE_KIND_UNREACHABLE`, `FAILURE_KIND_AUTH_FAILED`, `FAILURE_KIND_TIMEOUT`, `FAILURE_KIND_MODULE_ERROR` (plus the `FAILURE_KINDS` tuple). String constants for `_last_failure_kind` values. Use them in transition predicates rather than string literals.

Every `@module_action` writes the ambient sentinels: `_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`, `_last_failure_kind`. Burr's tracker captures the full Ansible module result dict per step, so the trace shows `stdout`, `stderr`, `rc`, `diff`, `changed`, and any module-specific fields alongside the state snapshot.

## Environment variables

- `PHILIP_DEBUG=1`. Disables ansible-runner's quiet mode; the underlying `ansible-playbook` stdout and stderr stream to the controller. Use when a module crashes in a way the event stream doesn't capture cleanly.

## `from_playbook` supported constructs

| Playbook idiom | Lowering |
|---|---|
| `gather_facts: yes` | leading `ansible.builtin.setup` action |
| play-level `vars:` | seeded into `with_state(...)` |
| `set_fact: { k: v }` | pure-Python `@action` doing `state.update(**rendered)` |
| `register: name` | full module result dict captured at `state[name]` |
| `when: cond` | transition predicate; consecutive false `when:`s are chain-skipped |
| `when: result.attr == 0` | rewritten to `result['attr'] == 0` so Python `eval` accepts it |
| `failed_when: cond` | guard transition to `escalate` |
| `changed_when: cond` (or `false`) | post-action overrides `_last_changed` against the predicate |
| `ignore_errors: yes` | suppresses the failure -> escalate edge for that task |
| `become:` per-task or per-play | threaded through `module_action(..., become=...)` |
| `block:` (group-only) | inlined; block's `when:` AND-propagated to each inner task |
| `include_tasks: file.yml` / `import_tasks:` | referenced file's tasks inlined at conversion; outer `when:`/`notify:` propagate |
| `notify: handler_name` | synthesized marker flips `_notified_<slug>` when `_last_changed`; handler appended after main tasks, gated on the flag |
| `handlers:` (top-level on the play) | each handler becomes a regular task with `when: _notified_<slug>` |
| `loop:` / `with_items:` (literal list) | three-action sub-FSM (init -> body -> advance, back-edge until exhausted); body sees `{{ item }}` |
| Jinja templates in args | rendered against play vars + registered values + non-internal state keys |
| `wait_for: ...` | hand-write via `philip.wait_until(...)`; `from_playbook` does not lower `wait_for` modules specially |

## `from_playbook` rejected constructs

These raise `UnsupportedPlaybookConstruct` at convert time with the offending node identified:

- `rescue:` / `always:` (planned: STRATUS-style undo/transactional edges)
- `loop_control:`, `with_dict`, `with_fileglob`, `with_subelements`
- Jinja-templated `loop:` / `with_items:` values
- Jinja-templated `include_tasks:` / `import_tasks:` paths
- `import_role:`, `include_role:`, `include:`
- `roles:` blocks
- `pre_tasks:` / `post_tasks:`
- `serial:`, `strategy:`, `max_fail_percentage:`, `any_errors_fatal:`
- Multi-play files

## MAST failure classification

Loosely aligned with the MAST taxonomy (arXiv:2503.13657), `_last_failure_kind` carries one of:

| Value | When |
|---|---|
| `ok` | the module completed without failing |
| `unreachable` | ansible flagged the host unreachable |
| `auth_failed` | `msg` matches permission/auth/publickey-denied phrases |
| `timeout` | the per-call timeout cap fired and ansible-playbook was killed |
| `module_error` | anything else that failed |

Transitions can branch on the kind: retry on `unreachable`, escalate immediately on `auth_failed`, fall back to an alternative module on `module_error`, raise the cap next try on `timeout`. Without this, FSMs had to grep `_last_msg` to distinguish recovery paths.
