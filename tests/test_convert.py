"""Tests for the forward playbook -> Application converter."""

from __future__ import annotations

from pathlib import Path

import pytest

import philip

FIXTURES = Path(__file__).parent / "fixtures"


def test_simple_playbook_runs_to_done() -> None:
    app = philip.from_playbook(FIXTURES / "playbook_simple.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["outcome"].startswith("OK")
    # The last executed module action wrote into _last_action.
    assert final["_last_action"] == "tag"


def test_when_predicate_skips_task() -> None:
    """``when: not skip_me`` with skip_me=True should skip the middle task."""
    app = philip.from_playbook(FIXTURES / "playbook_with_when.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The middle task "b" must not have been the last action; the FSM should
    # have skipped straight from "a" to "c".
    assert final["_last_action"] == "c"


def test_register_capture_lands_in_state() -> None:
    """``register: name`` should populate state[name] with the module result."""
    app = philip.from_playbook(FIXTURES / "playbook_register.yml")
    _, _, final = app.run(halt_after=["done", "escalate"])
    ping = final["ping_result"]
    assert isinstance(ping, dict)
    assert ping.get("ping") == "pong"


def test_task_level_include_role_inlines_role_tasks() -> None:
    """``include_role: name: myrole`` (and ``import_role:``) inside a task
    list resolves the role under ``roles/myrole/`` and inlines its
    ``tasks/main.yml`` at conversion time. The calling task's ``when:``
    propagates to each role task as inherited when.

    Task-level include_role does NOT pull in role-level vars or defaults
    (only the tasks); that's the play-level ``roles:`` behavior. So
    ``myrole_who`` is left undefined in this test even though the role
    references it in a debug message."""
    app = philip.from_playbook(FIXTURES / "playbook_with_include_role.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # Role tasks ran (the role's set_fact flipped myrole_ran).
    assert final["myrole_ran"] is True
    # Names: role tasks appear AFTER the calling pick_phase action.
    names = [a.name for a in app.graph.actions]
    assert names.index("pick_phase") < names.index("role_mark")
    assert names.index("role_mark") < names.index("after_include_role")


def test_play_with_roles_inlines_role_tasks_and_vars() -> None:
    """``roles: - myrole`` resolves ``roles/myrole/{tasks,vars,defaults,
    handlers}/main.yml`` next to the playbook, inlines role tasks before
    the play's own tasks, and merges vars (defaults < role vars <
    play vars)."""
    app = philip.from_playbook(FIXTURES / "playbook_with_role.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # Role tasks ran (set_fact in the role flipped myrole_ran).
    assert final["myrole_ran"] is True
    # vars/main.yml ("world") overrode defaults/main.yml ("default-fallback").
    assert final["myrole_who"] == "world"
    # Defaults still surface for keys the role's vars don't override.
    assert final["myrole_extra"] == "from-defaults"
    # The play's own task (`after role`) ran AFTER the role's tasks, so
    # it saw the role's state.
    assert final["post"] == "myrole_ran=True extra=from-defaults"
    # Role tasks appear at the START of the action list, before play tasks.
    names = [a.name for a in app.graph.actions]
    assert names.index("role_greet") < names.index("after_role")


def test_to_playbook_round_trips() -> None:
    """``to_playbook(from_playbook(p))`` produces a valid YAML that
    re-parses to the same play structure."""
    import yaml as _yaml

    src_path = FIXTURES / "playbook_simple.yml"
    app = philip.from_playbook(src_path)
    emitted = philip.to_playbook(app)
    re_parsed = _yaml.safe_load(emitted)
    original = _yaml.safe_load(src_path.read_text())
    assert re_parsed == original


def test_to_playbook_raises_for_hand_authored_app() -> None:
    """An Application that wasn't loaded via ``from_playbook`` has no
    canonical YAML representation; ``to_playbook`` must raise rather
    than silently emit a stub."""
    from burr.core import ApplicationBuilder, action

    @action(reads=[], writes=["x"])
    def step(state):
        return state.update(x=1)

    app = (
        ApplicationBuilder()
        .with_actions(step=step)
        .with_transitions()
        .with_state(x=0)
        .with_entrypoint("step")
        .build()
    )
    with pytest.raises(ValueError, match="not loaded by from_playbook"):
        philip.to_playbook(app)


def test_block_rescue_always_full_combo_rescued() -> None:
    """block + rescue + always where the block fails and rescue succeeds:
    - rescue runs (rescued=True)
    - always runs (always_ran=True)
    - the rescue clears the latched failure so restore is a no-op
    - the FSM reaches done, not escalate
    """
    app = philip.from_playbook(FIXTURES / "playbook_block_rescue_always.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["rescued"] is True
    assert final["always_ran"] is True
    assert final["_last_failed"] is False


def test_block_rescue_always_full_combo_double_failure() -> None:
    """block + rescue + always where BOTH the block AND rescue fail:
    - rescue's failure latches into the flag via save_failure
    - always still runs (always_ran=True)
    - restore_failure re-applies _last_failed=True after always
    - the FSM reaches escalate, not done
    """
    app = philip.from_playbook(FIXTURES / "playbook_block_rescue_always_double_fail.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "escalate"
    assert final["always_ran"] is True
    assert final["_last_failed"] is True


def test_block_plus_always_runs_always_after_block_success() -> None:
    """A block + always with no rescue, where the block succeeds, runs
    every always task after the block completes."""
    app = philip.from_playbook(FIXTURES / "playbook_block_always.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["block_succeeded"] is True
    assert final["always_ran"] is True


def test_block_plus_always_runs_always_after_block_failure_then_escalates() -> None:
    """A block + always where the block fails:
    - always still runs (always must run regardless of block outcome)
    - after always, the latched block failure is restored
    - the FSM reaches escalate, not done
    """
    app = philip.from_playbook(FIXTURES / "playbook_block_always_failure.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "escalate"
    # Always ran even though the block failed.
    assert final["always_ran"] is True
    # The latched failure was restored before escalation.
    assert final["_last_failed"] is True


def test_block_plus_rescue_runs_rescue_on_block_failure() -> None:
    """A deliberate ``ansible.builtin.fail`` inside ``block:`` must:
    - stop block execution at the failure point,
    - run the rescue chain,
    - clear the failure sentinels so post-block tasks run cleanly,
    - reach ``done`` (not ``escalate``).
    """
    app = philip.from_playbook(FIXTURES / "playbook_block_rescue.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The rescue chain ran: ``record_rescue`` set ``rescued=True``.
    assert final["rescued"] is True
    # The clear-failure post-action wiped ``_last_failed`` before the
    # post-block task executed; ``_last_failed`` is False at the end.
    assert final["_last_failed"] is False
    # The post-block task executed and registered a result.
    assert final["post_probe"]["ping"] == "pong"


def test_templated_loop_reads_list_from_state() -> None:
    """A ``loop: "{{ targets }}"`` with ``targets`` defined in play-level
    vars: iterates the actual list at runtime (three iterations here)."""
    app = philip.from_playbook(FIXTURES / "playbook_templated_loop.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["_loop_log_each_target_items"] == ["alpha", "bravo", "charlie"]
    assert final["_loop_log_each_target_idx"] == 3
    assert final["_loop_log_each_target_done"] is True


def test_jinja_templated_loop_resolves_at_runtime() -> None:
    """A ``loop:`` with a Jinja-templated value (``loop: "{{ users }}"``)
    resolves the template against current state at the loop_init step.
    Empty results are handled: done flips to True immediately."""
    app = philip.from_playbook(FIXTURES / "playbook_unsupported_loop.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    # The play has no ``users_from_inventory`` defined, so Jinja renders
    # empty; the loop fires zero iterations and the FSM still reaches done.
    assert last.name == "done"
    assert final["_loop_dynamic_loop_items"] == []
    assert final["_loop_dynamic_loop_done"] is True


def test_gather_facts_projects_ansible_facts_into_state() -> None:
    """When ``gather_facts: yes`` is set on the play, the converted FSM
    populates ``state['ansible_facts']`` with the projected facts dict
    (matching real-playbook idiom ``ansible_facts.os_family``), in
    addition to keeping the full module result at ``gathered_facts``."""
    app = philip.from_playbook(FIXTURES / "playbook_ansible_facts.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    facts = final["ansible_facts"]
    assert isinstance(facts, dict)
    # The setup module always returns a non-trivial dict of facts.
    assert len(facts) > 5, f"expected ansible_facts to be populated; got {len(facts)} keys"
    # Downstream Jinja references resolve via the projection.
    assert final["observed_system"] in ("Darwin", "Linux"), (
        f"expected observed_system to resolve via ansible_facts; got {final['observed_system']!r}"
    )


def test_jinja_filters_in_when_translate_to_python() -> None:
    """A ``when:`` clause with common Jinja filters (``| length``, ``| first``)
    and tests (``is defined``) is translated to equivalent Python at
    conversion time, so the FSM evaluates the predicate cleanly."""
    app = philip.from_playbook(FIXTURES / "playbook_jinja_filters.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # ``paths_list | length > 0`` is true; the skip task ran.
    assert final["skip_marker"] is False
    # ``label_dict is defined`` is true; defined task ran.
    assert final["defined_marker"] is True
    # ``(paths_list | first) == "/etc/foo"`` is true; default task ran.
    assert final["defaulted"] == "fallback"


def test_templated_include_vars_resolves_at_runtime() -> None:
    """``include_vars: "{{ pkg_mgr }}-vars.yml"`` renders the path against
    current state at task time and loads the matching YAML. Searches the
    playbook's base_dir then ``vars/`` then ``../vars/``."""
    app = philip.from_playbook(FIXTURES / "playbook_templated_include.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["distro_label"] == "debian"
    assert final["package_manager"] == "apt"


def test_include_vars_loads_yaml_into_state() -> None:
    """``include_vars: file: path`` reads the referenced YAML and writes
    every top-level key into Burr state. Downstream Jinja templates
    resolve against the freshly-loaded values."""
    app = philip.from_playbook(FIXTURES / "playbook_include_vars.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["distro_family"] == "debian"
    assert final["package_manager"] == "apt"
    assert final["default_packages"] == ["curl", "jq"]


def test_set_fact_writes_into_state_and_jinja_resolves_downstream() -> None:
    """``set_fact:`` lowers to a pure-Python state update, and a downstream
    ``set_fact:`` that templates the freshly-written value resolves it from
    Burr state. Validates that fact-set values propagate across tasks."""
    app = philip.from_playbook(FIXTURES / "playbook_set_fact.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["greeting"] == "hello"
    assert final["target"] == "world"
    assert final["composed"] == "hello, world!"


def test_changed_when_false_suppresses_change_signal() -> None:
    """``changed_when: false`` must force ``_last_changed=False`` even when
    the underlying module reports changed. This is the standard idiom for
    read-only command/shell tasks."""
    app = philip.from_playbook(FIXTURES / "playbook_changed_when.yml")
    _, _, final = app.run(halt_after=["done", "escalate"])
    assert final["_last_changed"] is False
    # The register still landed; only the changed signal was overridden.
    assert final["probe"]["rc"] == 0


def test_literal_loop_visits_each_item_in_order() -> None:
    """A ``loop:`` with a literal list lowers to a three-action sub-FSM:
    init -> task -> advance, with a back-edge from advance to task until
    the items are exhausted. The task action exposes ``{{ item }}`` in
    its Jinja context."""
    app = philip.from_playbook(FIXTURES / "playbook_with_loop.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The literal list landed in state via the loop_init action.
    assert final["_loop_greet_each_items"] == ["alice", "bob", "charlie"]
    # The advance walked all three; the final advance flipped done to True.
    assert final["_loop_greet_each_done"] is True
    # The final idx counter is one past the last item.
    assert final["_loop_greet_each_idx"] == 3
    # The graph has the three loop nodes.
    action_names = {a.name for a in app.graph.actions}
    assert "greet_each_loop_init" in action_names
    assert "greet_each" in action_names
    assert "greet_each_loop_advance" in action_names


def test_unsupported_strategy_raises() -> None:
    """``roles:`` is now supported; ``strategy:`` (and ``serial:`` /
    ``pre_tasks:`` / etc.) still raise as parallelism/phase keywords
    that philip fundamentally doesn't model."""
    with pytest.raises(philip.UnsupportedPlaybookConstruct, match="strategy"):
        philip.from_playbook(FIXTURES / "playbook_unsupported_roles.yml")


# ---------------------------------------------------------------------------
# v0.0.4: block (group-only), include_tasks/import_tasks, notify + handlers
# ---------------------------------------------------------------------------


def test_block_group_only_flattens_to_inline_tasks() -> None:
    """``block:`` without rescue/always inlines its inner tasks into the
    main sequence with the block's ``when:`` propagated to each inner task."""
    app = philip.from_playbook(FIXTURES / "playbook_block_group.yml")
    action_names = {a.name for a in app.graph.actions}
    # The block's inner tasks should appear as top-level actions.
    assert "first_inner" in action_names
    assert "second_inner" in action_names
    # And the wrapping block: name should NOT appear.
    assert "wrap" not in action_names
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["_last_action"] == "second_inner"


def test_include_tasks_inlines_referenced_file() -> None:
    """``include_tasks: <file>`` reads the referenced YAML and inlines its
    tasks into the main sequence at conversion time."""
    app = philip.from_playbook(FIXTURES / "playbook_with_include.yml")
    action_names = {a.name for a in app.graph.actions}
    # Tasks from the included file land in the main graph.
    assert "included_alpha" in action_names
    assert "included_beta" in action_names
    last, _, _ = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"


def test_import_tasks_inlines_referenced_file() -> None:
    """``import_tasks:`` behaves identically to ``include_tasks:`` at
    conversion time (we resolve the path once and inline)."""
    app = philip.from_playbook(FIXTURES / "playbook_with_import.yml")
    action_names = {a.name for a in app.graph.actions}
    assert "included_alpha" in action_names
    assert "included_beta" in action_names


def test_handler_runs_on_change() -> None:
    """A task with ``notify:`` triggers the named handler at the end of the
    play when the task reports ``changed=True``."""
    app = philip.from_playbook(FIXTURES / "playbook_with_handlers.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The notified handler was triggered (the marker after the changing
    # task flipped the slugified flag to True).
    assert final["_notified_say_hello"] is True
    # The handler action was the last module-flavored step before done.
    action_names = [a.name for a in app.graph.actions]
    assert any(n.startswith("handler_") for n in action_names)


def test_notify_unknown_handler_raises() -> None:
    """A ``notify:`` reference to a handler name that isn't declared must
    fail at convert time rather than producing a dead reference."""
    with pytest.raises(philip.UnsupportedPlaybookConstruct, match="unknown handler"):
        philip.from_playbook(FIXTURES / "playbook_notify_unknown.yml")
