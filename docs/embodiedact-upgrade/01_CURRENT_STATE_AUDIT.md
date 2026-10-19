# AutoResearch current-state audit

Date: 2026-10-19
Repository: `/Users/mobashirsifat/autoresearch`
Baseline: commit `a53241c` (`fix: harden publication contract and venue gates`) plus two untracked paper-submission folders.
Baseline test run: `369 passed` in 22.61s (`.venv/bin/python -m pytest tests/ -q`).

This audit is based on source inspection and one fully offline end-to-end execution
(`tests/test_smoke_e2e.py`) through the real services. It is not inferred from README text.
It records the **pre-upgrade** state; see `06_IMPLEMENTATION_CHANGELOG.md` and
`09_HOSTILE_REVIEW_RESPONSE.md` for what changed and what remains.

---

## 1. What is actually implemented

The repository is not four independent prompts. It is a Python package
(`src/autoresearch`, ~28.6k lines) with a SQLite state store and a CLI wrapper. The
five installed skills (`review`, `novelty`, `experiment`, `manuscript`, `workflow`)
are thin instruction bridges to the Python backend:

```
OpenCode / Claude skill (SKILL.md)
        │  invokes stable CLI
        ▼
autoresearch skill <stage>  (Typer)
        ▼
Application services
  DiscoveryService → LiteratureReviewService → NoveltyService
  MethodologyFreezer → ExperimentService → ManuscriptService
        ▼
Domain modules (search, extraction, gap analysis, prior art, planning,
implementation, execution, ingestion, analysis, reporting, manuscript)
        ▼
SQLite (schema v16) + checksummed artifacts on disk + Findings Memory
```

### 1.1 Skill boundaries and interfaces

| Skill | Entry point | Inputs | Outputs | Durable state |
|---|---|---|---|---|
| `review` | `skill review` / `skill deep-research` → `LiteratureReviewService.review` (`application/services.py:815`) | project id, `top_k`, optional seed/catalog, publication track | `project_id`, `review_run_id`, reviewed/failed counts, `top_paper_ids`, gap, evidence matrix, review corpus, citation contexts | `pipeline_runs`, `papers`, `evidence_claims`, `paper_reviews`, `priority_scores`, `research_gaps`, `artifacts`, checkpoints |
| `novelty` | `skill novelty` → `NoveltyService.generate_portfolio` (`application/services.py:1559`) | project id, gap id | portfolio id, candidate ids, low/balanced/high-risk tiers, frozen methodology | `novelty_candidates`, `prior_art_assessments`, `novelty_critiques`, `novelty_portfolios`, `novelty_claim_ledgers`, `methodology_freezes` |
| `experiment` | `skill experiment plan/implement/approve/execute/report` → `ExperimentService` (`application/services.py:1655`) | candidate id, explicit checksum approval | spec, environment manifest, implementation verification, worker result, statistical analysis, report/decision, finding | `experiment_specs`, `environment_manifests`, `experiment_approvals`, `worker_jobs`, `worker_job_transitions`, `execution_results`, `statistical_analyses`, `experiment_reports`, `research_findings` |
| `manuscript` | `skill manuscript` → `ManuscriptService.generate` (`application/manuscript.py:506`) | project id, optional shared publication contract | Markdown/LaTeX/BibTeX/tables/manifest/PDF, readiness, venue lint, claim ledger, reviewer panel, submission checklist | `manuscripts`, artifacts |
| `workflow` | `skill workflow` → `WorkflowService.run` (`application/workflow.py:71`) | seed or project id, `until` stop stage, publication track | per-invocation stage dispositions + durable stage identities | Reuses all stage tables; writes `publication_contract.json`, `publication_gaps.json`, `stage_metrics.json` |

### 1.2 Representative end-to-end trace (verified by `tests/test_smoke_e2e.py`)

1. `WorkflowService.run` validates `until`, then calls `DiscoveryService.discover` for a seed
   (`workflow.py:129`). Discovery writes the seed, search events, papers, downloads, and a
   checksummed discovery artifact.
2. `LiteratureReviewService.review` creates a `deep_literature_review` run, fans out paper
   extraction (bounded semaphore, per-paper checkpoints), verifies each quote verbatim, ranks
   papers, runs three gap candidates + adversarial counter-evidence search + meta-selection,
   writes `gap-report.json/.md`, `review_corpus.json`, `citation_contexts.json`,
   `publication_gaps.json`, and `evidence-matrix.json` (`services.py:1169`).
3. A completed gap is reused on resume. `WorkflowService` builds the shared
   `publication_contract.json` and, with `--publication`, stops at review/methodology/design
   gates when the slice cannot support submission (`workflow.py:160`).
4. Novelty runs five strategies, deterministic cheap rejection, concurrent prior-art check
   and critic ensemble, one revision round, diversity-aware portfolio selection
   (`novelty/pipeline.py`). `MethodologyFreezer` writes a versioned, checksummed
   `methodology-v{N}.json`.
5. Design creates an `ExperimentSpec` grounded in the gap's claims and the frozen
   methodology, with dataset checksum and git commit computed from the real target
   (`experiments/planner.py`). Implementation is generated + statically verified
   (`experiments/implementation.py`); failures are recorded as findings.
6. Execution requires an exact-spec approval checksum (`experiments/policy.py:29`).
   `LocalTypedWorker.run` executes the approved action, persists stdout/stderr to
   `run_outputs/worker_result_<job>.json`, then `MeasurementIngester` identity-checks the
   emitted JSON, `StatisticalAnalyzer` recomputes tests, `ScientificReportWriter` writes the
   decision, and `FindingsMemory` records the outcome.
7. Manuscript assesses readiness *before* prose (`publication_readiness.py:357`), builds a
   contribution contract, full-corpus literature synthesis, sentence-level claim ledger,
   deterministic reviewer panel, venue lint, body hygiene, and submission checklist. A
   `draft` is never reported as submission-ready.

### 1.3 Skill dependency map

```
review ──gap_id──► novelty ──candidate_id──► experiment ──spec/report──► manuscript
  ▲                    ▲                          │                         │
  │                    │                          │                         │
  └──── Findings Memory (all outcomes, incl. refutations) ◄──────────────────┘
       shared publication contract / publication gaps (workflow-owned)

workflow coordinates the forward path; no automated reverse path exists in code.
```

The dependency is real and typed: `research_gaps.id → novelty_candidates.gap_id →
methodology_freezes.candidate_id → experiment_specs.methodology_freeze_id →
statistical_analyses.spec_id → experiment_reports.analysis_id → manuscripts.lineage_json`.
The manuscript refuses to run without an analyzed report rather than filling the gap
(`workflow.py:431`). This is a genuine strength.

### 1.4 What is genuinely implemented vs described

| Capability | Status | Evidence |
|---|---|---|
| Verbatim-verified literature claims | Implemented | `review/extraction.py:260`; quote check in extraction path |
| Counter-evidence search for gaps | Implemented | `review/gap_analysis.py` (`_find_counter_evidence`) |
| Evidence matrix + coverage record | Implemented | `review/matrix.py` |
| Cross-paper contradiction detection | Partial | Matrix only surfaces claims the extractor itself tagged `contradiction`/`counter_evidence`/`limitation`; no cross-paper conflict pass |
| Prior-art assessment | Implemented | `novelty/prior_art_checker.py`; internal claims + external sources; corpus-only results downgraded to `expert_review_required` |
| Methodology freeze | Implemented | `novelty/methodology.py`; versioned checksummed artifact |
| Static implementation verification | Implemented | `experiments/implementation.py`; syntax, imports, identity, seeds, measurement contract, artifacts, mechanism coverage |
| Exact approval gate | Implemented, but scope gap | `experiments/policy.py`; checksum covers spec/actions/worker, **not script bytes** |
| Durable worker jobs + leases | Partial | `workers/local_typed.py`; lease/heartbeat/transition columns exist, but heartbeat is not maintained during a run |
| Runtime perception during execution | Absent | `LocalTypedWorker.run` calls `await process.communicate()` and only checks exit code/timeout |
| Early abort on numeric failure | Absent | No probe protocol, no monitor contract, no streaming reader |
| Operational failure memory | Partial | Post-execution ingestion failures are recorded; worker/process failures are raised before `FindingsMemory.record_failure` |
| Recovery decision policy | Absent | `_REVISION_WORTHY_DECISIONS` in `workflow.py:47` is dead documentation |
| Cross-skill reverse feedback | Absent | Workflow stops; no `experiment → review/novelty` action is recorded or executed |
| Manuscript evidence gates | Implemented | readiness, claim ledger, reviewer panel, hygiene, venue lint |
| Shared research-state snapshot | Absent | State is queryable in SQLite, but no consolidated machine-readable project state artifact |
| Explicit workflow state machine | Partial | Stage dispositions are per-invocation and not persisted; worker transitions are persisted |

---

## 2. Confirmed weaknesses (source-supported)

### W1. Execution is a black box until it finishes or times out

`workers/local_typed.py:105` launches the process and waits on
`asyncio.wait_for(process.communicate(), timeout=...)`. Nothing parses the stream while the
process runs. A training run that emits `loss=nan` at step 50 and continues for three hours
consumes the full budget before any check. The `ExecutionResult` path used by
`experiments/executor.py` has the same `communicate()` behaviour.

**Impact:** wasted compute; late detection; no intermediate evidence; the system cannot
distinguish "still healthy but slow" from "diverged 40 minutes ago".

### W2. Heartbeats and process identity are not maintained during execution

The schema stores `heartbeat_at`, `lease_expires_at`, `process_id`, `process_group_id`, and
`last_log_offset` (`repository.py:1448`). `LocalTypedWorker.heartbeat()` exists, but the run
loop never calls it; `process_id`/`process_group_id` are never written. `recover_stale`
therefore treats a healthy 3-hour job as stale after 120s if invoked concurrently.

**Impact:** false stale detection, duplicate execution risk, and no observability for long runs.

### W3. Worker failures bypass the Findings Memory

In `ExperimentService.execute_candidate` (`services.py:2015`), a non-`COMPLETED` worker result
raises `RuntimeError("execution_failed: ...")` before the ingestion/analysis block. Only
`IntegrityFailure`/`ValueError` inside that block calls `findings.record_failure`. A crashed,
timed-out, or numerically-aborted run leaves a worker row but no structured scientific finding
and no lesson for the next novelty cycle.

**Impact:** failed-run evidence is lost, violating the project's own "preserve failed-run
evidence" claim.

### W4. There is no decision policy for recovery

The workflow records `execution_failed` and stops. `_REVISION_WORTHY_DECISIONS` is declared
but unused. Nothing decides whether a failure is operational (retry/repair) or scientific
(return to novelty/review), and nothing records why.

**Impact:** the user must infer the next action; repeated failures can be patched locally
without recognizing that the hypothesis or method has failed.

### W5. Approval does not bind the executed script content

`specification_checksum` hashes the spec dataclass, action list, and worker
(`experiments/policy.py:29`). It does not hash `scripts/run_experiment.py`. A generated script
can be edited or regenerated after approval without invalidating the checksum. The
implementation artifact stores a checksum (`implementation.py:_record`), but execution never
compares against it.

**Impact:** the approved action can execute code different from the verified implementation,
which breaks the exact-spec approval contract.

### W6. No cross-paper contradiction index

`review/matrix.py` populates `contradictory_findings` with claims from the *same paper* whose
`claim_type` is `contradiction`, `counter_evidence`, or `limitation`. The extraction prompt
allows arbitrary `claim_type`, but there is no deterministic pass that surfaces opposing
results from different papers. The gap analyzer performs an adversarial counter-evidence
search for the selected gap, which is valuable but narrower than a corpus-level conflict map.

**Impact:** a literature review can miss "Paper A reports gains, Paper B reports no gains
under a compatible setting" unless the extractor happened to tag it.

### W7. No consolidated research-state artifact and no persisted stage states

`WorkflowService._result` writes `stage_metrics.json` but stage dispositions are per-call.
`pipeline_runs`, `worker_job_transitions`, and artifacts contain the truth, but there is no
single inspectable snapshot that answers: current question, novelty status, experiment status,
latest finding, open contradictions, pending recovery actions, manuscript readiness.

**Impact:** handoffs are implicit DB queries; users and downstream skills must reconstruct
state; evaluation of "what changed after a run" is manual.

### W8. Review corpus does not carry conflict/missing-evidence structure downstream

`review_corpus.json` carries themes, method families, datasets, years, coverage, and
publication gaps. It does not carry corpus-level conflicts or a missing-evidence ledger. The
manuscript's literature synthesis therefore cannot discuss conflicting evidence even when the
evidence matrix contains it.

### W9. Novelty cannot see corpus-level conflicts

Novelty receives the gap's supporting/counter claims and findings memory
(`novelty/pipeline.py:50`). It does not receive a corpus-level conflict map, so a candidate
that resolves a known conflict is not explicitly grounded in that conflict.

---

## 3. Weaknesses that remain hypotheses (not proven by inspection)

These require live runs or user studies; they are not counted as confirmed:

- **H1.** Review extraction may systematically over-tag `limitation` claims as contradictions.
- **H2.** The five-candidate novelty width may still under-cover cross-domain mechanisms.
- **H3.** Manuscript model-writer prose may pass the numeric ledger while making an
  unsupported causal claim that the deterministic reviewer panel does not detect.
- **H4.** Long review runs may hit provider rate limits before the per-source retry behavior
  is exercised at scale.
- **H5.** Reusing a completed gap after new papers are discovered may produce a stale
  question; resume currently prioritizes the latest completed gap.

---

## 4. Bottlenecks and context-loss points

1. **Execution → science:** the largest information loss. Intermediate telemetry is discarded;
   only final stdout survives.
2. **Failure → memory:** process failures never become findings, so novelty cannot avoid them.
3. **Review → novelty/manuscript:** conflict evidence is not a first-class handoff object.
4. **Workflow → operator:** stage status is ephemeral; `status` queries are read-only but not a
   consolidated state contract.
5. **Approval → code:** approval binds the plan, not the bytes that run.

## 5. What should not be changed

- The four skill names and CLI verbs are stable and tested; preserve them.
- The publication-readiness gate, claim ledger, and deterministic reviewer panel are strong;
  do not replace them with model judgment.
- The exact-approval gate and fixture/provenance rejection must remain.
- SQLite + checksummed artifacts is sufficient; no new database/framework is justified.
- No automatic expensive re-execution without explicit approval.

## 6. Audit conclusion

The system is already a serious evidence-gated pipeline, not a prompt collection. Its main
gap relative to EmbodiedAct is **not planning or reflection quality**; it is the absence of a
runtime perception and recovery layer around experiment execution, plus the absence of a
first-class conflict/recovery handoff between stages. The upgrade should add those mechanisms
deterministically and keep the existing scientific gates intact.
