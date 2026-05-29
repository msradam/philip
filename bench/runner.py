"""Run one trial of the MAST-aligned SRE agent and emit structured JSON.

Spawned by ``bench/run.py`` once per (model, scenario, trial) cell. Each
subprocess re-imports ``mast_sre_agent.fsm`` cleanly so the module-level
``LOG_SCENARIO`` / ``OLLAMA_MODEL`` constants pick up the env vars set
for this trial. Subprocess isolation also keeps any leaked state out of
the next cell.

Output: a single JSON object on stdout. Stderr is left alone for debug.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Make the example package importable without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from mast_sre_agent.fsm import build_application


def main() -> int:
    app = build_application()
    t0 = time.perf_counter()
    last_name = "unknown"
    step_count = 0
    saw_external_verify = False
    state = app.state
    while True:
        out = app.step()
        if out is None:
            break
        action, _, state = out
        step_count += 1
        last_name = action.name
        if last_name == "external_verify":
            saw_external_verify = True
        if last_name in ("done", "escalate"):
            break
    wall = time.perf_counter() - t0
    final = dict(state.get_all())

    json.dump(
        {
            "terminal": last_name,
            "step_count": step_count,
            "wall_seconds": round(wall, 3),
            "saw_external_verify": saw_external_verify,
            "classification": final.get("classification"),
            "classification_llm": final.get("classification_llm"),
            "validation_note": final.get("validation_note"),
            "http_status": final.get("http_status"),
            "outcome": final.get("outcome"),
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
