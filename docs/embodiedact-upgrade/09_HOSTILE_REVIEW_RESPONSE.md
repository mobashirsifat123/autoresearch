# Hostile review response

An independent hostile review was run against the working tree and tests. Its
verdict was **REJECT — major revision** with four blockers. This document
records what was fixed, what was only partially fixed, and what remains a
declared limitation. No blocker is claimed fixed without a test or a direct
code path.

## Blockers

| ID | Finding | Disposition | Evidence |
|---|---|---|---|
| **B1** | A generated script that failed static verification could execute; new implementation artifacts could re-baseline the approved digest. | **Fixed (including the `reused` path).** Failed sources are saved as `experiment_implementation_unverified`; any non-`STATIC_VERIFIED` verification artifact is refused, including `status="reused"`; the workflow re-verifies an existing script (deterministically, no model call) and stops on failure; identical source/verification bytes are deduplicated so re-verification after approval cannot create a new implementation artifact; new approvals bind the script digest. | `test_failed_generation_cannot_claim_the_verified_artifact_type`, `test_failed_static_verification_is_refused`, `test_reused_but_unverified_script_is_refused`, `test_approval_binding_rejects_a_changed_script`, workflow implementation gate |
| **B2** | Adding `monitor_contract` to `asdict` invalidated every pre-upgrade approval. | **Fixed.** `specification_checksum` omits an empty `monitor_contract`, preserving the exact pre-upgrade payload/hash. A non-empty contract changes the hash by design (new information). | `test_policy_checksum.py` |
| **B3** | Half the recovery table is unreachable from the real report writer; ingestion failures had no recovery decision; report output lacked recovery. | **Fixed for the reachable labels.** Ingestion failure writes a recovery decision and a measurement finding. `INSUFFICIENT_EVIDENCE` is caught before the generic ingestion handler and writes the slow-loop `redesign_experiment` decision plus a scientific finding; the direct report path does the same. `report_for_candidate` includes `recovery`. The remaining policy labels (`no_effect`, `optimization_failure`, `data_limitation`, `cost_prohibitive`, `domain_specific`, `mechanism_contradicted`) are future analyzer extensions and are labelled as such. | ingestion path, report path, docs/07 §4 |
| **B4** | `experiment cancel` raised `NameError`; a live worker kept running; late bookkeeping could mark the run completed. | **Fixed (with a residual untested two-process timing path).** The undefined kwarg is removed; `cancel_run` signals only a **fresh-heartbeat** process group, escalates to SIGKILL after 2s, and marks jobs `CANCELED`; `finish_run` cannot overwrite a cancelled run; the worker re-checks the job before spawn and immediately after spawn; heartbeat/offset writes are targeted and cannot overwrite `CANCELED`; the lease is a compare-and-swap; a second worker cannot lease the same queued job. | CLI invocation (`run_not_found`, no `NameError`), `test_cancellation_is_terminal...`, `test_heartbeat_does_not_resurrect_a_cancelled_job`; live two-process timing remains manual/future |

## Major findings addressed

| ID | Disposition |
|---|---|
| M1 unbounded timeout retry | **Fixed.** Worker attempt count is cumulative across jobs for the same spec/checksum; the second timeout reaches `adjust_parameters`. `test_repeated_timeouts_increment_the_attempt_budget`. |
| M2 1MB cap / 64KiB line | **Fixed.** Stream limit raised to 2MB; the tail preserves the last complete stdout line even when it exceeds the 200KB tail budget, so a 300KB measurement JSON survives a >1MB log. `test_a_long_single_line...`, `test_output_over_the_cap_preserves_a_large_tail_measurement`. |
| M3 tampered stored result | **Fixed for tampering.** The result digest is stored in the append-only artifact ledger and must match the workspace file; editing both `stdout` and the local checksum still fails. A missing result still re-executes the same approved deterministic action (pre-existing behavior) but now emits `worker.result_lost`; requiring a new approval remains future policy. |
| M4 bookkeeping replaces failure | **Fixed.** Recovery/state bookkeeping is best-effort and logs; the original execution error is preserved. |
| M5 state project-wide / completed-but-unanalyzed | **Partially fixed.** Recovery is run-scoped to the latest spec run; `COMPLETED` + no analysis → `analysis_pending`; `QUEUED` → `execution_queued`; finding counts are SQL counts. A re-run hidden by an older report/manuscript is not fully solved. |
| M6 false abort on noisy telemetry | **Not changed by design.** NaN/Inf and bound breaches are fatal immediately (matching EmbodiedAct); trends require persistence. This is documented as a tuning risk; a real-telemetry false-positive study is roadmap item 1. |
| M7 gameable/silent monitors | **Partially fixed.** Reports include `unobserved_monitors`, `predicate_status`, `malformed_probe_lines`; verifier requires `name`+`value`; invalid predicates are visible as `not_compiled`. Probes remain self-reported and `cadence` is not enforced. |
| M8 conflict polarity | **Fixed for the named cases.** Polarity/terms use the verbatim excerpt; "reduces error" and "without loss" are positive; `confidence` renamed `heuristic_score`. Adversarial tests added. The heuristic remains uncalibrated and English-centric. |
| M9 audit run scope | **Fixed for manuscript/novelty.** The synthesis filters by the selected review run and drops papers outside the corpus; novelty context receives the gap run id. `research_state` remains project-wide by design. |
| M10 selective reporting | **Partially fixed.** The report now records `failed_runs` and adds a limitation row when attempts failed/stalled/were cancelled. A full preregistration/attrition ledger is future work. |
| M11 misleading failure lesson | **Fixed.** Lesson/next-action text now depends on `operational_execution_failure`, `implementation_generation_failure`, or ingestion failure. |
| M12 entry-script-only integrity | **Not fixed.** Documented limitation; imported modules and `run_python_module`/`pytest` actions are not hashed. |
| M13 process cleanup | **Partially fixed.** Unexpected exceptions terminate the active process group; `killpg` is feature-detected with a terminate fallback. Grandchild-only survival and a post-spawn fault-injection test remain future work. |
| M14 concurrent duplicate runs | **Substantially fixed.** Two submitters can still insert rows, but the lease is a compare-and-swap and only one can execute; the loser raises `duplicate_execution`. No unique index, so orphan rows are possible. |
| M15 approval preview hides contract/digest | **Fixed.** Preview includes `monitor_contract`, `implementation_checksum`, `implementation_verified`, `implementation_status`; CLI displays them and stores the digest. |
| M16 migration not idempotent | **Fixed.** v17 applies column-by-column with `PRAGMA table_info` checks; a partial-migration test passes. Version-row atomicity across a process crash is still not a strict transaction. |
| M17 `required_artifacts=[]` | **Clarified, not changed.** Empty means “use the full standard contract” by design (planner comment); only non-empty subsets are persisted. Tested for a non-empty subset. |
| M18 `_row` swallows all errors | **Fixed.** Only `sqlite3.Error` is tolerated; programming errors surface. |
| M19 missing monitor artifact path | **Fixed.** The payload path is empty and `monitor_error` is recorded when the file cannot be written. |
| M20 stall/perception race | **Fixed.** First writer wins; the watchdog returns if an abort category is already set. |

## Residual risks that must be stated in any paper/claim

1. Probes are emitted by the generated code; a dishonest implementation can
   emit healthy-looking values. Cross-checking the last probe against ingested
   metrics is not implemented.
2. A bound breach is fatal on one sample. Thresholds are planner-authored and
   shown at approval, but have not been calibrated on a real training log.
3. Implementation integrity covers the entry script, not imported modules.
4. Concurrent execution of the same spec is not prevented at the database
   level.
5. Reverse-loop actions are recommendations; re-entry requires explicit user
   action. There is no automatic novelty/review loop.
6. Conflict detection is a heuristic retrieval aid, not semantic entailment,
   and has no expert-labeled precision estimate.
7. Legacy `worker_result` files created before v2 lack a ledger checksum and
   are accepted after internal checksum validation; only new results require
   the artifact-ledger match.
8. A missing `implementation_verification` artifact is treated as “no
   verification exists” (the worker still enforces approval binding and
   implementation bytes); it is not treated as verified.
9. Cancellation is tested at the terminal-state and heartbeat layers, not with
   a live second process during the exact lease→spawn window.

These limits are reflected in `05_SKILL_IMPROVEMENT_MATRIX.md`,
`07_TEST_AND_EVALUATION_REPORT.md`, and `08_NEXT_STEP_ROADMAP.md`.
