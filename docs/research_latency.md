# Review, novelty, and experiment latency

Updated 2026-10-15.

## Changes

- Review extracts rigor, reproducibility, and evidence together in one call per
  paper or hierarchical section. It still rejects excerpts absent from the
  source after whitespace normalization. Cached excerpts are checked against
  the source before reuse, including reviews from interrupted runs whose paper
  checkpoints completed. PDF parsing runs in a thread so it does not block
  asynchronous model calls and cancellation.
- The three novelty critics run concurrently and receive the prior-art
  assessment. Partial overlap receives at most one revision and reassessment.
  An individual candidate failure is recorded without discarding independently
  completed candidates. Scientific gates still apply to every selected method.
- Experiment planning makes one design and one independent adversarial call.
  The critique feeds falsification, confounders, and interpretation rules.
  Validate and inspect reuse the saved spec and environment snapshot, preserving
  approval checksums. Planning again explicitly creates a new spec.
- Raw metric thresholds are no longer treated as standardized effect sizes.
  Missing variance/effect-size assumptions mean power is unestablished; a
  computed requirement is not silently clamped to 30 seeds.
- Novelty JSON now includes methods, falsification conditions, minimum
  experiments, overlap uncertainty, and objections alongside candidate IDs.

## Completion and optional limits

| Stage | Default wall-clock deadline | Optional limit |
|---|---:|---|
| Review, including ranking and gap analysis | None | `skill review --max-runtime-seconds N` |
| Novelty generation through portfolio selection | None | `skill novelty --max-runtime-seconds N` |
| Experiment design and adversarial analysis | None | `skill experiment plan --max-runtime-seconds N` |

Research continues through its stages without a default token or runtime cutoff.
If a user explicitly supplies a timeout, it cancels awaited work and closes
the durable run with `runtime_limit`.
Cancellation closes owned runs as cancelled. Completed paper checkpoints can be
reused. The provider's configured `total_timeout` now includes rate limiting,
queueing, retries, and backoff, rather than applying separately to I/O phases.
These stage limits do not set experiment execution budgets, which remain
covered by the approved specification.

The example provider config omits hidden reasoning and model output caps and
sets read/total timeouts to 0 (disabled). `[research] max_tokens = 0` overrides
an inherited `AUTORESEARCH_DEFAULT_MAX_TOKENS` ceiling. Explicit per-project
budgets and optional role output limits remain honored when requested, including
the existing bounded truncation recovery. Provider context windows, maximum
response sizes, and account quotas still apply; application settings cannot
make those provider limits infinite.

## Controlled comparison

Both versions used deterministic fixture responses, two provider slots, and
30 ms simulated latency per call. The baseline was the staged source snapshot
before the performance changes. Timing excludes discovery, real network calls,
PDF parsing, and database writes; this measures orchestration rather than
scientific quality or live provider throughput.

| Workload | Before calls | After calls | Before seconds | After seconds |
|---|---:|---:|---:|---:|
| Extract 20 paper reviews | 60 | 20 | 0.957 | 0.323 |
| Critique one candidate | 3 | 3 | 0.096 | 0.064 |
| Plan one experiment | 7 | 2 | 0.158 | 0.063 |

Fewer review/design calls reduce provider work by 67% and 71%, respectively.
Critic concurrency reduces one candidate's waiting time; it does not reduce
total critic calls and cannot exceed provider capacity. No live research
quality improvement is established by this fixture comparison.

Regression checks are in `tests/test_research_latency.py`, alongside the existing
review, workflow, approval, and provider contract tests. They exercise stalled
providers, retry backoff, timeouts, cancellation, interrupted-work reuse,
altered quotations, planning request counts, and approval preservation.
