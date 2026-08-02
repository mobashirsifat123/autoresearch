# AutoResearch user guide

Start the backend, then launch the terminal client from the sibling repository. Provider credentials belong only in the backend environment.

```bash
cd /Users/mobashirsifat/autoresearch
export AUTORESEARCH_AGENT_ROOTS=/Users/mobashirsifat/autoresearch:/Users/mobashirsifat/autoresearch-cli
autoresearch-api
cd /Users/mobashirsifat/autoresearch-cli
npx --yes bun@1.3.14 run --cwd packages/autoresearch-cli dev
```

The first screen is a conversational `Default Agent`, not a pipeline picker.
Ask about code, files, tests, architecture, or errors without a slash command.
Read-only repository tools are selected by the backend model; file edits and
short terminal commands show a scoped permission request before running.

Useful controls:

* `/tools` shows the source-of-truth capability registry.
* `/mode` shows the current persistent mode; `/use default|study-search|review|novelty|experiment` changes it.
* `/auto on` and `/auto off` control conservative capability suggestions.
* `/discover`, `/review`, `/invent`, and `/experiment` remain typed shortcuts.
* `/approve permission:ID` approves a generic tool; `/approve suggestion:ID` activates a suggested pipeline.
* `/runs`, `/artifacts`, `/usage`, `/provider`, `/cancel`, and `/help` inspect durable state.

`Ctrl+C` cancels the active agent turn or pipeline run and keeps the session
open. `Escape` exits the terminal client. If no provider configuration exists,
the backend remains usable for projects, sessions, tools, and inspection while
model-backed turns report the missing provider role explicitly.

Use `/invent --gap-id GAP_ID` after an evidence-backed review. Inspect prior-art status, critique and the persisted claim ledger before selecting a candidate. Use `/experiment plan CANDIDATE_ID` first. `validate`, `inspect`, `execute`, and `report` are explicit modes; execution must not be interpreted as available unless backend capability metadata reports a configured policy-controlled worker and a matching approval.

`/reinvent` remains disabled. A potentially-novel label means only that no duplicate was found within recorded retrieval scope; expert verification remains required.
