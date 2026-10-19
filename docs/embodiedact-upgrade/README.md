# EmbodiedAct-informed AutoResearch upgrade

This folder contains the audit, source analysis, implementation plan, and
validation record for the upgrade. The code is in the working tree on top of
commit `a53241c`.

| Document | Contents |
|---|---|
| [01_CURRENT_STATE_AUDIT.md](01_CURRENT_STATE_AUDIT.md) | What the four skills actually do, state ownership, execution trace, confirmed weaknesses, hypotheses. |
| [02_EMBODIEDACT_SOURCE_ANALYSIS.md](02_EMBODIEDACT_SOURCE_ANALYSIS.md) | Verified EmbodiedAct mechanisms, paper-level claims, transfer map, non-transferable ideas. |
| [03_IMPLEMENTATION_PLAN.md](03_IMPLEMENTATION_PLAN.md) | Prioritized plan with problem, location, mechanism, benefit, risk, tests, success criteria. |
| [04_BEFORE_AFTER_ARCHITECTURE.md](04_BEFORE_AFTER_ARCHITECTURE.md) | Before/after diagrams and implemented-vs-future table. |
| [05_SKILL_IMPROVEMENT_MATRIX.md](05_SKILL_IMPROVEMENT_MATRIX.md) | Per-skill original behavior, weakness, change, tests, remaining gap. |
| [06_IMPLEMENTATION_CHANGELOG.md](06_IMPLEMENTATION_CHANGELOG.md) | Changed files, design decisions, compatibility, behavior changes. |
| [07_TEST_AND_EVALUATION_REPORT.md](07_TEST_AND_EVALUATION_REPORT.md) | Executed commands, results, microbenchmark, coverage, untested areas. |
| [08_NEXT_STEP_ROADMAP.md](08_NEXT_STEP_ROADMAP.md) | Ranked remaining work with evidence requirements and risk. |
| [09_HOSTILE_REVIEW_RESPONSE.md](09_HOSTILE_REVIEW_RESPONSE.md) | Independent review findings, blocker dispositions, residual risks. |

Key rule preserved: the system may recommend a scientific pivot, but it never
silently performs one, and it never turns an operational success into
scientific evidence.
