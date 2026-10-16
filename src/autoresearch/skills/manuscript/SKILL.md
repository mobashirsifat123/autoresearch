---
name: manuscript
description: Stage 4 of the AutoResearch cycle — generate a complete, evidence-grounded scientific manuscript (Markdown + LaTeX + BibTeX + tables + manifest) from the exact validated review→novelty→experiment lineage of a project. Deterministic generation; never fabricates results, citations, or authors. Reports precise evidence gaps when the chain is incomplete.
metadata:
  opencode/autoinvoke: "false"  # hidden from model auto-selection; explicit invocation only
  opencode/slash: "true"        # expose as an explicit /<name> command in the OpenCode TUI
---

# Manuscript (AutoResearch backend)

Thin bridge to the AutoResearch manuscript stage. Generation is
deterministic: every sentence is assembled from stored, checksummed evidence(review gap + verified claims, frozen methodology, experiment spec,
statistical analysis, report). No model call invents prose, numbers,
citations, authors, or results. This skill never submits or publishes
anything — it only writes editable files locally.

## When to use

- The experiment stage produced an analyzed report and the user wants the
  manuscript ("write the paper", "draft the manuscript", "generate the
  paper from the results").
- The user asks what a complete write-up would need (an incomplete draft +
  evidence-gap report is a valid, honest outcome).
- The full cycle is driven by `autoresearch skill workflow --until
  manuscript` (this stage runs automatically as stage 7).

## When NOT to use

- No project exists yet — run `review` first.
- The user wants to publish or submit somewhere — out of scope by design.

## Prerequisites

- A project with a completed review, frozen methodology, executed
  experiment, validated statistical analysis, and deterministic report.
  Missing pieces produce an INCOMPLETE draft plus a precise gap list, never
  fabricated content.

## Usage

```bash
"${AUTORESEARCH_BIN:-autoresearch}" skill manuscript \
  --project "<project_id>"

# Or through the full workflow:
"${AUTORESEARCH_BIN:-autoresearch}" skill workflow \
  --project "<project_id>" --until manuscript
```

## Interpreting the JSON result

- `status` — `complete` (full evidence chain present) or `incomplete`.
- `evidence_gaps` — exact missing prerequisites (e.g. `"experiment: no
  validated statistical analysis (measurement ingestion/analysis pending)"`).
- `decision` — the experiment report's decision, quoted verbatim
  (`supported`, `refuted`, …). Negative and null results are valid.
- `lineage` — the exact gap/portfolio/freeze/spec/analysis/report chain this
  manuscript was built from. Incompatible lineages are never mixed.
- `artifacts` — paths to `markdown`, `latex`, `bibtex`,
  `evidence_bundle.json` (all lineage ids + checksums), `manifest.json`
  (file checksums), and `pdf` (only when a compiler was available AND
  compilation verified; otherwise `null`).

## What to tell the user

- Report `status` first; if `incomplete`, list `evidence_gaps` verbatim and
  state that no results were fabricated.
- Report the decision verbatim with its evidence — including negative/null
  outcomes, which are valid scientific results.
- Point to the editable Markdown/LaTeX/BibTeX files for author revision.
- If `pdf` is null, say no verified LaTeX compiler was available.

## Exit codes

`0` success (including an honestly incomplete draft) · `1` internal ·
`2` usage · `4` project not found.

## Writer modes

- `deterministic` (default): field-assembled manuscript, zero model calls.
- `model` (`[research] manuscript_writer = "model"` or
  `AUTORESEARCH_MANUSCRIPT_WRITER=model`): model-assisted scientific
  argument (contribution outline → section drafting from a pre-formatted
  evidence pack) gated by a **deterministic audit**: every number must trace
  to the allowed evidence set (sign- and rounding-tolerant), every citation
  key must exist, overclaim phrases are rejected, and the decision word must
  appear verbatim. One bounded revision; surviving violations fall back to
  the deterministic draft. The audit is persisted (`writing_audit.json`) and
  the deterministic companion (`manuscript_deterministic.md`) is kept for
  diffing.

## Quality rules

- NEVER present an incomplete manuscript as complete; the gap report is
  part of the deliverable.
- NEVER paraphrase the decision or invent numerical values; numbers in the
  manuscript come from the deterministic statistical analysis only.
- Every citation key is verified against the generated BibTeX, which is
  built exclusively from stored paper metadata — invented references are
  structurally impossible.
- Re-running the skill regenerates text from unchanged durable evidence; it
  never re-trains, re-runs experiments, or duplicates model calls.
- Fixture-labeled measurements cannot pass the upstream evidence gates, so
  they cannot reach this stage as results.
