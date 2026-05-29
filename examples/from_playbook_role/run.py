"""Convert a multi-file role-style playbook into a Burr Application.

The playbook lives at ``main.yml`` next to this file. It mirrors the
shape every popular community Ansible role (geerlingguy.mysql, .docker,
.postgresql, .nginx) opens with: an OS-conditional ``include_tasks``
dispatch into a ``tasks/setup-<distro>.yml`` file.

This demo proves the conversion handles the cross-file include path:
``main.yml`` references ``tasks/setup-debian.yml`` and
``tasks/setup-redhat.yml``; the converter resolves both, inlines their
tasks, and gates them on the ``when:`` clause from the include line.

Two runs below: the first with ``target_pkg_mgr=apt`` (Debian path),
the second with ``target_pkg_mgr=dnf`` (RedHat path). The same converted
``Application`` does the work; only the play-level var changes.

Run::

    uv run python examples/from_playbook_role/run.py
"""

from __future__ import annotations

from pathlib import Path

import philip

_PLAYBOOK = Path(__file__).resolve().parent / "main.yml"


def main() -> None:
    app = philip.from_playbook(_PLAYBOOK)
    last_action, _, final = app.run(halt_after=["done", "escalate"])
    print(f"final action:        {last_action.name}")
    print(f"announce_text:       {final.get('announce_text')!r}")
    print(f"distro_family:       {final.get('distro_family')!r}")
    print(f"installed_label:     {final.get('installed_label')!r}")
    print(f"completion notified: {final.get('_notified_notify_completion')!r}")


if __name__ == "__main__":
    main()
