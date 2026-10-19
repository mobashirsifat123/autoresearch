# Before-and-after architecture

Status legend: **[implemented]** completed and tested in this upgrade;
**[future]** documented recommendation, not implemented.

## Before

```mermaid
flowchart LR
    subgraph Skills
      R[review] --> N[novelty] --> E[experiment] --> M[manuscript]
    end
    subgraph Backend
      R --> DB[(SQLite + artifacts)]
      N --> DB
      E --> DB
      M --> DB
      E --> W[LocalTypedWorker]
      W -->|communicate(); exit code only| P[process]
    end
    W -->|worker failure raises| X[workflow stop]
    X -. no reverse edge .-> R
    X -. no reverse edge .-> N
    DB --> F[Findings Memory]
    F -. only analyzed outcomes .-> N
```

The forward path is evidence-gated, but the execution boundary is opaque and
the reverse path is absent. A failed process does not become a finding, and no
stage records what should happen next.

## After

```mermaid
flowchart LR
    subgraph Skills
      R[review] --> N[novelty] --> E[experiment] --> M[manuscript]
    end
    subgraph Shared durable state
      DB[(SQLite + checksummed artifacts)]
      RS[research_state.json]
      FM[Findings Memory]
      PC[publication contract]
    end
    R --> DB
    N --> DB
    E --> DB
    M --> DB
    R -->|review_audit.json: conflict candidates + missing evidence| DB
    DB --> RS
    DB --> FM
    DB --> PC

    subgraph Execution boundary
      E --> W[LocalTypedWorker]
      W --> I[implementation-integrity check]
      I --> P[process group]
      P -->|stdout lines| SP[StreamingPerception]
      SP -->|fatal| K[terminate + partial evidence]
      SP -->|suspect| TR[monitor report]
      SP -->|healthy| P
      W -->|heartbeat, pid, offset| DB
      K --> RD[recovery policy]
      E -->|report decision| RD
    end

    RD -->|fast: retry / repair / adjust| E
    RD -->|slow: redesign| E
    RD -->|slow: revise| N
    RD -->|slow: investigate| R
    RD -->|stop| S[explicit stop]
    RD --> DB
    FM --> N

    style RS fill:#d9f2d9
    style RD fill:#ffe6cc
    style SP fill:#cfe2ff
    style I fill:#cfe2ff
```

Green/blue/orange nodes are the implemented additions. The reverse edges are
**recommendations with provenance**, not automatic expensive actions: the next
stage is only entered after an explicit resume, and execution still requires a
fresh exact-spec approval.

## Implemented vs future

| Capability | Status | Artifact / entry point |
|---|---|---|
| Probe contract persisted in spec | implemented | `experiment_specs.monitor_contract_json` (schema v17) |
| Streaming perception / early kill | implemented | `experiments/perception.py`, `LocalTypedWorker` |
| Heartbeat + process identity | implemented | `worker_jobs.heartbeat_at/process_id/process_group_id/last_log_offset` |
| Execution monitor evidence | implemented | `execution_monitor` artifact |
| Script-content integrity check | implemented | `implementation_integrity` failure |
| Failure finding in Findings Memory | implemented | `operational_execution_failure` |
| Fast/slow recovery decision | implemented | `recovery_decision` artifact + `research.recovery_decision` event |
| Cross-paper conflict candidates | implemented | `review_audit.json` + review corpus + novelty context + manuscript section |
| Consolidated research state | implemented | `research_state.json`; `autoresearch skill state` |
| Automatic re-entry after refutation | future | requires explicit authorization + loop budget |
| GPU telemetry worker | future | transport-agnostic contract is ready |
| LLM-on-stream consult | future | deterministic checks cover confirmed failures |

## Data-flow invariants (unchanged)

- Every claim still requires a verbatim source excerpt.
- Every execution still requires the exact approval checksum.
- A simulation/fixture artifact still cannot pass the manuscript evidence gate.
- The manuscript still cannot run without an analyzed experiment report.
- No new database/framework was introduced; schema v17 adds two additive columns.
