# Novelty and experiment user guide

Start the backend, then launch the terminal client from the sibling repository. Provider credentials belong only in the backend environment.

```bash
autoresearch-api
cd /Users/mobashirsifat/autoresearch-cli
npx --yes bun@1.3.14 run --cwd packages/autoresearch-cli start
```

Use `/invent --gap-id GAP_ID` after an evidence-backed review. Inspect prior-art status, critique and the persisted claim ledger before selecting a candidate. Use `/experiment plan CANDIDATE_ID` first. `validate`, `inspect`, `execute`, and `report` are explicit modes; execution must not be interpreted as available unless backend capability metadata reports a configured policy-controlled worker and a matching approval.

`/reinvent` remains disabled. A potentially-novel label means only that no duplicate was found within recorded retrieval scope; expert verification remains required.
