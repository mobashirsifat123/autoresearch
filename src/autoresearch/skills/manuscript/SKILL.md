---
name: manuscript
description: Stage 4 of the AutoResearch cycle — generate an evidence-grounded, publication-readiness-gated manuscript (Markdown + LaTeX + BibTeX + tables + manifest) from the exact validated review→novelty→experiment lineage. The skill distinguishes a complete evidence chain from a submission-ready paper, synthesizes the reviewed corpus, traces every number to a claim ledger, and runs a deterministic reviewer/venue gate. It never fabricates results, citations, or authors, and it never calls a simulation an empirical measurement.
metadata:
  opencode/autoinvoke: "false"  # hidden from model auto-selection; explicit invocation only
  opencode/slash: "true"        # expose as an explicit /<name> command in the OpenCode TUI
---

# Manuscript (AutoResearch backend)

Thin bridge to the AutoResearch manuscript stage. Generation is
evidence-grounded: every factual claim traces to the stored lineage (review
gap + verified claims, frozen methodology, experiment spec, statistical
analysis, report). The stage writes editable files locally; it never submits
or publishes anything.

The core distinction is **evidence-chain complete** versus **submission
ready**. A complete lineage with weak evidence is a `draft` or a
`technical_report`, not a publishable paper.

## Publication-readiness gate (before prose)

Before writing, the stage assesses:

- **Study type and provenance** — `empirical`, `simulation`, or `unverified`
  (the gate does not infer benchmark/survey/theory labels). Explicit
  measurement-simulation markers are detected from the experiment source and
  artifacts, not inferred from prose. For an empirical venue, a simulation
  cannot be `submission_ready`; a weak/unverified RNG provenance is blocked
  as `provenance_unverified` until reviewed.
- **Identity** — the dataset checksum and implementation commit must be real
  content/commit hashes for empirical work.
- **Evidence sufficiency** — seeds, power, external baselines, ablations,
  robustness tests, and related-work coverage against the selected venue
  profile.
- **Readiness state** — `submission_ready`, `workshop_ready`,
  `technical_report`, or `not_ready`, with exact `blocking_issues`.

The gate is persisted as `readiness_report.json`. It never turns a simulation
into evidence and never invents a missing baseline or ablation.

## Venue profiles

The bundled FastTrack Academic Research Paper Template is **one profile**,
not the universal objective. Profiles live in
`src/autoresearch/application/venue_profiles.py`:

- `fasttrack` — the teaching template (six-part structured abstract).
- `generic_journal` — structured abstract, >=20 reviewed references, >=2
  external baselines, >=1 ablation, power required, real execution required.
  The review stage's default `top_k` is small; run review with a larger
  `--top-k`/`--limit` (or use a corpus with >=20 reviewed papers) before
  expecting this profile to pass.
- `neurips`, `acl`, `cvpr` — conference-style unstructured abstracts,
  anonymity, venue-specific required statements.
- `workshop` — exploratory/simulation studies are acceptable when labeled.
- `technical_report` — honest scaffold for incomplete or simulation evidence.

Select one with `[research] manuscript_venue = "<name>"` in
`.autoresearch/config.toml` or `AUTORESEARCH_MANUSCRIPT_VENUE=<name>`.
The default for new runs is `generic_journal`; set `fasttrack` explicitly
only when the bundled teaching template is the intended target.

`venue_lint.json` checks the selected profile: title/abstract style,
required sections, reference coverage, and required statements. Page limits
and PDF anonymity are recorded as "not checked" unless a compiled PDF is
inspected.

## Contribution contract

`contribution_contract.json` records the paper's argument before drafting:

- working title and five candidates (no ellipsis, no method dump);
- one-sentence takeaway;
- 2–4 contributions, each with a novelty delta, evidence ids, and the
  artifact that proves it;
- closest prior work, threats to validity, and a figure/table plan.

The title builder uses this contract and refuses truncated titles.

## Claim ledger

`claim_ledger.json` is the sentence-level evidence graph. It includes the
validated analysis, the rendered results table, the spec, the report, and the
reviewed corpus. The numeric audit checks a **sentence** against this ledger,
not every digit in the draft:

- table values (`p`, Cohen's `d`, `std`, `n`) are evidence;
- cited literature values (`40%`, `46.7`, `0.81-0.83`) are evidence;
- years, section numbers, citation keys, DOIs, URLs, hex ids, and code spans
  are exempt;
- a genuinely unsupported number is reported with its sentence for repair,
  not used to discard the whole paper.

## Full-corpus literature synthesis

`literature_synthesis.json` is built from the whole reviewed corpus, not only
the gap's supporting papers. Papers are grouped into themes by explicit
keyword rules; each theme is a narrative built only from recorded problem,
method, results, and limitation fields, followed by a comparison table and an
explicit positioning paragraph. Every paper carries a citation key. A paper
with no recorded review still contributes metadata; no claim is invented.

## Reviewer panel

`review_report.json` is a deterministic hostile checklist on the dimensions
reviewers actually use: novelty, rigor, clarity, reproducibility, and
significance. It produces concrete required fixes and a meta-review
(`accept`, `minor_revision`, `major_revision`, `reject`). It is **not** peer
review and never claims to be; it is a gate that refuses a paper whose
readiness, body hygiene, venue lint, or reviewer rubric is not ready. The
sentence-level claim-ledger audit is a separate gate on the writer output,
not a reviewer input.

`body_hygiene.json` rejects internal hex ids, claim ids, review-run
references, checksum prefixes, raw JSON in Results, placeholder text, and
truncated titles in the body. Lineage ids belong in the appendices/evidence
bundle.

## Usage

```bash
"${AUTORESEARCH_BIN:-autoresearch}" skill manuscript \
  --project "<project_id>"

# Or through the full workflow:
"${AUTORESEARCH_BIN:-autoresearch}" skill workflow \
  --project "<project_id>" --until manuscript
```

To target a real venue profile, set `[research] manuscript_venue` in the
project's `.autoresearch/config.toml` before running.

## Interpreting the JSON result

- `status`:
  - `complete` — the lineage is complete **and** the publication gate,
    quality gate, venue lint, body hygiene, and reviewer panel passed.
  - `draft` — the lineage is complete but at least one publication gate
    failed (for example: simulation evidence, missing checksum, no external
    baselines, no power, no exemplar style evidence). This is an editable
    draft, not a submission.
  - `incomplete` — the evidence chain is missing; `evidence_gaps` lists it.
- `submission_ready` — the single boolean a caller should trust for a
  submission decision. It is the persisted `submission_checklist.json`
  decision and is true only when `status == "complete"` and every gate
  passed; a `draft` is never submission-ready.
- `readiness` — the full `readiness_report.json`: `study_type`,
  `provenance`, `evidence`, `readiness`, `submission_ready`,
  `blocking_issues`, `warnings`.
- `contribution_contract` — title/contributions/prior work/threats.
- `literature_synthesis` — themes, papers, comparison table, positioning.
- `review` — reviewer panel meta-review and required fixes.
- `venue_lint` — selected profile violations.
- `body_hygiene` — internal-scaffolding errors and warnings.
- `claim_ledger` — claims, numbers, evidence ids, citations.
- `submission_checklist` — venue, status, blocking issues, required fixes,
  gate results, and next actions.
- `shared_publication_contract` / `publication_gaps` — the workflow's
  cross-stage contract (when passed in) and the exact review/novelty/
  experiment gaps that prevent `submission_ready`. A false shared contract
  forces `draft` even if the local manuscript checks pass.
- `quality` — prose audit + exemplar parity; `quality.gate.passed` is true
  only when the prose audit is `PASS` and exemplar parity passes
  (`INCONCLUSIVE`/`REVISE` are never treated as a pass).
- `template_audit` — the FastTrack audit when that profile is selected;
  otherwise the venue profile is the format gate.
- `lineage` — the exact gap/portfolio/freeze/spec/analysis/report chain.
- `artifacts` — paths to Markdown, LaTeX, BibTeX, tables, manifest, optional
  PDF, and every gate artifact above.

## Writer modes

- `deterministic` (default): field-assembled **scaffold**, zero model calls.
  It is evidence-accurate but not submission prose; it is labelled `draft`
  unless every gate passes.
- `model` (`[research] manuscript_writer = "model"` or
  `AUTORESEARCH_MANUSCRIPT_WRITER=model`): model-assisted scientific
  argument from a pre-formatted evidence pack that includes the contribution
  contract, literature synthesis, and claim ledger. The deterministic audit
  keeps numerical/citation authority. Every attempt is saved
  (`manuscript_model_attempt_1.md`, `..._2.md`); a rejected final draft is
  saved as `manuscript_model_rejected.md`. Legitimate table/literature
  numbers are not grounds for fallback. Surviving violations fall back to the
  deterministic draft and are recorded in `writing_audit.json`.

## Quality rules

- NEVER present an incomplete or `draft` manuscript as submission-ready.
  `submission_ready` is the decision signal; `status` explains the state.
- NEVER present simulated, synthetic, or placeholder measurements as
  empirical observations. A simulation study may be a workshop paper or a
  technical report only when it says so in the title/abstract/methods.
- NEVER invent a number, citation, author, dataset, baseline, ablation, or
  result. Missing evidence is reported as a gap or limitation.
- Keywords come only from Findings Memory, the gap/candidate/dataset, and
  paper metadata; method fragments and model names are filtered. A missing
  methodology field is written "not specified at freeze time" and reported
  as a limitation.
- The FastTrack template is mandatory only when that profile is selected.
  For real venues, the venue profile and reviewer panel are the format gate.
- Re-running the skill regenerates text from unchanged durable evidence; it
  never re-trains, re-runs experiments, or duplicates model calls.
- Fixture-labelled measurements cannot pass the upstream evidence gates; a
  simulation that writes realistic-looking metrics is caught by the
  provenance scan, not by a prose disclaimer.

## Exit codes

`0` success (including an honestly incomplete draft or a gated `draft`) ·
`1` internal · `2` usage · `4` project not found.
