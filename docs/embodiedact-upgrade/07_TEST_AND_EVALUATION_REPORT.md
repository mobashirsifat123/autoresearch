# Test and evaluation report

Environment: macOS, Python 3.14.5, repository `.venv`, no network calls, no
paid provider calls, no GPU. All experiment tests use local Python subprocesses.
Base commit `a53241c`; EmbodiedAct commit `3689e91`; source/test/README content
hash `a969415d8ddbb5be2689cee2f67ac83721a6648ee5378ef0e798b0735e21a85e`.

## 1. Commands actually executed

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/ -q` (before changes) | **369 passed**, 3 warnings, 22.61s |
| `.venv/bin/python -m pytest tests/ -q` (after changes) | **412 passed**, 3 warnings, ~30s |
| `.venv/bin/python -m pytest tests/test_execution_perception.py tests/test_runtime_perception_worker.py tests/test_recovery_policy.py tests/test_review_conflicts.py tests/test_research_state.py tests/test_perception_workflow_integration.py tests/test_schema_migration.py tests/test_policy_checksum.py -q` | **43 passed** |
| `.venv/bin/python -m compileall -q src/autoresearch tests` | OK |
| `autoresearch skill experiment cancel --run-id nope` (temp cwd) | exit 4 `run_not_found`; no `NameError` |
| `ruff check ...` | Not run: `ruff` is not installed in the environment (`NO_RUFF`) |
| `ty check ...` | Not run: `ty` is not installed |

The 3 warnings are the pre-existing
`DeprecationWarning: 'asyncio.get_event_loop_policy' is deprecated and slated
for removal in Python 3.16` emitted by `tests/test_cli_five_skills.py`; they are
unrelated to this change.

## 2. New tests and what they prove

| Test file | Tests | Proves |
|---|---:|---|
| `test_execution_perception.py` | 7 | Probe parsing, contract bounds/cap, AST safety, NaN/bound/trend/guard ladder, bounded monitor summary, unobserved/malformed probes, invalid predicate status. |
| `test_runtime_perception_worker.py` | 16 | Contract persists; NaN probe aborts a 30s script early; healthy stream completes; timeout is `FAILED/TIMEOUT`; failed/reused-unverified verification refused; failed generation cannot claim the verified artifact type; approval binding rejects changed bytes; attempts increment; long line and >1MB/300KB tail survive; tampered result rejected by the ledger; edited script rejected. |
| `test_recovery_policy.py` | 7 | Fast vs slow classification, refuted → novelty, null → redesign, data limitation → review, support → manuscript, artifact failure → repair; cancellation terminal; artifact round-trip. |
| `test_review_conflicts.py` | 5 | Opposing claims flagged; same-paper/unrelated not flagged; reduce-harm/without-loss polarity; missing-evidence ledger; conflict rendering. |
| `test_research_state.py` | 3 | Exact lineage in the snapshot; review audit persisted; recovery advances state/open questions; completed report writes `advance_manuscript`/`revise_novelty` recovery. |
| `test_perception_workflow_integration.py` | 1 | Full workflow: fatal probe → early stop → `execution_failed` → `repair_implementation` → `research_state=recovery_required` → operational finding. |
| `test_schema_migration.py` | 2 | Synthetic v16→v17 migration; partial v17 migration is idempotent. |
| `test_policy_checksum.py` | 2 | Empty monitor contract preserves the pre-upgrade checksum; non-empty contract changes it. |

## 3. Observed behavior (local microbenchmark, not a scientific benchmark)

A single local run of the new worker with an instrumented script:

| Case | State | Category | Observed wall time |
|---|---|---|---|
| NaN probe then `time.sleep(30)` | FAILED | `PERCEPTION_ABORT` | **0.043 s** |
| Four decreasing loss probes then final JSON | COMPLETED | — | **0.038 s** |

The first case would have consumed 30 seconds without runtime perception (or
the full approval timeout). This is a mechanism check, not a claim about ML
training speed.

## 4. Scenario coverage against the requested evaluation suite

| Requested scenario | Coverage | Evidence |
|---|---|---|
| Literature claim conflicting with source | Existing verbatim-quote verification retained; new audit surfaces cross-paper candidates. | Existing extraction tests + `test_review_conflicts.py` |
| Novelty claim contradicted by a close paper | Existing prior-art path retained; candidate context now includes corpus conflicts. | Existing novelty tests + context rendering test |
| Recoverable experiment execution error | `TIMEOUT`/`STALL` → retry once, then adjust parameters; integrity/code error → repair. | `test_recovery_policy.py`, worker timeout test |
| Successful run contradicting hypothesis | `refuted`/`mechanism_contradicted` → slow loop → novelty. | `test_recovery_policy.py` |
| Parameter tuning vs flawed strategy | Reachable report decisions: `refuted` → revise novelty; `insufficient_evidence` → redesign (handled from the writer's integrity failure); `implementation_failure` → repair. `no_effect`/`optimization_failure`/`data_limitation` mappings exist in the policy table but the current deterministic writer does not emit those labels (future analyzer extension). | `test_recovery_policy.py` + report writer inspection |
| Experiment with incomplete provenance | Existing identity/measurement integrity gates retained; failed execution now also persists a finding. | Existing ingestion/integrity tests + integration test |
| Manuscript result not reproducible | Existing claim ledger/number audit retained, unchanged. | Existing manuscript tests |
| Missing citation / unsupported conclusion | Existing citation/claim audits retained, unchanged. | Existing audit tests |
| Failure requiring reverse information flow | Workflow records `revise_novelty`/`request_more_evidence` with evidence ids and exposes `pending_recovery`. | Integration test + state test |

## 5. What was **not** tested

- Live Semantic Scholar/OpenAlex/Crossref/arXiv/OpenReview calls.
- A live LLM planner generating a `monitor_contract` (the schema/prompt/parser
  and verifier are tested with deterministic fixtures).
- Real GPU/ML training, CUDA telemetry, or multi-worker scheduling.
- The model-assisted manuscript writer with a conflict section; the
  deterministic synthesis path is tested.
- A real v16 production database copy (the migration test builds a synthetic
  v16 schema).
- Long-running (hours) heartbeat behavior; the heartbeat task is exercised
  only for sub-second runs, though the code updates every 5 seconds.
- Worker-level bound/stall/guard aborts: the unit ladder tests them, but the
  end-to-end worker test exercises a NaN fatal sample.
- A live two-process cancellation test; the CLI invocation and terminal-state
  guard are tested separately.
- Human expert review of novelty comparisons; no expert rating was performed.
- Imported-module integrity and self-reported probe cross-checks remain future
  work. Concurrent execution is prevented by the lease CAS, but no unique
  index prevents orphan queued rows.
- A live second process cancelled during the exact lease→spawn window; the
  CAS/terminal/heartbeat logic is tested separately.
- An end-to-end `INSUFFICIENT_EVIDENCE` run through the real analyzer (the
  handler is code-inspected; the policy mapping is unit-tested).

Therefore no claim is made about improved scientific discovery rates, novelty
quality, or manuscript acceptance. The verified improvements are operational:
earlier detection, preserved failure evidence, explicit recovery decisions,
script-content integrity, and traceable conflict/missing-evidence state.

## 6. Metrics that can now be measured on real projects

The upgrade makes these measurable without inventing baselines:

- **Invalid-run detection lead time:** first fatal probe timestamp vs process
  start/end (`monitor report`).
- **Wasted compute avoided:** elapsed time/estimated cost of aborted runs
  relative to their approved timeout.
- **Operational failure retention:** percentage of failed jobs with a matching
  finding (`operational_execution_failure`).
- **Recovery decision coverage:** percentage of failures with an explicit
  `recovery_decision` and `next_stage`.
- **Implementation integrity:** percentage of executions whose script digest
  matches the verified artifact.
- **Conflict traceability:** number of conflict candidates with both claim ids
  and source papers; expert-confirmed precision can be added later.
- **Manuscript evidence coverage:** unchanged claim-ledger gate; now also
  whether conflicting evidence is acknowledged.
