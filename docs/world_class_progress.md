# AutoResearch program progress

| Phase | Status | Evidence | Next action |
|---|---|---|---|
| 0 — Preserve and stabilize | Complete | Baseline commits, tags, bundles, clean worktrees, full suites pass | Implement Phase 1 |
| 1 — Release platform | In progress | None yet | Launcher, setup/doctor, compatibility, packaging, CI |
| 2 — Evaluation foundation | Pending | None yet | Do not start until Phase 1 gate passes |
| 3 — Extensibility | Deferred | Out of immediate scope | Requires Phase 2 evidence |
| 4 — Remote workers | Deferred | No authenticated worker | Keep remote execution disabled |
| 5 — Observability | Deferred | Basic usage/events exist | Revisit after evaluations |
| 6 — Team/cloud readiness | Deferred | Local SQLite only | Revisit after local release gates |
| 7 — Additional clients | Deferred | Terminal and headless first | No web client |
| 8 — Reinvention/publication | Deferred | Reinvention disabled | Requires validated execution lineage |

## Phase 0 capability snapshot

The backend and terminal client expose a default conversational agent, durable
sessions/runs/events, provider-first research workflows, scoped permissions,
and approval-gated local experiment execution. Remote workers and Reinvention
remain disabled with explicit capability reasons.
