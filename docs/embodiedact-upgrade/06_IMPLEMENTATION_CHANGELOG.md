# Implementation changelog

Date: 2026-10-19. The upgrade is committed on top of `a53241c`; the exact
commit hash is in `git log`. The two untracked `paper_submission_*` directories
were present before this work and were not modified. EmbodiedAct was inspected at
`3689e9118376e7d0906ff021a48d36204da2ee68`. Source/test/README content hash
at validation time:
`a969415d8ddbb5be2689cee2f67ac83721a6648ee5378ef0e798b0735e21a85e`
(computed over the changed/untracked files under `src/`, `tests/`, and the
README; docs are excluded so updating this record does not change the hash).

## New modules

| File | Purpose |
|---|---|
| `src/autoresearch/experiments/perception.py` | Probe line parser (`AUTORESEARCH_PROBE:` / legacy `EA_PROBE:`), monitor contract (≤3 signals), safe AST predicate compiler, deterministic severity ladder, bounded telemetry history, monitor report. |
| `src/autoresearch/application/recovery.py` | Fast/slow recovery policy, deterministic decision artifact + event, latest-decision reader. |
| `src/autoresearch/application/research_state.py` | Consolidated read-only project snapshot with explicit state names and open questions. |
| `src/autoresearch/review/conflicts.py` | Cross-paper conflict candidates and missing-evidence ledger. |

## Modified files

| File | Change | Why |
|---|---|---|
| `src/autoresearch/domain/models.py` | `ExperimentSpec.monitor_contract: Mapping[str, Any]` (default empty). | Pre-registered runtime signals without breaking old specs. |
| `src/autoresearch/storage/repository.py` | Schema v17: additive `experiment_specs.monitor_contract_json` and `required_artifacts_json`; idempotent column-by-column migration; save/load round-trip; cancellation is terminal on late `finish_run`. | Persist the contract; old rows default to `{}`. Also fixes a latent persistence gap where a declared artifact subset reverted to the full default after reload. |
| `src/autoresearch/experiments/planner.py` | Optional `monitor_contract` schema, prompt field, spec construction. | Let the planner declare early-failure signals, not invent measurements. |
| `src/autoresearch/experiments/implementation.py` | Prompt requires probe lines when a contract exists; verifier check 7 (`monitor_contract_probed`) requires `name` + `value`; a failed generated script is saved as `experiment_implementation_unverified`, never `experiment_implementation`. | Prevent “contract exists but code emits nothing” and block failed code from claiming the verified artifact type. |
| `src/autoresearch/workers/local_typed.py` | Line-streaming readers, perception feed, fatal kill, watchdog/stall, heartbeat task, pid/pgid/offset, implementation-integrity check, failed-result persistence, monitor artifact, explicit error categories. | Turn execution from a black box into an observable, bounded loop. |
| `src/autoresearch/application/services.py` | On every failed approved execution: recovery decision + operational finding; on report: report recovery decision; `recovery_for_candidate`. | Make failures informative and give a consistent next action. |
| `src/autoresearch/application/findings.py` | `record_failure(..., failure_classification=...)`; lesson/next-action text now depends on the classification. | Distinguish operational failure, implementation-generation failure, and measurement-ingestion failure without misleading the next novelty cycle. |
| `src/autoresearch/application/workflow.py` | Recovery fields in experiment dispositions; `research_state` + `pending_recovery` + artifact path in every result. | Persist the handoff and make the next action visible. |
| `src/autoresearch/application/review_publication.py` | Corpus gains `cross_paper_conflicts` and `missing_evidence`. | Carry the audit downstream. |
| `src/autoresearch/review/evidence_context.py` | Latest conflict candidates included in novelty/planner context and rendered. | Ground candidate generation in disagreements. |
| `src/autoresearch/review/matrix.py` | Evidence matrix includes audit conflicts and missing evidence. | One decision-useful artifact. |
| `src/autoresearch/application/literature_synthesis.py` | `conflicts`/`missing_evidence` fields; “Conflicting evidence” manuscript subsection. | Do not silently average contradictory literature. |
| `src/autoresearch/application/services.py` review path | Write `review_audit.json` before publication; include `review_audit_path` in outcome/CLI JSON. | Make the audit inspectable. |
| `src/autoresearch/cli/skills.py` | Add `skill state --project`; review JSON exposes audit/corpus/context paths. | Operator-facing state without a new database. |
| `README.md`, five `SKILL.md` files | Document runtime perception, recovery, conflict audit, research state. | Documentation matches code. |

## Design decisions and compatibility

1. **Deterministic monitoring, no LLM at stream rate.** The perception layer uses
   numeric rules and a sandboxed predicate compiler. This avoids cost and
   nondeterminism; it also means the monitor cannot “interpret” ambiguous
   telemetry. Suspect observations are evidence, not aborts.
2. **No automatic re-execution or re-entry.** A fast-loop decision is recorded,
   but execution still needs a fresh exact-spec approval; novelty/review re-entry
   needs an explicit resume. This protects compute budgets and scientific
   integrity.
3. **Approval checksum compatibility.** The checksum payload omits an empty
   `monitor_contract`, so every pre-v17 approval keeps its original checksum.
   A non-empty contract is new information and requires a fresh approval. New
   approvals also store the script digest in `approved_actions_json`; the
   worker refuses bytes that differ. Legacy approvals (no binding) fall back to
   the latest verified artifact and reject an artifact created after approval.
4. **Additive schema only.** Two additive columns (one defaulted JSON object,
   one nullable); migration runs once. Older databases and older specs load
   with `monitor_contract = {}` and the full default artifact set, and behave
   as before (except heartbeat/integrity improvements).
5. **Conflict candidates are not contradictions.** The heuristic requires
   different papers, ≥2 shared content terms, and opposing polarity; it is
   labelled and capped. No claim of semantic entailment.
6. **Findings Memory classification is explicit.** `operational_execution_failure`
   is distinct from a refuted hypothesis; this prevents a crash from being read
   as scientific evidence against a method.
7. **Required artifacts behavior fixed at the load boundary.** `monitor_contract`
   is loaded by keyword so it cannot shift into `required_artifacts` (a latent
   positional-construction hazard exposed by schema v17).

## Behavior changes users may notice

- Long runs now write heartbeat updates every 5 seconds and store pid/pgid.
- A timeout ends `FAILED` with category `TIMEOUT`, not `CANCELED`.
- Running an edited script after static verification fails with
  `implementation_integrity`; regenerate/re-verify and re-approve.
- A failed execution (including a measurement-ingestion failure) now leaves a
  `recovery_decision` artifact and a Findings Memory row; the workflow result
  has `pending_recovery`.
- `experiment cancel` now works across processes: it signals the recorded
  process group, marks the job `CANCELED`, and a late worker finish cannot
  resurrect the run as completed.
- Approval previews show the monitor contract, implementation digest, and
  verification status. New approvals bind that digest.
- Review JSON has a `review_audit_path`; the manuscript may gain a
  “Conflicting evidence” subsection.
- Workflow results have `research_state` and an artifact path.
