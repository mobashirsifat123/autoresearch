---
name: status
description: Read-only progress snapshot for AutoResearch projects — which pipeline runs are active, what stage each project reached, and what the next action is. Use whenever the user asks whether research is still running, how far along it is, or what state a project is in.
metadata:
  opencode/autoinvoke: "false"  # hidden from model auto-selection; explicit invocation only
  opencode/slash: "true"        # expose as an explicit /<name> command in the OpenCode TUI
---

# Status (AutoResearch backend)

Thin read-only bridge to the AutoResearch backend. It never starts, resumes,
approves, or cancels anything — it only reports durable state. Polling is
side-effect-free and safe while another session or terminal is mid-run.

## When to use

- The user asks "is it still running?", "how far along is it?", "what stage
  are we at?", or "did it finish?"
- Checking state between long steps of the `workflow` or `experiment` skills,
  or from a second session while another one is working.
- Recovering context after a disconnect or crash without re-running anything.

## When NOT to use

- The user wants to advance the pipeline — use the `workflow` skill (resume
  with `--project`) or a stage skill instead.
- The user wants to cancel — `experiment cancel` is the write path; this
  skill only reports.

## Usage

```bash
# Snapshot of the 20 most recent projects:
"${AUTORESEARCH_BIN:-autoresearch}" skill status

# One project:
"${AUTORESEARCH_BIN:-autoresearch}" skill status --project "<project_id>"
```

## Interpreting the JSON result

`projects[]` (most recent first), each with:

- `paper_count` — discovered corpus size.
- `runs[]` — durable pipeline runs, newest first, each `{id, kind, status}`:
  - `kind` = the stage the project is in (`discovery`, `review`, `novelty`,
    `experiment`, …).
  - `status` = liveness: `running` (still working); `succeeded`, `failed`,
    `stopped`, `cancelled` (terminal); `awaiting_approval` (paused for an
    explicit user approval).
- `latest_gap` / `latest_novelty_portfolio` / `latest_experiment_spec` — the
  newest durable artifact of each stage, i.e. how far the project has ever
  gotten.

## What to tell the user

- A compact table: project, current stage (newest run `kind`), run `status`,
  papers.
- "Still working" is true only while some run has `status: running`.
- A `failed` discovery/review run is resumable: suggest
  `autoresearch resume --run <run_id>`.
- `awaiting_approval` means an exact-spec package is waiting on the user's
  explicit decision — point to the `experiment` skill approval step. Never
  report it as "done".

## Exit codes

`0` success · `1` internal · `2` usage · `4` not found.

## Quality rules

- Read-only: never invoke write, approve, or execute commands from this skill.
- Report run statuses verbatim; never paraphrase `awaiting_approval` or
  `failed` as progress or success.
- If the backend is unavailable, say so and suggest `autoresearch doctor`.
