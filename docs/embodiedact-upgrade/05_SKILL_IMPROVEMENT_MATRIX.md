# Four-skill improvement matrix

The skill names remain `review`, `novelty`, `experiment`, `manuscript` (plus the
coordinating `workflow`). Public CLI verbs are unchanged; only additive outputs
and one additive `skill state` command were introduced.

| Skill | Original behavior | Confirmed weakness | Implemented improvement | Tests | Remaining gap |
|---|---|---|---|---|---|
| **review** | Multi-source discovery, verbatim evidence extraction, parallel ensemble, ranking, 3 gap candidates, adversarial counter-evidence search, meta-selection, evidence matrix, publication corpus. | Cross-paper contradictions only appear if the extractor happened to tag them; no corpus-level conflict or missing-evidence ledger carried downstream. | Deterministic `review_audit.json`: bounded cross-paper **conflict candidates** (shared terms + opposing polarity, explicitly not verified contradictions); missing-evidence ledger (no page/section, no claims, abstract-only, failed reviews, absent counter-evidence); fields added to review corpus, evidence matrix, novelty context, and manuscript synthesis. | `tests/test_review_conflicts.py` (5 tests) + full suite | Conflict detection is heuristic and English-centric; no semantic entailment; no automatic dataset/metric compatibility check. |
| **novelty** | 5 strategies, deterministic cheap rejection, prior-art ensemble, 3-perspective critique, revision round, diversity-aware portfolio, methodology freeze, Findings Memory retrieval. | Candidate generation did not see corpus-level disagreements; a candidate that “resolves a conflict” could not ground that claim. | Evidence context now includes the newest review audit's top conflict candidates; prompt renders them as candidates needing verification. Refuted/operational findings already feed generation and now include execution failures. | Existing novelty tests + `test_review_conflicts.py` rendering test | No claim-by-claim mechanism matrix beyond the existing prior-art assessment; no automated re-entry after a refutation (deliberate). |
| **experiment** | Spec planning, power analysis, adversarial check, generated + statically verified code, exact approval, worker execution, identity-checked ingestion, statistical analysis, report, Findings Memory. | Execution buffered all output; no early abort; heartbeat not maintained; script bytes not bound; process failure bypassed Findings Memory; no recovery decision. | Optional monitor contract (≤3 signals) in planner/spec; probe line protocol; deterministic severity ladder; early process-group kill; monitor artifact; suspect evidence retained; heartbeat/pid/offset; failed-verification refusal; approval-bound script digest; operational failure finding; fast/slow recovery policy with rationale and evidence; aborted-attempt count in the report. | `test_execution_perception.py`, `test_runtime_perception_worker.py`, `test_perception_workflow_integration.py`, `test_recovery_policy.py`, `test_policy_checksum.py` (33 tests) | No GPU telemetry adapter; no automatic code hot-fix (requires a new verification + approval); no multi-worker scheduler; probes are self-reported and integrity covers the entry script only. |
| **manuscript** | Readiness gate, contribution contract, full-corpus synthesis, claim ledger, venue lint, reviewer panel, body hygiene, submission checklist. | Literature synthesis could not discuss conflicting evidence even when the corpus contained it. | Synthesis reads the newest review audit and adds a **Conflicting evidence** subsection with both statements and an explicit “heuristic candidates, not verified contradictions” caveat. | `test_review_conflicts.py` synthesis test + full manuscript suite | The claim ledger is numeric/citation-oriented; it does not yet perform semantic entailment of every causal sentence. |
| **workflow** | Durable forward loop with reuse, publication gates, approval gate, stage metrics. | Stage dispositions were ephemeral; no consolidated state; no reverse feedback; no recovery reason. | `research_state.json` at every invocation; `research_state` + `pending_recovery` in the result; recovery fields on experiment dispositions; `autoresearch skill state --project`; reverse recommendations (`revise_novelty`, `request_more_evidence`, `redesign_experiment`, `stop`) recorded with evidence. | `test_research_state.py`, `test_perception_workflow_integration.py`, full suite | Reverse edges are recommendations, not automatic loops; an explicit user resume is required. |

## What did **not** change

- The four skill names, CLI verbs, approval semantics, and exit codes.
- The manuscript’s `submission_ready` decision rule.
- The requirement for a verbatim excerpt on every evidence claim.
- The dataset/evaluation criteria and locked test sets.

Known limits kept explicit: monitor probes are self-reported by the generated
code, a bound breach is fatal on the first sample by design, implementation
integrity covers the entry script (not imported modules), and expensive
reverse-loop actions are recommendations awaiting explicit resume.
- No new database, queue, or framework. Two additive SQLite columns (v17).
