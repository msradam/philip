"""Tests for the ``philip`` command-line entry point."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from philip.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def test_run_executes_playbook() -> None:
    code, output = _run(["run", str(FIXTURES / "playbook_simple.yml")])
    assert code == 0
    assert "final action: done" in output


def test_run_with_halt_after_default_to_done_escalate() -> None:
    code, output = _run(["run", str(FIXTURES / "playbook_simple.yml")])
    assert code == 0
    assert "final action: done" in output


def test_run_refuses_unknown_suffix(tmp_path: Path) -> None:
    bogus = tmp_path / "not_a_playbook.txt"
    bogus.write_text("hello")
    with pytest.raises(SystemExit):
        _run(["run", str(bogus)])


def test_run_refuses_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-here.yml"
    with pytest.raises(SystemExit) as exc:
        _run(["run", str(missing)])
    assert "no such file" in str(exc.value).lower()


def test_graph_mermaid_default_format() -> None:
    code, output = _run(["graph", str(FIXTURES / "playbook_simple.yml")])
    assert code == 0
    assert "graph TD" in output
    assert "done[done]" in output
    assert "escalate[escalate]" in output


def test_graph_dot_format() -> None:
    code, output = _run(["graph", str(FIXTURES / "playbook_simple.yml"), "--format", "dot"])
    assert code == 0
    assert "digraph philip {" in output
    assert "->" in output


def test_graph_text_format() -> None:
    code, output = _run(["graph", str(FIXTURES / "playbook_simple.yml"), "--format", "text"])
    assert code == 0
    assert "actions:" in output
    assert "transitions:" in output


def test_run_loads_python_app_attribute(tmp_path: Path) -> None:
    fsm = tmp_path / "fsm.py"
    fsm.write_text(
        "from burr.core import ApplicationBuilder, action\n"
        "\n"
        "@action(reads=[], writes=['x'])\n"
        "def step(state):\n"
        "    return state.update(x=1)\n"
        "\n"
        "@action(reads=['x'], writes=['outcome'])\n"
        "def done(state):\n"
        "    return state.update(outcome=f'x={state[\"x\"]}')\n"
        "\n"
        "app = (ApplicationBuilder()\n"
        "    .with_actions(step=step, done=done)\n"
        "    .with_transitions(('step', 'done'))\n"
        "    .with_state(x=0, outcome='')\n"
        "    .with_entrypoint('step')\n"
        "    .build())\n"
    )
    code, output = _run(["run", str(fsm)])
    assert code == 0
    assert "final action: done" in output
    assert "outcome:      x=1" in output


def test_run_loads_python_build_application_callable(tmp_path: Path) -> None:
    fsm = tmp_path / "fsm.py"
    fsm.write_text(
        "from burr.core import ApplicationBuilder, action\n"
        "\n"
        "@action(reads=[], writes=['x'])\n"
        "def step(state):\n"
        "    return state.update(x=42)\n"
        "\n"
        "@action(reads=['x'], writes=['outcome'])\n"
        "def done(state):\n"
        "    return state.update(outcome=f'x={state[\"x\"]}')\n"
        "\n"
        "def build_application():\n"
        "    return (ApplicationBuilder()\n"
        "        .with_actions(step=step, done=done)\n"
        "        .with_transitions(('step', 'done'))\n"
        "        .with_state(x=0, outcome='')\n"
        "        .with_entrypoint('step')\n"
        "        .build())\n"
    )
    code, output = _run(["run", str(fsm)])
    assert code == 0
    assert "outcome:      x=42" in output


def test_lint_reports_action_counts_on_success() -> None:
    """``philip lint`` should produce a structural summary for a
    playbook that converts cleanly."""
    code, output = _run(["lint", str(FIXTURES / "playbook_simple.yml")])
    assert code == 0
    assert "OK" in output
    assert "actions:" in output
    assert "transitions:" in output
    assert "module + python tasks" in output


def test_lint_reports_unsupported_construct(tmp_path: Path) -> None:
    """``philip lint`` should exit 1 and name the blocking construct
    when the playbook hits an UnsupportedPlaybookConstruct."""
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "- name: bad\n"
        "  hosts: localhost\n"
        "  gather_facts: no\n"
        "  strategy: free\n"
        "  tasks:\n"
        "    - ansible.builtin.ping:\n"
    )
    code, output = _run(["lint", str(bad)])
    assert code == 1
    assert "REJECTED" in output
    assert "strategy" in output


def test_lint_refuses_python_file(tmp_path: Path) -> None:
    """lint operates only on YAML playbooks, not Python modules."""
    py = tmp_path / "fsm.py"
    py.write_text("app = None\n")
    with pytest.raises(SystemExit):
        _run(["lint", str(py)])


def test_run_python_file_with_neither_app_nor_builder(tmp_path: Path) -> None:
    fsm = tmp_path / "fsm.py"
    fsm.write_text("# no Application here\nVALUE = 42\n")
    with pytest.raises(SystemExit) as exc:
        _run(["run", str(fsm)])
    assert "neither" in str(exc.value).lower() or "app" in str(exc.value).lower()
