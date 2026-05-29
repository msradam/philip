"""Run the multi-feature playbook through ``philip.from_playbook`` and
report what landed in Burr state.

The same playbook is what ``philip run examples/from_playbook_advanced/playbook.yml``
would execute. This script just adds a small report on top so the
constructs exercised are visible at the command line.
"""

from __future__ import annotations

from pathlib import Path

import philip


def main() -> None:
    playbook = Path(__file__).resolve().parent / "playbook.yml"
    app = philip.from_playbook(playbook)

    last_action, _, final = app.run(halt_after=["done", "escalate"])

    print(f"final action:     {last_action.name}")
    print(f"outcome:          {final.get('outcome', '')}")
    print()
    print("set_fact values:")
    print(f"  workspace_dir = {final.get('workspace_dir')!r}")
    print(f"  marker_file   = {final.get('marker_file')!r}")
    print(f"  manifest_file = {final.get('manifest_file')!r}")
    print()
    print("loop state (manifest entries):")
    print(f"  items     = {final.get('_loop_write_manifest_entries_items')!r}")
    print(f"  idx       = {final.get('_loop_write_manifest_entries_idx')!r}")
    print(f"  done      = {final.get('_loop_write_manifest_entries_done')!r}")
    print()
    print("notify + handler:")
    print(f"  log marker write triggered: {final.get('_notified_log_marker_write')!r}")
    print()
    print("changed_when override on count_probe:")
    print(f"  _last_changed at end       = {final.get('_last_changed')!r}")
    print(f"  count_probe.stdout (head)  = "
          f"{(final.get('count_probe') or {}).get('stdout', '').splitlines()[:1]}")


if __name__ == "__main__":
    main()
