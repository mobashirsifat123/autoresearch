# Prioritized implementation plan

The plan is ordered by scientific benefit × reliability gain ÷ cost/risk. Every
item names the confirmed problem and the exact source location. Items marked
**E** are implemented in this upgrade; **O** are optional extensions not
implemented.

---

## E1. Runtime perception for experiment execution

- **Confirmed problem:** W1 — `workers/local_typed.py:105` buffers all output via
  `process.communicate()`; NaN/divergence is detected only after the run ends or
  the timeout fires. `experiments/executor.py` has the same behaviour.
- **Source location:** `src/autoresearch/workers/local_typed.py`,
  `src/autoresearch/experiments/executor.py`, `domain/models.py` (`ExperimentSpec`),
  `storage/repository.py` (schema), `experiments/planner.py`,
  `experiments/implementation.py`.
- **Solution:**
  1. New deterministic `experiments/perception.py`: probe-line parser,
     monitor-contract parser (≤3 signals), safe AST predicate compiler,
     severity ladder (`info/suspect/fatal`), bounded history, digest.
  2. `ExperimentSpec.monitor_contract` persisted in schema v17.
  3. Planner asks for an optional contract (bounds/trend/abort_if/stall).
  4. Implementation verifier requires declared probe names when a contract
     exists; generation prompt includes the line protocol.
  5. Worker reads stdout/stderr line-by-line, feeds probes, kills the process
     group on fatal verdicts, keeps partial output, writes
     `execution_monitor` artifact.
- **EmbodiedAct link:** monitor contract, streaming perception, severity ladder,
  pre-execution lint gate, probe line protocol.
- **Expected benefit:** early termination of invalid runs; intermediate
  evidence; fewer wasted GPU-hours; inspectable failure cause.
- **Complexity:** medium-high.
- **Compatibility risk:** planner schema additive; old specs have empty
  contracts; no LLM calls added; generated scripts only change when a contract
  is present.
- **Tests:** unit (`test_execution_perception.py`), worker e2e with NaN probe,
  healthy probe run, timeout, no-contract regression.
- **Success criteria:** NaN-emitted script terminates before its natural end;
  monitor artifact exists; healthy existing smoke flow still passes.

## E2. Heartbeat, process identity, and honest terminal states

- **Confirmed problem:** W2 — heartbeat is not maintained; `recover_stale` can
  mark a healthy run stale; timeout is conflated with cancellation.
- **Source location:** `workers/local_typed.py`, `worker_jobs` schema.
- **Solution:** heartbeat task during execution (5s), write `process_id`,
  `process_group_id`, `last_log_offset`; distinguish `TIMEOUT`, `STALL`,
  `PERCEPTION_ABORT`, ordinary `FAILED`, `CANCELED`.
- **EmbodiedAct link:** run–wait–perceive, watchdog terminal state.
- **Expected benefit:** correct stale recovery; no duplicate execution.
- **Complexity:** low-medium.
- **Compatibility risk:** state vocabulary extended additively; existing
  `COMPLETED/FAILED/CANCELED` still used.
- **Tests:** pid/offset recorded; timeout maps to `FAILED` + `TIMEOUT`, not
  `CANCELED`; cancellation is terminal. An hours-long heartbeat-advance test
  remains future work.
- **Success criteria:** job row records process identity; timeout category is
  exact; a late finish cannot overwrite a cancelled run.

## E3. Implementation-content integrity at execution time

- **Confirmed problem:** W5 — approval checksum covers the spec/action/worker,
  not the script bytes; an approved action can execute edited code.
- **Source location:** `experiments/policy.py:29`, `experiments/implementation.py:_record`,
  `workers/local_typed.py`.
- **Solution:** before launching, hash the script bytes and compare with the
  latest `experiment_implementation` artifact for this spec. Mismatch →
  `implementation_integrity` failure, no execution. A generated or reused
  implementation that failed static verification is refused at the workflow
  and the worker boundary. New approvals also record the script digest in the
  approval row, so a later artifact cannot re-baseline the approved bytes. The
  specification checksum is unchanged when `monitor_contract` is empty (old
  approvals remain valid); a non-empty contract is new information and
  requires a new approval.
- **EmbodiedAct link:** replayable evidence / exact code state (conceptual).
- **Expected benefit:** closes silent post-approval code substitution.
- **Complexity:** low.
- **Compatibility risk:** users who intentionally edit a generated script after
  generation are blocked until they regenerate/re-verify; this is the intended
  safety behavior and is reported explicitly.
- **Tests:** matching digest passes; edited digest raises before process start.
- **Success criteria:** edited script produces `implementation_integrity`
  failure with both checksums in the message.

## E4. Deterministic recovery policy (fast vs slow loop)

- **Confirmed problem:** W4 — no retry/repair/replan decision; dead
  `_REVISION_WORTHY_DECISIONS`.
- **Source location:** `application/workflow.py:47`, `application/services.py:2015`.
- **Solution:** new `application/recovery.py`:
  - execution failures → `retry_same_spec` (transient/timeout/stall),
    `repair_implementation` (perception/numeric/code defect), or `stop`;
  - report decisions → `advance_manuscript`, `adjust_parameters`,
    `revise_novelty`, `request_more_evidence`, or `stop`;
  - each decision records loop (`fast`/`slow`), rationale, evidence ids, and
    whether a new approval/user action is required.
  - persisted as a checksummed `recovery_decision` artifact and
    `research.recovery_decision` event.
- **EmbodiedAct link:** hot-fix vs local-adjust vs replan budgets; explicit
  terminal states; constraint-gated acceptance.
- **Expected benefit:** consistent, auditable next action; prevents blind
  patching; makes cross-skill feedback real and machine-readable.
- **Complexity:** medium.
- **Compatibility risk:** decisions are recommendations; no expensive action is
  auto-executed. Exit codes unchanged.
- **Tests:** table-driven policy tests covering supported/refuted/no-effect/
  implementation-failure/timeout/stall; workflow result includes recovery.
- **Success criteria:** 100% of enumerated decisions map to an action and
  rationale; no case returns an ambiguous `None`.

## E5. Persist operational failures in Findings Memory

- **Confirmed problem:** W3 — worker failure bypasses `FindingsMemory`.
- **Source location:** `application/services.py:2015-2110`,
  `application/findings.py:record_failure`.
- **Solution:** on non-completed worker result, record a failure finding with
  `failure_classification="operational_execution_failure"` (new optional
  argument; existing default unchanged), including monitor reason and job id.
- **EmbodiedAct link:** every abort is an evidence-bearing event.
- **Expected benefit:** next novelty cycle sees failed execution and avoids
  repeating it.
- **Complexity:** low.
- **Compatibility risk:** extra findings rows; retrieval ranks failures first by
  design.
- **Tests:** failed run creates one finding with exact spec/job lineage.
- **Success criteria:** finding exists after a forced perception abort.

## E6. Corpus-level conflict candidates and missing-evidence ledger (review)

- **Confirmed problem:** W6/W8 — only same-paper tagged contradictions; no
  corpus conflict map carried downstream.
- **Source location:** `review/matrix.py`, `application/review_publication.py`,
  `review/evidence_context.py`.
- **Solution:** new `review/conflicts.py` deterministic heuristic:
  - claim pairs from different papers with ≥2 shared content terms and opposing
    result polarity (improve/increase vs no/did-not/decrease/worse);
  - labelled `candidate_conflict_not_verified`, with shared terms and both
    claim/paper ids; never called a verified contradiction;
  - missing-evidence ledger: claims without page/section, papers without
    verified claims, failed reviews, abstract-only papers, empty counter-evidence.
  - write `review_audit.json`; add `cross_paper_conflicts` and
    `missing_evidence` to `review_corpus.json` and the evidence matrix.
  - novelty evidence context includes the top conflicts;
  - manuscript literature synthesis renders a "Conflicting evidence" subsection
    when conflicts exist.
- **EmbodiedAct link:** none direct; this is a scientific-quality adaptation of
  the same "observe intermediate state, don't wait for the final output" principle.
- **Expected benefit:** surfaces contradictory literature instead of silently
  averaging it; gives novelty an explicit conflict to resolve; makes missing
  evidence visible.
- **Complexity:** medium.
- **Compatibility risk:** additive artifact fields; heuristic is explicitly
  labelled and bounded (≤20).
- **Tests:** opposing claim pair detected; unrelated pair not detected;
  same-paper pair excluded; corpus and synthesis carry the field.
- **Success criteria:** synthetic conflict appears in review audit, corpus, and
  literature synthesis; no false positive on unrelated claims.

## E7. Consolidated research-state snapshot and explicit states

- **Confirmed problem:** W7 — no single inspectable state; stage dispositions
  are ephemeral.
- **Source location:** `application/workflow.py:_result`, new
  `application/research_state.py`.
- **Solution:** deterministic `build_research_state(project_id)` over existing
  tables; writes `research_state.json` artifact at the end of every workflow
  invocation (and exposes it in the result). States include
  `review_pending`, `novelty_pending`, `methodology_pending`, `design_pending`,
  `approval_pending`, `execution_running`, `operational_failure`,
  `analysis_pending`, `report_pending`, `manuscript_pending`, `complete`,
  `draft_gate_failed`. Includes latest recovery action and open questions.
- **EmbodiedAct link:** persistent execution state / explicit terminal states.
- **Expected benefit:** one machine-readable handoff object; easier debugging
  and evaluation; no new database.
- **Complexity:** low-medium.
- **Compatibility risk:** additive artifact; no schema change.
- **Tests:** state transitions on a synthetic project; snapshot contains exact
  lineage ids.
- **Success criteria:** state matches the durable rows at each stage.

## O1. Automatic re-entry into novelty/review after a refutation (not implemented)

- **Why deferred:** model calls and compute are expensive; the system should not
  silently start a new cycle. The recovery artifact records the exact
  recommendation and IDs; an explicit user resume is required.
- **Would require:** a new `workflow --feedback auto` authorization, loop budget,
  and duplicate-question detection.

## O2. GPU/telemetry heartbeat worker (not implemented)

- **Why deferred:** no GPU environment in this workspace; current local worker is
  the only configured worker. The monitor contract is transport-agnostic and can
  consume GPU metrics later.

## O3. LLM-on-stream consult (not implemented)

- **Why deferred:** deterministic checks are cheaper and sufficient for the
  confirmed failures. An L2 LLM consult can be added behind an opt-in flag when
  telemetry is ambiguous.

---

## Sequencing and gates

1. Docs/audit (this file) — complete before code.
2. E2/E3 (worker integrity/heartbeat) — low risk, independent.
3. E1 (perception + planner + verifier + schema).
4. E4/E5 (recovery + findings).
5. E6 (review) and E7 (state).
6. Full test suite; offline e2e; targeted new tests.
7. Hostile review of claims and diff; update docs.

No dataset, locked test set, evaluation criterion, or scientific conclusion is
modified. No destructive or paid experiment is run. All new behavior is
backward-compatible at the CLI and skill-name level.
