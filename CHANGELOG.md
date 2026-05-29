# Changelog

All notable changes to philip are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-05-28

### Changed

- Project renamed from ``ansiburr`` to ``philip``. Apache Burr trademark
  resolved by leaving the Burr name out. Sibling library to
  [Theodosia](https://github.com/msradam/theodosia) in the next-generation
  Hamilton family.
- PyPI distribution name is ``philip-fsm`` (the bare ``philip`` was taken).
  Python import remains ``philip``. CLI command remains ``philip``.
- Public API surface stable. All previous ``ansiburr.X`` imports become
  ``philip.X``. Callers update one import line.

### Added

- ``philip.inspect(playbook_path)`` returns an ``InspectionReport`` with
  two static analyses: variable provenance (every ``{{ var }}`` reference
  traced to its defining sites across playbook ``vars:``, role
  ``defaults/main.yml``, role ``vars/main.yml``, ``set_fact``, ``register``,
  and caller ``extra_vars``) and failure topology (per-action
  ``FAILURE_KIND`` routing on the lifted FSM, flagging unhandled failures
  and actions with true recovery branches).
- ``InspectionReport.rendered_markdown()`` for a single human-readable
  report.
- ``philip inspect <playbook.yml>`` CLI subcommand with ``--format
  markdown|json``.
- Public dataclasses: ``InspectionReport``, ``VariableProvenance``,
  ``VariableDefinition``, ``VariableUse``, ``ActionFailureTopology``,
  ``FailureEdge``.
- README rewritten with the three-source validation (STRATUS, ITBench,
  Wikimedia Spicerack) and the Theodosia composition recipe.

### Migration from ansiburr 0.0.x

```python
# before
import ansiburr
app = ansiburr.from_playbook("site.yml")

# after
import philip
app = philip.from_playbook("site.yml")
```

```toml
# pyproject.toml
# before
dependencies = ["ansiburr>=0.0.26"]
# after
dependencies = ["philip-fsm>=1.0"]
```

The ``ansiburr`` distribution on PyPI will ship one final 0.0.27 release
whose ``__init__.py`` raises ``ImportError`` pointing to ``philip-fsm``.

## [0.0.26] - 2026-05-21

### Changed

- CI workflow runs ``vulture`` (dead-code detection) and ``refurb``
  (modernization suggestions) on every push and pull request. They join
  the existing ruff + mypy + pytest gate, so future hygiene regressions
  fail the build instead of silently accumulating.
- Refurb is invoked with ``--ignore 184`` because the one finding
  (FURB184 on ``args = parse_args(...); args.func(args)`` in cli.py)
  can't be chained without parsing argv twice.
- ``dict(state_dict)`` in the changed_when evaluator switched to
  ``state_dict.copy()`` (FURB123 modernization). Burr's
  ``State.get_all()`` returns a dict so ``.copy()`` is safe.

## [0.0.25] - 2026-05-21

Refactor / hygiene release. No behavior change; all 61 tests pass and all
six geerlingguy roles still convert with identical action counts.

### Changed

- ``from_playbook`` refactored: the four big dispatch branches (block +
  rescue + always, block + always, block + rescue, loop) extracted as
  inner functions named ``_emit_block_rescue_always``,
  ``_emit_block_always``, ``_emit_block_rescue``, and ``_emit_loop_task``.
  Plus ``_allocate_block_subgraph_names`` for the repeated py-name
  allocation pattern across block variants.
- Cyclomatic complexity of ``from_playbook`` dropped from 160 to 119
  (still F-grade, but a 25% reduction). ``_convert.py``'s maintainability
  index rose from 2.41 to 8.56 (3.5x improvement; still C-grade).
- Consolidated three near-identical ``_record_inner*`` helpers (one per
  block variant) into a single ``_record_block_inner_task`` defined at
  ``from_playbook`` scope.

### Tooling

- Added ``vulture``, ``radon``, and ``refurb`` to the dev dependency
  group so the same hygiene checks run locally as in CI.
- Fixed two vulture findings (unused ``signum`` / ``frame`` parameters
  in the SIGINT handler, renamed to ``_signum`` / ``_frame``).
- Fixed six refurb findings (``list(x)`` -> ``x.copy()`` style
  modernizations, plus one ``{**a, **b}`` -> ``a | b`` dict-merge).
- Refurb still flags one false positive in ``cli.py``
  (``args = parser.parse_args(...)`` then ``return int(args.func(args))``,
  which can't be chained because ``args`` is used twice). Left as-is.

## [0.0.24] - 2026-05-21

### Added

- ``philip.to_playbook(app)`` returns the canonical YAML for an
  ``Application`` that was loaded via ``from_playbook(...)``. The
  YAML is reformatted via ``yaml.safe_dump`` so the output is
  consistent across runs (sorted keys, normalized indentation). Use
  it to normalize a playbook, to diff against a hand-edited version,
  or as part of a YAML -> Application -> YAML round-trip pipeline.
  Raises ``ValueError`` for Applications that weren't loaded via
  ``from_playbook`` (e.g. hand-authored FSMs).
- ``philip emit <playbook.yml>`` CLI subcommand runs the round-trip
  and writes the canonical YAML to stdout. Useful as a one-line
  normalizer or for inspecting how philip internally stores the
  playbook source.
- Two new tests: a YAML -> Application -> YAML -> dict round-trip
  reproduces the original play structure exactly; a hand-authored
  Application's ``to_playbook`` raises ``ValueError`` with a clear
  message about the limitation.

### Scope

- This is the scoped reverse-conversion case: ``to_playbook`` only
  works for Applications that originated from ``from_playbook``.
  Programmatic FSM-to-YAML synthesis (walking the graph and
  reconstructing tasks) is a different and substantially larger
  problem; not yet implemented.

## [0.0.23] - 2026-05-21

### Added

- ``philip lint <playbook>`` subcommand. Dry-runs ``from_playbook``
  and reports a structural summary without executing anything. On
  success, prints the action and transition counts plus a per-bucket
  breakdown showing how many actions are module/python tasks vs
  loop-init synthesized vs notify markers vs changed_when posts vs
  block save/restore actions vs terminals vs handlers. On
  ``UnsupportedPlaybookConstruct``, names the blocking construct
  and exits 1. On any other error, prints the exception type +
  message and exits 2.

  Example against the converted geerling docker role::

      $ philip lint /tmp/geerling-docker.yml
      philip lint  geerling-docker.yml: OK
        actions:     63
        transitions: 205

        breakdown:
          module + python tasks              41
          loop init/advance (auto)            4
          notify markers (auto)               9
          changed_when posts (auto)           5
          terminals (auto)                    2
          handlers                            2

- Three new CLI tests covering the OK path, the REJECTED path, and the
  refusal to lint Python files.

## [0.0.22] - 2026-05-21

### Changed

- ``run_module`` now reuses a single process-wide ``private_data_dir``
  across calls instead of creating and tearing down a fresh tempdir for
  each module invocation. Saves roughly 20ms per call from filesystem
  materialization overhead (mkdir, write inventory, write playbook,
  rmtree). The inventory and project subdirs are wiped between calls so
  no stale content leaks. The dir itself is cleaned up at interpreter
  exit via ``atexit``. Measured savings: ~3% of wall-clock latency per
  ping call (612ms persistent vs 633ms per-call tempdir); the absolute
  ansible-runner overhead dominates either way, but the savings compound
  on long FSMs.
- ``PHILIP_NO_REUSE_DATA_DIR=1`` falls back to per-call tempdirs.
  Useful for debugging or when multiple Applications run concurrently
  from the same process (which the runner module docstring already
  flags as unsupported).

## [0.0.21] - 2026-05-21

The last geerling holdout closes. **All six** of the most-downloaded
geerlingguy roles now convert from their unmodified published source.

  ansible-role-docker      63 actions, 205 transitions
  ansible-role-nginx       40 actions, 153 transitions
  ansible-role-mysql       78 actions, 265 transitions
  ansible-role-postgresql  63 actions, 186 transitions
  ansible-role-redis       13 actions,  27 transitions
  ansible-role-php        101 actions, 312 transitions

### Added

- Converter: best-effort Jinja-expression-to-Python translation for
  ``when:`` and ``failed_when:`` clauses. Supports:
  - ``X is defined`` -> ``(X is not None)``
  - ``X is not defined`` -> ``(X is None)``
  - ``X | length`` -> ``len(X)``
  - ``X | list`` -> ``list(X)``
  - ``X | first`` / ``| last`` -> ``[0]`` / ``[-1]``
  - ``X | upper`` / ``| lower`` -> ``.upper()`` / ``.lower()``
  - ``X | int`` / ``| string`` -> ``int(X)`` / ``str(X)``
  - ``X | default(Y)`` -> ``(X if X is not None else Y)``
  Chained filters collapse (``X | list | length`` -> ``len(list(X))``).
- Numeric dot access in register chains (``item.1.path``) is now
  translated to integer indexing (``item[1]['path']``), matching how
  Ansible interprets ``item.<number>``. The implicit ``item`` loop
  variable is always treated as a known register.

### Tests

- New positive test exercises a ``when:`` with ``| length``, ``| first``,
  ``| default``, and ``is defined`` against play-level vars and resolves
  cleanly.

## [0.0.20] - 2026-05-21

### Documented

- README "From an existing playbook" section now carries the geerling
  conversion table on the front page. 5/6 of the most-downloaded
  geerlingguy roles convert from their unmodified published source.
  Visible on the PyPI project page from this release onward.

## [0.0.19] - 2026-05-21

### Added

- Converter: ``with_subelements:`` (nested-iteration lookup) lowers to
  the same three-action loop sub-FSM ``loop:`` uses. The 2-element list
  ``with_subelements: ["{{ parents_template }}", subkey]`` is flattened
  at task time into a list of ``[parent, sub]`` items so the loop body
  sees ``item.0`` = parent and ``item.1`` = sub, matching Ansible.
  Geerlingguy php's ``configure-opcache`` and ``configure-apcu`` tasks
  use this idiom.

### Geerlingguy role conversion status

| Role | Status |
|---|---|
| ansible-role-docker | 63 actions, 205 transitions |
| ansible-role-nginx | 40 actions, 153 transitions |
| ansible-role-mysql | 78 actions, 265 transitions |
| ansible-role-postgresql | 63 actions, 186 transitions |
| ansible-role-redis | 13 actions, 27 transitions |
| ansible-role-php | structural conversion succeeds (with_subelements is now lowered); failure has moved to a different layer: a ``when:`` clause uses Jinja filter syntax (``\| last``) that Python's ``eval`` can't parse |

The remaining php blocker is the Jinja-vs-Python expression grammar
mismatch in ``when:`` clauses, which is a different class of work
than the structural lowerings shipped in v0.0.4-v0.0.19.

## [0.0.18] - 2026-05-21

Five of the six most-downloaded geerlingguy roles now convert:

  ansible-role-docker      63 actions, 205 transitions
  ansible-role-nginx       40 actions, 153 transitions
  ansible-role-mysql       78 actions, 265 transitions
  ansible-role-postgresql  63 actions, 186 transitions
  ansible-role-redis       13 actions,  27 transitions

ansible-role-php is the last holdout, blocked on ``with_subelements:``
(nested-iteration lookup).

### Added

- ``loop_control:`` (Ansible's loop modifier with ``label:``, ``pause:``,
  ``extended:`` etc.) moved from unsupported to reserved. The fields
  are purely cosmetic (output labels and pacing) and don't affect the
  iteration semantics philip emits, so silently ignoring them lets
  the surrounding ``loop:`` convert.

## [0.0.17] - 2026-05-21

Four of the six most-downloaded geerlingguy roles now convert cleanly
(docker, nginx, mysql, redis). Postgresql and php block on
``loop_control:`` and ``with_subelements:`` respectively.

### Added

- Converter: ``include_vars: "{{ ansible_facts.os_family }}.yml"``
  (Jinja-templated path) lowers to a resolver that renders the
  template against state at task time and searches in
  ``base_dir``, ``base_dir/vars``, and ``base_dir/../vars`` for the
  first existing file. Geerlingguy nginx, postgresql, redis, and php
  all open with this pattern.
- Converter: ``with_first_found:`` paired with
  ``include_vars: "{{ item }}"`` lowers to the same first_found
  resolver as the ``lookup('first_found', params)`` form. Geerlingguy
  mysql and php use this older idiom.
- ``environment:`` is now in the reserved task-key set, so tasks that
  set environment variables on the module (geerlingguy nginx
  setup-FreeBSD.yml uses this) convert without error.
- Include-vars resolvers (templated path, first_found, and with_first_found)
  now fill declared-but-unset state keys with their current values, so
  Burr's reducer-writes check passes even when the chosen file is
  missing keys the union scan declared.

### Still rejected

- ``loop_control:`` (postgresql uses this for ``label:`` modifiers
  on a loop).
- ``with_subelements:`` (php uses this for nested iteration).

54/54 tests pass.

## [0.0.16] - 2026-05-21

The combination of changes in this release is what lets the converter
actually ingest a real published geerlingguy role end-to-end. Tested
against ``geerlingguy.ansible-role-docker`` (Galaxy's most-downloaded
container role): converts cleanly into a 63-action Burr Application.

### Added

- Converter: ``loop:`` and ``with_items:`` accept Jinja templates
  (``loop: "{{ docker_users }}"``) in addition to literal lists. The
  template is resolved against current state at the loop_init step;
  the rendered result is parsed as a Python list (or ``ast.literal_eval``-ed
  when Jinja returns a string repr). Empty lists are valid; the done
  flag flips to True immediately.
- Converter: ``include_vars: "{{ lookup('first_found', params) }}"``
  with a task-level ``vars:`` block defining ``params``. The action
  built for this case resolves the params dict (files + paths) against
  state at task time and loads the first existing YAML file. Pre-scans
  every YAML under the paths dirs at conversion time to declare the
  write set Burr requires up front. This is the dominant geerlingguy
  pattern for OS-conditional vars file selection.
- Converter: task-level ``vars:`` blocks are now made available in the
  Jinja context for the task they're attached to (currently only the
  first_found resolver consumes them; the wiring opens the door to
  broader task-vars support later).

### Tests

- Two new positive tests: a templated loop reading from play-level
  vars iterates correctly; an empty templated loop completes
  immediately. The negative ``test_jinja_templated_loop_still_raises``
  was repurposed.

## [0.0.15] - 2026-05-21

### Added

- Converter: ``gather_facts: yes`` now also surfaces ``ansible_facts``
  as a top-level state dict, matching real-playbook idiom. A converted
  task with ``when: ansible_facts.os_family == 'Debian'`` (the dominant
  pattern in geerlingguy's roles) resolves correctly because the
  dot-access translator rewrites it to ``ansible_facts['os_family']``
  and the state dict is now populated where expected. The full module
  result remains available at ``gathered_facts`` for callers that want
  diagnostic fields like ``stdout`` or ``changed``.
- ``ansible_facts`` and ``gathered_facts`` are added to the converter's
  known-register set so the dot-access translator handles them
  uniformly with user-declared registers.

## [0.0.14] - 2026-05-21

### Added

- Converter: ``include_vars: file: <path>`` (and the FQCN
  ``ansible.builtin.include_vars:`` form) lowers to a pure-Python
  action that reads the referenced YAML at task execution and writes
  every top-level key into Burr state. The ``name:`` argument
  (Ansible's namespacing form) lands the whole dict under
  ``state[<name>]`` instead of spreading the keys. Found while testing
  the converter against geerlingguy's published roles; their
  ``include_vars`` calls open every dispatch chain.

### Still rejected

- ``include_vars`` with ``lookup(...)`` expressions or Jinja-templated
  paths. The ``first_found`` lookup pattern that geerlingguy roles use
  for OS-conditional dispatch needs runtime evaluation we don't
  implement. Workaround: replace with a literal-path
  ``include_vars: file: ...`` per branch under explicit ``when:`` gates.

## [0.0.13] - 2026-05-21

### Added

- ``examples/from_playbook_role/``: a multi-file role-style demo with
  ``main.yml`` + ``tasks/setup-debian.yml`` + ``tasks/setup-redhat.yml``.
  Mirrors the structural shape every popular community Ansible role
  (geerlingguy.mysql, .docker, .postgresql, .nginx) opens with: an
  OS-conditional ``include_tasks`` dispatch to a setup-<distro>.yml
  file. Validates the converter's cross-file include path against a
  realistic playbook shape.

### Fixed

- Converter: ``ansible.builtin.include_tasks:`` and
  ``ansible.builtin.import_tasks:`` (FQCN form, what modern roles use)
  are now recognized in addition to the bare ``include_tasks:`` /
  ``import_tasks:`` form. The community roles surveyed in v0.0.4 all
  use the FQCN form, so without this the conversion silently treated
  them as task modules instead of including them.
- Converter: ``set_fact:`` tasks with ``notify:`` now correctly insert
  a notify-marker so the handler fires. Previously the set_fact
  early-return skipped the notify-handling logic.
- Converter: ``set_fact:`` actions now write ``_last_changed=True`` to
  match Ansible's "set_fact always reports changed" semantics. This
  makes ``notify:`` on a set_fact behave as it would in a real
  playbook.

## [0.0.12] - 2026-05-21

Closes out the block/rescue/always trilogy. Every standard Ansible
error-handling shape now lowers cleanly into the FSM.

### Added

- Converter: ``block: + rescue: + always:`` (the full three-clause
  shape) lowers into a coordinated sub-graph combining the v0.0.10 and
  v0.0.11 lowerings. Block failure routes to rescue (as in v0.0.10);
  rescue failure routes to the failure-latch action so the failure
  survives the subsequent always chain (as in v0.0.11); rescue success
  routes through the clear-failure action then through the latch
  (no-op when nothing is latched) then to always; always runs
  unconditionally; restore-failure after always re-applies a latched
  rescue-failure so it escalates after the always chain finishes.
- Two new tests covering the happy path (block fails, rescue succeeds,
  always runs, FSM reaches done) and the double-failure path (block
  fails, rescue fails, always still runs, FSM reaches escalate with
  ``_last_failed`` restored).

### Still rejected

- Nested block within block+rescue, block+always, or the triple combo.
- ``loop:`` / ``with_items:``, ``notify:``, ``changed_when:`` inside
  block, rescue, or always. (These features all work outside.)

The "still rejected" list of Ansible idioms is now down to advanced
constructs and edge cases. Every popular community-role pattern from
the v0.0.4 research pass is supported.

## [0.0.11] - 2026-05-21

### Added

- Converter: ``block: + always:`` round-trip. Block tasks execute as
  usual; whether the block succeeds or fails, the ``always:`` chain
  runs unconditionally; block failures are latched into a sticky state
  field so they propagate to ``escalate`` only after the always chain
  finishes (matching Ansible semantics: always runs no matter what,
  failure surfaces afterward).
- Synthesized ``_block_save_failure`` and ``_block_restore_failure``
  actions sandwich the always chain. Save captures ``_last_failed``
  into a per-block flag before always runs (which would otherwise
  reset the sentinels via the success path). Restore re-applies the
  flag's value to ``_last_failed`` after the always chain finishes, so
  the standard escalate edge fires when needed.

### Still rejected

- ``block: + rescue: + always:`` (all three) still raises. The path
  through rescue plus always needs a coordinated lowering of both the
  rescue-clear and the always-restore. Planned for v0.0.12.
- Nested block within a block+always (or block+rescue) raises.
- ``loop:`` / ``with_items:``, ``notify:``, ``changed_when:`` inside a
  block, rescue, or always still raise.

### Tests

- Two new tests: ``block + always`` with successful block runs every
  always task; ``block + always`` with failed block still runs every
  always task and then escalates with ``_last_failed`` restored.
- ``test_block_with_always_still_raises`` renamed to
  ``test_block_rescue_plus_always_still_raises`` since the only
  still-rejected combination is all three together.

## [0.0.10] - 2026-05-21

### Added

- Converter: ``block: + rescue:`` round-trip. Block tasks route to the
  rescue chain on failure (rather than to ``escalate``); the
  rescue chain runs with normal escalate routing for its own failures;
  after the rescue chain completes successfully, a synthesized
  ``_block_clear_failure`` action wipes ``_last_failed``, ``_last_msg``,
  ``_last_unreachable``, and ``_last_failure_kind`` so downstream tasks
  do not see stale failure state.
- The lowering matches Ansible's semantics: a deliberate
  ``ansible.builtin.fail`` inside ``block:`` is "rescued" by the
  ``rescue:`` chain, and post-block tasks run normally afterward.
  Aligns conceptually with the STRATUS Transactional No-Regression
  property (NeurIPS 2025, arXiv:2506.02009).
- Block-inner ``set_fact:`` works (e.g. ``set_fact: rescued: true`` in a
  rescue task to record that recovery ran).

### Still rejected

- ``always:`` clauses still raise ``UnsupportedPlaybookConstruct``
  (planned for v0.0.11; the deferred-failure semantics need a different
  lowering than rescue).
- Nested ``block + rescue`` raises ``UnsupportedPlaybookConstruct``.
- ``loop:`` / ``with_items:``, ``notify:``, ``changed_when:`` inside a
  block or rescue raise. These features all work outside block/rescue;
  combining them is planned for v0.0.11.

### Tests

- New positive test exercises a 3-task block whose middle task
  deliberately fails, a rescue chain that sets ``rescued=True``, and a
  post-block task that runs cleanly because the failure sentinels were
  cleared.
- ``test_unsupported_block_raises`` renamed to
  ``test_block_with_always_still_raises`` and the fixture updated to
  use ``always:`` (rescue is now supported).

## [0.0.9] - 2026-05-21

### Added

- ``examples/from_playbook_walker.py``: a colored step-by-step walker
  that runs ``philip.from_playbook(...)`` on the advanced demo playbook
  and prints each emitted action as a discrete observable step. Renders
  the conversion story at the command line: set_fact lowering, block
  expansion, loop iteration, notify-marker, changed_when post-action,
  handler firing, all visible.
- ``vhs/conversion.gif`` + ``vhs/conversion.tape``: VHS recording of the
  walker against the advanced playbook. 17 distinct FSM steps from a
  single playbook conversion.
- README hero swapped from the wait_until polling demo to the new
  conversion walker; tells the playbook -> FSM story at the top of the
  page.

### Fixed

- README image URLs converted from repo-relative paths to absolute
  GitHub raw URLs. PyPI's long-description renderer does not resolve
  relative paths, so the v0.0.5 - v0.0.8 PyPI pages had broken hero
  images. v0.0.9 onward renders correctly on both GitHub and PyPI.

## [0.0.8] - 2026-05-21

### Added

- ``examples/from_playbook_advanced/`` exercising the converter end-to-end
  on a multi-feature playbook (``set_fact``, ``block`` group, ``loop`` over
  a literal list with per-iteration ``{{ item }}``, ``notify`` + handler,
  ``changed_when: false`` on a read-only command, ``when:`` skip). The
  run.py reports each construct's effect from the final state so the
  conversion is visible at the command line. Workspace lives under
  ``~/.philip-advanced-demo`` (not ``/tmp``) and is cleaned up by the
  playbook's final task.

### Documented

- README demo-corpus table now lists both conversion demos
  (``from_playbook`` and the new ``from_playbook_advanced``) and reflects
  the actual demo count (twelve FSMs plus two conversion demos).

## [0.0.7] - 2026-05-21

### Added

- Converter: ``set_fact:`` is lowered to a pure-Python state-update
  action rather than handed to ansible-runner. Each task in the converter
  runs in its own play, so a normal Ansible set_fact would not propagate
  across tasks. The new lowering renders each arg value's Jinja against
  current Burr state and writes the result to state, so
  ``set_fact: composed: "{{ greeting }}, {{ target }}!"`` works as
  authored.
- Converter: ``changed_when:`` (string predicate or YAML bool) is honored
  via a post-action that re-evaluates ``_last_changed`` against state.
  Standard idioms (``changed_when: false`` on a read-only command,
  ``changed_when: result.rc != 0`` for a custom predicate) now do the
  right thing in the converted FSM.
- Jinja rendering in task arguments and set_fact values now sees every
  non-internal state key (set_fact-written, gather_facts-derived,
  play_vars-seeded), not just registered values. Subsequent task args
  can reference set_fact values as if they were facts in a single play.
- ``_when_to_expr_string`` accepts YAML booleans (``changed_when: false``
  parses as Python ``False``) and produces the matching Python literal.

### Tests

- Two new positive tests: set_fact chain (``greeting`` + ``target`` ->
  ``composed``) and ``changed_when: false`` forcing
  ``_last_changed=False`` on a successful command.

## [0.0.6] - 2026-05-21

### Added

- Converter: ``loop:`` and ``with_items:`` with a literal list lower to a
  three-action sub-FSM (``<task>_loop_init`` -> ``<task>`` -> ``<task>_loop_advance``)
  with a back-edge from advance to the task body until the items are
  exhausted. The task body sees the per-iteration value as ``{{ item }}``
  in its Jinja context, matching Ansible's variable naming. Each iteration
  is a discrete Burr step (visible in the trace and tracker).
- Loop state surfaces as ``_loop_<task>_items``, ``_loop_<task>_idx``,
  ``_loop_<task>_item``, ``_loop_<task>_done`` for FSMs that want to read
  iteration progress from a downstream action.
- Jinja-templated ``loop:`` values (e.g. ``loop: "{{ users }}"``) still
  raise ``UnsupportedPlaybookConstruct`` because resolving them needs
  runtime variable evaluation. Only literal lists are lowered in v0.0.6.

### Tests

- New positive test exercising a three-item loop end-to-end. The negative
  ``test_unsupported_loop_raises`` was repurposed to
  ``test_jinja_templated_loop_still_raises`` since literal-list loops
  are now supported.

## [0.0.5] - 2026-05-21

### Added

- ``_last_failure_kind`` sentinel populated on every ``@module_action`` call.
  Classifies the result into one of ``ok``, ``unreachable``, ``auth_failed``,
  ``timeout``, or ``module_error``. Lets transitions take distinct recovery
  paths per failure mode without grepping ``_last_msg``: e.g. retry on
  ``unreachable``, escalate immediately on ``auth_failed``, fall back to an
  alternative module on ``module_error``, raise the timeout next try on
  ``timeout``. Loosely aligned with the MAST taxonomy (Multi-Agent System
  failure Taxonomy, IBM Research + UC Berkeley, arXiv:2503.13657) restricted
  to modes a deterministic FSM can detect from an Ansible result.
- Exported as module-level constants: ``philip.FAILURE_KIND_OK``,
  ``FAILURE_KIND_UNREACHABLE``, ``FAILURE_KIND_AUTH_FAILED``,
  ``FAILURE_KIND_TIMEOUT``, ``FAILURE_KIND_MODULE_ERROR``, plus the
  ``FAILURE_KINDS`` tuple. Pattern: use the constants in transition
  predicates rather than string literals for typo-safety.
- ``initial_sentinels()`` seeds ``_last_failure_kind="ok"``.
- Four new tests in ``test_failures.py`` covering: ok on success,
  module_error on intentional fail, timeout on runner cap overrun, and
  branching a transition on the kind.

### Documented

- The classification is conservative and pattern-based: ``unreachable``
  trusts ansible's flag; ``auth_failed`` matches "permission denied" /
  "authentication failed" / publickey-denied phrases in ``msg``;
  ``timeout`` matches "exceeded timeout" / "timed out"; anything else that
  failed lands in ``module_error``. False negatives on auth_failed against
  exotic SSH error wording fall through to ``module_error``, which still
  routes correctly through any failure-handling branch.

## [0.0.4] - 2026-05-21

The converter now ingests the constructs every popular community Ansible
role opens with (OS-conditional ``include_tasks``, ``block:`` grouping,
``notify:`` + handlers). Without this, ``from_playbook`` could not convert
realistic third-party playbooks.

### Changed

- Burr dependency migrated from the pre-incubator ``burr[tracking]`` to
  ``apache-burr[tracking]>=0.42,<0.43``. The upstream project graduated to
  Apache incubation and renamed its PyPI distribution; the internal
  ``burr.core`` and ``burr.tracking`` import paths are unchanged. Fresh
  ``pip install philip`` was previously stranded on the pre-rename
  package.

### Added

- Converter: ``block:`` (group-only) lowers into inline tasks with the
  block's ``when:`` AND-propagated to every inner task. ``rescue:`` /
  ``always:`` still raise ``UnsupportedPlaybookConstruct`` (deferred to a
  later release as STRATUS-style undo/transactional edges).
- Converter: ``include_tasks:`` and ``import_tasks:`` with a literal
  filesystem path inline the referenced file's tasks at conversion time.
  Outer ``when:`` and ``notify:`` propagate down to every included leaf.
  Jinja-templated paths are not yet supported.
- Converter: ``notify:`` + ``handlers:`` round-trip end-to-end. Each
  notifying task gets a synthesized notify-marker action that flips a
  ``_notified_<slug>`` state flag when ``_last_changed`` is true. Handlers
  are appended after the main tasks and gated on their flag. Notify
  references to unknown handlers raise at convert time.
- Converter: chained ``when:`` skip transitions. Consecutive tasks whose
  ``when:`` evaluates false are skipped together rather than only the
  immediate next one. Matters for handler chains with multiple unnotified
  handlers.

### Tests

- Fixtures and tests added for the four new converter features. Updated
  ``test_unsupported_block_raises`` to ``test_block_with_rescue_still_raises``
  (block alone is now supported; ``rescue:``/``always:`` still rejected).
- Total suite: 40 tests, all green.

## [0.0.3] - 2026-05-21

### Added

- `philip` command-line entry point. `philip run <path>` executes an FSM
  from either a YAML playbook or a Python module that exposes an `app`
  attribute (or a `build_application()` callable). `philip graph <path>`
  prints the FSM structure as mermaid (default), graphviz dot, or plain
  text. `--halt-after ACTION` (repeatable) overrides the default halt set;
  unknown halt names produce a clear error instead of an opaque library
  trace.

### Documented

- README has a CLI section between "From an existing playbook" and "Demo
  corpus". REFERENCE.md gets a CLI usage block at the top.

## [0.0.2] - 2026-05-21

Runner hardening and a real playbook-conversion example.

### Added

- `run_module` and `@module_action` accept a `timeout` parameter (default
  300 seconds). On overrun the ansible-playbook subprocess is killed and
  the action's result carries `failed=True` with a diagnostic `msg`.
  Threaded through `Host.module(...)` and the shorthand methods.
- `PHILIP_DEBUG=1` environment variable disables ansible-runner's quiet
  mode. The ansible-playbook stdout and stderr stream to the controller,
  useful when a module crashes in a way the event stream does not capture.
- SIGINT handling. Ctrl-C during a long-running module call asks
  ansible-runner to cancel the subprocess via its `cancel_callback`,
  then re-raises `KeyboardInterrupt`. Without this, ansible-playbook
  would orphan and the SSH ControlMaster socket would linger.
- The `from_playbook(...)` converter now renders Jinja2 templates inside
  task arguments using Burr state as the context. Lets a converted
  playbook with `msg: "{{ git_check.stdout }}"` resolve across tasks.
  Previously each task ran as its own play and could not see prior
  registered values.
- `from_playbook(...)` now translates Jinja-style attribute access
  (`when: git_check.rc == 0`) on registered names into Python bracket
  access (`when: git_check['rc'] == 0`) so Burr's `expr()` can evaluate
  the predicate.
- `examples/from_playbook/`: a small playbook (`command` + `register` +
  `ignore_errors` + `changed_when: false` + Jinja-templated debug
  messages + `when: register.attr`) and a `run.py` that converts and
  runs it.
- README "From an existing playbook" section showing the inline YAML,
  the two-line `from_playbook(...)` conversion, and sample output.

### Documented

- The single-Application serial execution contract. Two `@module_action`
  calls against the same host running concurrently can collide on the
  shared SSH ControlPath; parallel Applications across distinct hosts
  is fine.

## [0.0.1] - 2026-05-21

Initial alpha release.

### Added

- `module_action` decorator wrapping `ansible-runner` as a Burr `@action`.
  Supports `reads`, `writes`, `register`, `host`, `connection`, `become`,
  `check_mode`, `diff`.
- `host()` factory plus `Host` dataclass for connection profiles. Captures
  Ansible hostvars and `become` once; exposes shorthand decorators
  (`.module`, `.service`, `.systemd`, `.shell`, `.command`, `.copy`,
  `.template`, `.file`, `.find`, `.slurp`, `.uri`).
- `Host.gather_facts()` runs `ansible.builtin.setup` and flattens facts
  into top-level State keys. `Host.initial_facts()` seeds placeholder
  values so transitions can read fact keys before the gather has executed.
- Ambient sentinel state keys written by every `@module_action`:
  `_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`,
  `_last_msg`. `initial_sentinels()` provides placeholder seeds.
- `snapshot_sentinels(write="failure_reason")` pure-Python action that
  preserves the current diagnostic into a durable state key, surviving
  any recovery actions that overwrite the sentinels.
- `wait_until(name, check, condition_expr, max_attempts, interval_s,
  on_success, on_timeout)` polling sub-graph builder. Returns a
  `WaitGraph` to merge into an `ApplicationBuilder`. Each poll attempt
  is a discrete Burr step.
- `from_playbook(path)` forward converter. Parses a YAML playbook and
  returns a runnable `burr.core.Application`. Supports single play with
  flat task list, `when:`, `register:`, `failed_when:`, `become:`,
  `gather_facts:`, and play-level `vars:`. Raises
  `UnsupportedPlaybookConstruct` for blocks, loops, handlers, includes,
  roles, and multi-play structures.
- ControlPersist and pipelining enabled by default for SSH connection
  reuse across modules targeting the same host.
- Eleven self-contained example FSMs in `examples/` spanning
  `ansible.builtin`, `community.crypto`, `community.docker`,
  `community.general`, and `ansible.posix`.
- VHS-recorded GIFs for two demos (`fact_driven_inspect`,
  `plan_then_apply`) plus the tape sources.
- pytest suite with smoke, failure-mode, and converter coverage.
- GitHub Actions CI for Python 3.11, 3.12, 3.13 on Linux.

### Known gaps

- Single-host only. Multi-host inventories and parallel fan-out are not
  yet supported; one Burr `Application` per host is the current pattern.
- No reverse converter. `from_playbook` covers the playbook-to-FSM
  direction; FSM-to-playbook emission is on the roadmap.
- The Burr dependency is pinned to a tight range. Burr is incubating at
  Apache, and an API change in a release philip does not yet support
  will break installs at version-resolution time rather than at runtime.

[0.0.1]: https://github.com/msradam/philip/releases/tag/v0.0.1
[0.0.2]: https://github.com/msradam/philip/releases/tag/v0.0.2
[0.0.3]: https://github.com/msradam/philip/releases/tag/v0.0.3
[0.0.4]: https://github.com/msradam/philip/releases/tag/v0.0.4
[0.0.5]: https://github.com/msradam/philip/releases/tag/v0.0.5
[0.0.6]: https://github.com/msradam/philip/releases/tag/v0.0.6
[0.0.7]: https://github.com/msradam/philip/releases/tag/v0.0.7
[0.0.8]: https://github.com/msradam/philip/releases/tag/v0.0.8
[0.0.9]: https://github.com/msradam/philip/releases/tag/v0.0.9
[0.0.10]: https://github.com/msradam/philip/releases/tag/v0.0.10
[0.0.11]: https://github.com/msradam/philip/releases/tag/v0.0.11
[0.0.12]: https://github.com/msradam/philip/releases/tag/v0.0.12
[0.0.13]: https://github.com/msradam/philip/releases/tag/v0.0.13
[0.0.14]: https://github.com/msradam/philip/releases/tag/v0.0.14
[0.0.15]: https://github.com/msradam/philip/releases/tag/v0.0.15
[0.0.16]: https://github.com/msradam/philip/releases/tag/v0.0.16
[0.0.17]: https://github.com/msradam/philip/releases/tag/v0.0.17
[0.0.18]: https://github.com/msradam/philip/releases/tag/v0.0.18
[0.0.19]: https://github.com/msradam/philip/releases/tag/v0.0.19
[0.0.20]: https://github.com/msradam/philip/releases/tag/v0.0.20
[0.0.21]: https://github.com/msradam/philip/releases/tag/v0.0.21
[0.0.22]: https://github.com/msradam/philip/releases/tag/v0.0.22
[0.0.23]: https://github.com/msradam/philip/releases/tag/v0.0.23
[0.0.24]: https://github.com/msradam/philip/releases/tag/v0.0.24
[0.0.25]: https://github.com/msradam/philip/releases/tag/v0.0.25
[0.0.26]: https://github.com/msradam/philip/releases/tag/v0.0.26
[Unreleased]: https://github.com/msradam/philip/compare/v0.0.26...HEAD
