# P0 Implementation Report

**Date:** 2026-10-18
**Repository:** local AutoResearch checkout
**Scope:** P0 of `SKILLS_ENHANCEMENT_PLAN.md`; P1 interfaces deferred (see below).

## Verdict

P0 is implemented: the pipeline now has a shared publication contract, review corpus and citation contexts, publication-design gates at freeze and plan time, real provenance computation for dataset checksum and baseline commit, execution-artifact binding, workflow early stops on publication gaps, and per-stage metrics. The manuscript gate consumes the shared freeze gaps and remains fail-closed.

P1 (section-level model drafting, model reviewer ensemble, full venue package, human-calibration harness) is **not** implemented in this run. The existing deterministic reviewer panel and manuscript gate remain the fallback.

No top-journal acceptance is claimed. The gates and tests support reviewer-ready gating, not acceptance.

## P0 status

| Item | Status | Evidence |
|---|---|---|
| P0.1 Shared publication contract | Done | `application/publication_contract.py`; contract/gap tests |
| P0.2 Review publication mode | Done | `application/review_publication.py`; 20-paper corpus + gaps tests |
| P0.3 Novelty publication design | Done | `novelty/methodology.py` schema + `publication_design_gaps`; tests |
| P0.4 Experiment design + real provenance | Done | `experiments/publication_design.py`; planner computes checksum/commit; plan CLI returns gaps; tests |
| P0.5 Workflow early gates + metrics | Done | `workflow.py` publication gates, `publication_gaps.json`, `stage_metrics.json`; CLI `--publication`/`--venue`; tests |
| P0.6 Docs + install | Done | README + five SKILL.md files; `skill update --target all`; `skill doctor` healthy |

## Files added

- `src/autoresearch/application/publication_contract.py`
- `src/autoresearch/application/review_publication.py`
- `src/autoresearch/experiments/publication_design.py`
- `tests/test_publication_contract.py`

## Files changed

- `src/autoresearch/application/services.py`
- `src/autoresearch/application/workflow.py`
- `src/autoresearch/application/venue_profiles.py`
- `src/autoresearch/novelty/methodology.py`
- `src/autoresearch/experiments/planner.py`
- `src/autoresearch/cli/skills.py`
- `README.md`
- `src/autoresearch/skills/review/SKILL.md`
- `src/autoresearch/skills/novelty/SKILL.md`
- `src/autoresearch/skills/experiment/SKILL.md`
- `src/autoresearch/skills/workflow/SKILL.md`
- `src/autoresearch/skills/manuscript/SKILL.md`
- `tests/test_smoke_e2e.py`

## Exact verification

```bash
cd <repo>
python -m pytest tests/ -q
python -m compileall -q src/autoresearch
git diff --check
autoresearch skill doctor
```

Final result: **363 passed, 3 warnings** (warnings are pre-existing asyncio deprecations).
`compileall`, `git diff --check`, and `skill doctor` are clean/healthy.

## AutoFID result

The real AutoFID project remains correctly gated:

```json
{
  "status": "draft",
  "submission_ready": false,
  "readiness": "technical_report",
  "decision": "partially_supported",
  "review": "reject",
  "blocking_issues": [
    "simulated_evidence",
    "dataset_checksum_missing",
    "implementation_commit_missing",
    "real_execution_not_verified",
    "execution_artifact_missing",
    "power_not_established",
    "external_baselines_missing (1 < 2)",
    "ablations_missing (0 < 1)",
    "related_work_coverage_below_minimum (6 < 20)"
  ],
  "body_hygiene_errors": []
}
```

The review corpus is six papers; the generic journal profile requires twenty. The study is a simulation with one baseline, no ablations, no power analysis, and no execution artifact. The manuscript skill refuses to call it submission-ready.

## Strong-fixture evidence

Unit-level strong fixtures pass:

- `test_contract_accepts_a_complete_empirical_slice`
- `test_readiness_accepts_a_real_powered_study` (real source/data I/O, computed-format checksum/commit, two labeled baselines, one ablation, robustness, power)
- `test_delivery_state_enforces_complete_iff_submission_ready` (`complete`/`true` is representable)

A full end-to-end `submission_ready` manuscript fixture is **not** included: the quality gate requires exemplar PDFs and the venue lint requires a real 20-paper corpus, which the offline smoke fixture does not supply. This is deferred to P1 with the human-calibration harness.

## P1 deferred

1. `ManuscriptWriter` section-level interface, parallel section drafting, failing-section repair.
2. Five-persona model reviewer ensemble + meta-review + 2–3 revision rounds.
3. Full venue package: venue LaTeX class, page limit, anonymity lint, cover letter, supplementary, reproducibility checklist, response-to-reviewers.
4. Human-calibrated evaluation harness (blind expert review; reviewer-model calibration).

## Risks and honest limitations

- The review corpus is only as good as the review extractor; citation contexts reuse verified excerpts and do not add semantic entailment.
- `dataset_checksum` is computed only when `dataset_identity` is a real path; prose descriptions produce an explicit gap. This is intentional.
- `baseline_commit` is computed only when the target is a git repository; otherwise the drafted value is retained and the gate rejects it if it is not a hash.
- The strong-design unit fixture proves the gates can pass, not that a real top-journal paper exists.
- Acceptance remains a function of contribution, execution, venue fit, and reviewers. No software guarantee is possible.

## Next step

P1.1 and P1.2: section-level manuscript drafting and the model reviewer ensemble, behind deterministic tests and the existing manuscript gate.
