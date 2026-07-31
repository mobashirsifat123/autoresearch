# Novelty and experiment API contract

The FastAPI OpenAPI document is generated with:

```bash
.venv/bin/python -m autoresearch.api.openapi > /tmp/autoresearch-openapi.json
npx --yes openapi-typescript /tmp/autoresearch-openapi.json -o ../autoresearch-cli/packages/autoresearch-cli/src/generated/schema.d.ts
```

`POST /v1/projects/{project_id}/invent` accepts an optional validated `gap_id` and returns a durable run immediately. `POST /v1/projects/{project_id}/experiment` accepts `candidate_id` and a mode (`plan`, `validate`, `execute`, `inspect`, or `report`); execution capability remains conditional. Mutations honor `Idempotency-Key`. Events replay through `/v1/runs/{run_id}/events` using `Last-Event-ID`.

The capability endpoint is authoritative: discovery, review, novelty, and experiment design are enabled; experiment execution reports its approval/worker/isolation constraints; reinvention is disabled.
