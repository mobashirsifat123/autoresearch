# Next-step research roadmap

Ranked by expected impact × confidence ÷ (complexity + risk). None of these are
implemented in this upgrade; they require evidence or explicit authorization.

| Rank | Improvement | Why it matters | Evidence required first | Complexity | Risk |
|---:|---|---|---|---|---|
| 1 | **Live evaluation of monitor contracts** on one real ML project | The mechanism is tested synthetically; real telemetry (loss, grad norm, val metric) will show false-positive/negative rates. | A small approved run with a real training script and a declared contract. | M | Medium: may abort a legitimate run; tune thresholds from the pilot. |
| 2 | **Human-labeled conflict precision study** | The heuristic conflict index could surface noise; expert labels are needed before using it in a manuscript claim. | 50–100 sampled claim pairs labeled by two researchers; report precision/recall by theme. | M | Low: read-only evaluation. |
| 3 | **Explicit, opt-in reverse loop** (`workflow --feedback auto` with a loop budget) | Currently a refutation produces a recommendation; automation would close the loop. | Policy tests, a hard budget, duplicate-question detection, and explicit user authorization. | L | High: cost and runaway loops. |
| 4 | **Repair workflow bound to a new approval** | On `repair_implementation`, generate a new implementation, re-verify, show the diff, require approval, and record the repair lineage. | A design for approval/version semantics; tests that the old run remains immutable. | L | High: code changes must never silently alter the hypothesis. |
| 5 | **GPU/telemetry worker adapter** | The contract is transport-agnostic; real GPU metrics (memory, utilization, temperature) enable resource-exhaustion detection. | Access to a GPU worker and a stable metric protocol. | M | Medium: hardware-specific false positives. |
| 6 | **Semantic claim–evidence entailment for manuscripts** | Numeric traceability is strong; causal sentences are not semantically checked. | A labeled set of supported/unsupported sentences; measure precision before gating. | L | High: model-based gate could be wrong; keep deterministic fallback. |
| 7 | **Review audit persistence of expert verdicts** | Conflict candidates need resolution states (`confirmed`, `not_a_conflict`, `incomparable`) for reuse. | Schema/UI decision and a labeling workflow. | S | Low: additive. |
| 8 | **Cross-project prior contradiction check** | A new project may re-propose a mechanism refuted in another project. Existing memory is project-scoped. | Privacy/scope decision; findings are project-local by design. | M | Medium: cross-project leakage. |
| 9 | **Manuscript claim-to-conflict linkage** | If the study resolves a conflict, the manuscript should cite both sides explicitly. | Conflict-resolution verdicts (rank 7) and a claim-ledger extension. | S | Low. |
| 10 | **Operational dashboards** over `research_state.json` | Operators currently inspect JSON/CLI. | A UI requirement; no scientific risk. | S | Low. |

## Explicit non-goals

- Do not add an LLM reflection call after every probe sample.
- Do not auto-approve execution or auto-spend compute.
- Do not treat runtime success as scientific support.
- Do not replace the deterministic statistical analyzer or manuscript gates
  with an LLM judgment.
- Do not add a new database/framework; SQLite + artifacts already carry the
  state, and schema v17 is additive.
