"""Lift an existing Ansible playbook into a runnable Burr Application.

This is the same two-line conversion users get from the README. The
``playbook.yml`` next to this file is a normal Ansible playbook with
``register:``, ``ignore_errors:``, and attribute-access ``when:`` clauses.

Run::

    uv run python examples/from_playbook/run.py
"""

from __future__ import annotations

from pathlib import Path

import philip


def main() -> None:
    playbook = Path(__file__).resolve().parent / "playbook.yml"
    app = philip.from_playbook(playbook)

    last_action, _, final = app.run(halt_after=["done", "escalate"])

    print(f"final action:  {last_action.name}")
    print(f"outcome:       {final.get('outcome', '')}")
    print()
    for register in ("git_check", "jq_check"):
        result = final.get(register) or {}
        rc = result.get("rc", "?")
        stdout = (result.get("stdout") or "").strip()
        if rc == 0:
            print(f"  {register:10} rc={rc}  {stdout}")
        else:
            print(f"  {register:10} rc={rc}  (not installed)")


if __name__ == "__main__":
    main()
