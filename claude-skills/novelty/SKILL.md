---
name: novelty
description: Run the AutoResearch novelty pipeline — generate, prior-art-check, and adversarially critique novelty candidates against a validated research gap. Use when the user wants to invent or assess candidate methods and their novelty relative to existing work.
---

# Novelty (AutoResearch backend)

Thin bridge to the AutoResearch scientific backend. Never run a conversational
agent for this work — call the stable skill CLI and interpret its JSON.

## When to use

- The user wants candidate methods/mechanisms proposed for a validated research
  gap, with prior-art verification and adversarial critique.
- A deep-research run produced a `gap` and the user asks "what could be novel?"
  or "invent approaches to this gap".

## When NOT to use

- There is no validated gap yet: run `deep-research` first (a gap comes from
  the review step).
- The user wants creative brainstorming without evidence checks — the backend
  is deliberately skeptical and will report overlap, not flatter.

## Usage

```bash
"${AUTORESEARCH_ROOT:-/Users/mobashirsifat/autoresearch}/.venv/bin/python" \
  -m autoresearch.cli.app skill novelty \
  --project "<project_id>" [--gap-id "<gap_id>"]
```

`--gap-id` is optional; the default is the project's highest-confidence gap.

Exit codes: `0` success · `1` internal · `2` usage · `3` approval · `4` not found · `5` insufficient evidence.

## Interpreting the JSON result

- `portfolio_id` — the durable novelty portfolio.
- `candidate_ids` plus `low_risk_candidate_id`, `balanced_candidate_id`,
  `high_risk_candidate_id` — risk-tiered candidates.
- The backend has already run prior-art search and an ensemble critic; it does
  NOT claim novelty. Report candidate status honestly to the user.
- Pick the `low_risk_candidate_id` when the user next wants to plan an experiment.

## Quality rules

- Never present a candidate as "novel" unless the backend's prior-art
  assessment supports it; surface `known_method` / `partial_overlap` outcomes.
- If the user asks to plan an experiment, call
  `autoresearch skill experiment plan --candidate-id <candidate_id>`.
- If `skill doctor` shows the scientific-critic role unconfigured, the critic
  scores are absent — say so instead of summarizing empty critiques.
