---
name: experiment
description: Run the AutoResearch experiment lifecycle — plan a falsifiable experiment from a novelty candidate, review the exact-spec approval, then (only with explicit user consent) execute it through the policy-controlled worker, and report results. Use when the user wants to design or run a controlled experiment for a proposed method.
---

# Experiment (AutoResearch backend)

Thin bridge to the AutoResearch scientific backend. Planning never executes
code. Execution is gated behind an exact-specification approval that must be
explicitly confirmed by the user.

## When to use

- The user wants a falsifiable, publication-oriented experiment designed from a
  novelty candidate (plan / validate / inspect).
- The user explicitly asks to run the approved experiment, after seeing the
  exact action plan.

## When NOT to use

- There is no novelty candidate yet: run `novelty` first.
- The user asks to "just run" the code without reviewing the spec — never
  skip the approval step.

## Lifecycle

```bash
# 1. Plan (no code runs). Prints spec + exact action-plan preview + checksum.
... skill experiment plan --candidate-id "<candidate_id>"

# 2. Validate the execution environment (no code runs).
... skill experiment validate --candidate-id "<candidate_id>"

# 3. Show the approval preview. Record it ONLY after the user confirms the
#    exact actions, checksum, worker, and budget:
... skill experiment approve --candidate-id "<candidate_id>"            # preview only
... skill experiment approve --candidate-id "<candidate_id>" --accept   # records approval

# 4. Execute the approved exact-spec plan through the policy-controlled worker.
... skill experiment execute --candidate-id "<candidate_id>"

# 5. Inspect / report / cancel as needed.
... skill experiment inspect --candidate-id "<candidate_id>"
... skill experiment report --candidate-id "<candidate_id>"
... skill experiment cancel --run-id "<run_id>"
```

All commands above use:
`"${AUTORESEARCH_ROOT:-/Users/mobashirsifat/autoresearch}/.venv/bin/python" -m autoresearch.cli.app`

Exit codes: `0` success · `1` internal · `2` usage · `3` approval required ·
`4` not found · `5` insufficient evidence · `6` worker unavailable.

## Interpreting the JSON result

- `plan` returns `spec` (hypothesis, falsification_condition, metrics, seeds,
  statistical_plan) and `approval` (worker, actions, checksum, budget).
  Show this to the user verbatim before any approval.
- `approve` without `--accept` writes nothing and returns the preview —
  that is the step to show the user.
- `execute` returns the run status and worker `jobs`; `report` returns the
  decision and decision evidence.

## Quality rules

- Never run `execute` without a recorded `--accept` approval and without the
  user's explicit confirmation of the exact actions.
- Never pass the checksum from one candidate's spec to another candidate's
  execution — the backend rejects mismatches.
- If the worker is unavailable (`6`), report that execution is disabled rather
  than faking results.
