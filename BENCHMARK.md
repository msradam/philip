# Benchmark

## What this file is

A local structural-invariant check on `examples/mast_sre_agent`'s
FSM-structured SRE agent. Not ITBench. Not a leaderboard submission.

## What it is not

[ITBench](https://github.com/itbench-hub/ITBench) is the published
SRE-agent benchmark (IBM Research + UC Berkeley, accepted at ICML 2025
and NeurIPS 2025 via the STRATUS paper). It deploys real Kubernetes
clusters with fault scenarios injected and scores agents on fault
localization, repair time, and resolution rate.

Putting philip on ITBench is a separate, larger project: it requires
a local K8s cluster (Kind, 8 CPU / 16 GB / 50 GB), the ITBench scenario
deployment tooling, and a custom agent adapter against the reference
SRE agent's tool surface (kubectl, Prometheus, Jaeger, ClickHouse).
That's on the roadmap, not in this repo yet.

## What this file does measure

The `bench/run.py` harness runs the existing demo agent 18 times across
two LLMs and three fault scenarios, then counts whether two structural
invariants held and reports a per-cell summary.

The latest run is in [`bench/results.md`](./bench/results.md);
reproduce with `uv run python bench/run.py` (prerequisites: the demo
container started via `bash examples/service_remediation/start.sh`,
plus a running Ollama daemon with both listed Granite models pulled).

### Invariants verified

Across 18 trials:

1. **Verify-before-done held in 6/6 trials that reached `done`.** Every
   `done` terminal was preceded by an `external_verify` action returning
   HTTP 200. The FSM has no edge from remediation directly to done, so
   this should be true by construction. The measurement confirms the
   graph is wired the way the design says.

2. **No `done` terminals reached via off-allow-list LLM picks.** The
   upstream classifier parser normalizes unrecognized LLM output to
   `other`, and `other` routes to `escalate`. The trial set didn't
   produce raw off-list strings to stress-test the validator's
   demotion path, but the structural property held end-to-end.

### Safety/effectiveness across LLM quality

The interesting finding is the comparison across model sizes:

| Model | Trials | Reached `done` | Reached `escalate` |
|---|---|---|---|
| `ibm/granite4:micro` (2 GB) | 9 | 6 | 3 |
| `ibm/granite4:350m` (700 MB) | 9 | 0 | 9 |

The smaller Granite emitted unrecognizable raw output on every scenario
including the answerable OOM and disk-full cases. The parser normalized
to `other` and the FSM bailed to `escalate`. **Zero wrong-remediation
chains ran. Zero false-positive `done` terminals.**

The takeaway is the structural-mitigation claim made concrete. A weak
LLM driving this FSM is **safe but ineffective**: nothing wrong gets
executed, but nothing right gets resolved either, so a human picks it
up via the escalate terminal. A capable LLM is **safe and effective**:
resolves the answerable scenarios, bails on unanswerable ones. The
safety property holds across the LLM quality gap; the effectiveness
property scales with the LLM. The conventional ReAct-loop pattern
mixes the two and lets a confused LLM execute unsafe actions; the
FSM separates them by construction.

## Roadmap

- **ITBench integration**: build the kubectl / Prometheus / Jaeger /
  ClickHouse tool surface as philip `@module_action` and `@action`
  nodes, package as an ITBench-compatible Docker agent, run against
  the published SRE scenarios. Multi-session project.
- **More LLMs**: this run used two Granite variants. Expanding to
  Llama 3.x, Qwen 2.x, GPT-4o-mini would let the "safe across LLM
  quality" claim land on a wider sample.
- **More scenarios**: the demo has 2 fault classes (OOM, disk_full)
  plus garbage. ITBench has 21 mechanisms across 6 scenarios; adding
  representative ones locally would broaden the structural check
  without the K8s overhead.
