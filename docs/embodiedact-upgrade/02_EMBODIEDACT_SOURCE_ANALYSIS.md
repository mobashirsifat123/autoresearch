# EmbodiedAct source-level analysis and transfer map

Sources inspected:

- Paper: *Grounding LLMs in Scientific Discovery via Embodied Actions*,
  arXiv:2602.20639 (abstract retrieved from the arXiv API).
- Public repository: `https://github.com/THU-AICosmos/EmbodiedAct`
  (cloned at commit HEAD on 2026-10-19; shallow clone).
- The anonymous.4open.science link was **not required** because the public
  repository was accessible. No claim below depends on the anonymous artifact.
- The public release is explicitly scoped to runtime components; benchmark
  datasets and paper-specific evaluation scripts are excluded
  (`README.md`, "Overview"). Claims about benchmark numbers are therefore
  paper-level, not independently verified here.

Categories used below:

- **[V] Verified in source** — read directly in the cloned repository.
- **[P] Paper/repository claim** — documented but not independently reproduced.
- **[A] Proposed adaptation** — my design for AutoResearch, not an EmbodiedAct
  claim.

---

## 1. Verified architecture

### 1.1 Four-module dual loop

`src/embodiedact/agent/runner.py:154-157` defines four explicit budgets:

```
MAX_HOTFIX_RETRIES   = 2   # batch-path code repair
MAX_STREAM_REPAIRS   = 2   # streaming path: aborts + errors
MAX_LOCAL_ADJUST     = 2   # parameter refinement within the plan
MAX_REPLAN_ITERATIONS= 1   # outer-loop strategic revision
```

The outer loop (`runner.py:275-430`) runs
`M_plan → M_code → execute → M_perc → M_ref`. The inner loop
(`_execute_cycle_streaming`, `runner.py:819-880`) treats an abort or execution
error as a **repair trigger**: it hot-fixes the code with the live telemetry and
re-executes, bounded by `MAX_STREAM_REPAIRS`. A watchdog timeout is terminal and
suppresses repair because the session state is unknown (`runner.py:845-856`).

**[V]** This is a genuine two-level loop: parameter adjustment, code repair, and
global replanning have separate budgets and separate prompts.

### 1.2 Monitor contract and executable abort predicates

`agent/contracts.py` defines `MonitorSignal` and `MonitorContract`: at most the
planner-authored signals with `name`, `constraint_id`, `units`, `cadence`,
`healthy` bounds/trend, `abort_if`, and a `perceiver` expression.
`agent/perceivers.py` compiles a perceiver with an AST allowlist (no attributes,
subscripts, lambdas, comprehensions, power operator; function whitelist
`abs/min/max/isnan/isinf/sqrt/log/...`; empty builtins). Compilation failure
falls back to the rule engine; runtime failures disable the perceiver after
three errors.

**[V]** The contract is executable, not decorative.

**[P]** The paper describes the planner as synthesizing this contract per
problem. The repository implements the parsing, compilation, and enforcement;
the planner prompt path is present but not run here.

### 1.3 Streaming perception and severity ladder

`agent/streaming_perception.py`:

- `observe(payload)` converts a probe sample to a `StreamVerdict`.
- NaN/Inf, bound breaches, compiled-perceiver fires, and guard events are
  `fatal` (`abort=True`).
- Trend violations are `suspect` after `max_increases` consecutive violations
  and escalate to `fatal` at `max_increases * escalation_factor`.
- `on_event` treats an `ea_emit` guard as fatal.
- `recent_window()` and `trend_features()` keep bounded history (64 samples) for
  repair prompts; `render_digest()` produces a compact summary.

**[V]** The classification is deterministic and O(1) per sample; no LLM is called
at stream rate.

### 1.4 Two-tier intervention and steering

`agent/steering.py` implements a bounded, deduplicating `SteeringBuffer`:
suspect verdicts are queued and injected into the **next** correction prompt;
fatal verdicts interrupt immediately. `peek_digest` does not clear; only a
correction prompt (`drain_digest`) consumes the queue.

`agent/handle.py` implements run–wait–perceive: `OperationHandle.launch` starts
the operation, a pump routes events, and `await_verdict()` sleeps until
`ABORT / COMPLETE / FAILED / ESCALATION / TIMEOUT`. The watchdog is a hard
terminal state; an expired operation is never re-blessed by reflection.

**[V]** The distinction between "suspect steers" and "fatal stops" is real and
tested (`tests/test_steering.py`, `tests/test_streaming_perception.py`).

### 1.5 Python probe line protocol

`platforms/python_adapter.py`:

- Generated code prints `EA_PROBE:{"name": ..., "value": ...}` and
  `EA_EMIT:{"event": ...}`.
- The adapter reads stdout line-by-line with `readline()` and a remaining-time
  deadline, yields `probe` / `monitor_event` / `stdout` events, and terminates
  the process on timeout or interrupt.
- stderr is drained after stdout EOF (documented limitation).

**[V]** This is the most directly reusable mechanism for AutoResearch, which
already executes Python scripts through a subprocess.

### 1.6 Pre-execution instrumentation lint

`agent/lint_gate.py` checks generated code against the monitor contract before
execution: every signal is probed, the probe return value is honored, cadence
is throttled, arrays are not probed, and a final result line exists. Policy:
one regeneration with lint feedback, then proceed with a **recorded warning**,
never a hard failure.

**[V]** This catches "the monitor exists but the code never emits it" before
wasting a run.

### 1.7 Journal and metrics

`runtime/journal.py` writes append-only JSONL with monotone `seq` and posix
timestamps. `runtime/metrics.py` computes divergence-detection lead time and
interrupt reaction time from the journal.

**[V]** Replayable evidence is a first-class artifact, not logging side effects.

### 1.8 Constraint-gated acceptance

`runner.py` keeps `accepted` distinct from `execution_successful`; the reflector
must return a per-constraint checklist and the stop gate accepts only when every
entry passes (`runner.py:373`, `:402-404`). A run that exits 0 but violates a
constraint is not accepted.

**[V]** This is exactly the distinction AutoResearch needs between "the command
completed" and "the hypothesis was supported".

---

## 2. Paper-level claims not independently verified

- **[P]** EmbodiedAct achieves SOTA on MATLAB engineering-design and scientific
  modeling benchmarks. The public repository excludes benchmark data/evaluation
  scripts, so this audit does not verify any score.
- **[P]** Cooperative interruption reacts in ~0.1s for blocking operations and
  forced simulation stop for asynchronous runs. The code paths exist; timing was
  not reproduced.
- **[P]** The four-cell perception ablation grid yields the reported lead-time
  improvements. The configuration exists (`PerceptionConfig`); the benchmark
  numbers were not run.

These claims do **not** transfer to AutoResearch's scientific validity. Improved
simulation execution does not establish literature novelty, hypothesis quality,
statistical validity, or manuscript integrity.

---

## 3. Mechanism → AutoResearch mapping

| EmbodiedAct mechanism | Problem it solves there | AutoResearch beneficiary | Transfer decision | Required change | New failure mode | Test |
|---|---|---|---|---|---|---|
| Monitor contract (≤3 signals, bounds/trend/abort_if) | Makes "what does failure look like?" explicit before execution | `experiment` spec/planner | **Adapt conceptually** | Optional `monitor_contract` in planner schema/spec, persisted; deterministic fallback when absent | Bad thresholds abort healthy runs | Unit: valid/invalid contract/bounds/trend; e2e: NaN probe through the worker |
| Streaming perception severity ladder | Detects NaN/divergence mid-run without LLM cost | `workers/local_typed.py` | **Reuse mechanism, new code** | Line-oriented stdout reader + deterministic evaluator | False fatal on noisy metrics | Unit: NaN, bound, trend; process kill test |
| Probe line protocol (`EA_PROBE:`) | Decouples instrumentation from the agent | generated experiment script | **Adapt names** (`AUTORESEARCH_PROBE:`) | Prompt/verifier contract; parser accepts legacy `EA_PROBE` too | Generated code omits probes | Static verifier test + e2e probe run |
| Instrumentation lint gate | Prevents silent lack of monitoring | `experiments/implementation.py` | **Adapt** | Verify declared monitor names appear in source; bounded repair | Extra repair loop cost | Verifier test with/without probes |
| Two-tier intervention (fatal stop, suspect steer) | Avoids killing runs on mere suspicion | execution + recovery policy | **Adapt** | Fatal → terminate; suspect → recorded evidence only (no LLM mid-run) | Suspect noise floods report | Unit: severity routing |
| Abort → repair → resume | Turns interruption into a recovery action | `experiment` recovery | **Adapt with approval** | Deterministic decision to `repair_implementation`; actual code regeneration stays a separate, approved action | Patch loop hides scientific failure | Policy tests for each decision class |
| Hot-fix vs local adjust vs replan | Separates code bug, parameter issue, strategy failure | `application/recovery.py` | **Adapt** | Classify execution failure vs report decision; record rationale | Misclassification | Table-driven tests |
| Watchdog terminal state | Prevents retrying an unknown session | worker timeout/stall | **Adapt** | Timeout is a terminal `FAILED/TIMEOUT`; recovery may recommend at most one same-spec retry, then parameter review. No silent re-execution. | Repeated retry burns compute | Test timeout → retry recommendation → escalation |
| Heartbeat / lease | Detects dead workers without guessing | `worker_jobs` | **Reuse existing columns** | Update heartbeat during run; write pid/pgid/offset | DB writes during long run | Run persists heartbeat/pid; hours-long advancement test is future |
| Journal (append-only seq) | Replayable evidence | execution monitor + recovery | **Adapt** | Persist a bounded `execution_monitor` summary + verdict trace; keep append-only `api_events`. Not a full JSONL replay. | Artifact size | Test artifact content |
| Constraint-gated acceptance | "ran" ≠ "verified" | experiment report / manuscript | **Already present** (decision + readiness gates) | Report records failed/aborted attempt count; direct monitor-to-manuscript linkage is future | Attrition must remain visible | Report limitation test / existing gates |
| Steering buffer | Guidance for next correction | recovery decision evidence | **Concept only** | Include suspect observations in recovery record | N/A | Policy test |

### 3.1 What should **not** be transferred

- **LLM reflection after every operation.** AutoResearch's statistical analyzer
  and decision gates are deterministic and stronger than an LLM verdict. Adding
  an LLM to the execution loop would add cost and nondeterminism.
- **Unbounded hot-fix loops.** EmbodiedAct bounds them at 2; AutoResearch must
  additionally preserve the scientific record and require a new approval when
  code changes.
- **MATLAB/Simulink-specific machinery.** The platform abstraction is irrelevant;
  only the runtime pattern transfers.
- **Claim that runtime reliability implies scientific validity.** It does not.

---

## 4. Proposed AutoResearch closed-loop architecture

```
                    ┌─────────────────────────────────────────────┐
                    │ Shared SQLite state + artifacts (existing)  │
                    │ questions · claims · candidates · specs ·   │
                    │ jobs · analyses · reports · findings        │
                    └───────────────▲─────────────────────────────┘
                                    │ append-only events / recovery decisions
 review ──conflicts+gap──► novelty ──candidate──► experiment ──report──► manuscript
    ▲                         ▲                       │                     │
    │                         │                       ▼                     │
    │                         │              ┌──────────────────┐           │
    │                         └──────────────│ recovery policy  │◄──────────┘
    └────── conflict evidence / findings ────│ fast: retry/repair│
                                             │ slow: novelty/review/stop
                                             └──────────────────┘
                                                       ▲
                                             runtime perception
                                             (probe stream, no LLM)
```

Implemented scope (this upgrade):

1. **Runtime perception** in the local worker: probe parsing, severity ladder,
   early kill, heartbeat, monitor artifact, integrity check of the executed
   script against the verified implementation.
2. **Deterministic recovery policy**: classify operational vs scientific
   failure and record an explicit next action with evidence and rationale.
   Expensive actions are **recommended, not auto-executed**.
3. **Failure evidence** persisted to Findings Memory so the next novelty cycle
   sees it.
4. **Corpus-level conflict candidates and missing-evidence ledger** in review,
   carried through the review corpus to novelty/manuscript.
5. **Consolidated research-state snapshot** with explicit workflow states.

Future (not implemented): automated re-entry into novelty/review after a
refutation, multi-worker GPU monitoring, LLM-on-stream consults.
