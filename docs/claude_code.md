# Claude Code research modes

## OpenCode provider credentials

The CLI does not inherit OpenCode's in-memory provider credentials as environment
variables. When the configured `api_key_env` is absent, AutoResearch resolves a
matching API key locally for supported official endpoints: DeepSeek, OpenAI,
Anthropic, Google Gemini, and OpenRouter. Explicit environment values win.

OpenCode v2 credentials are read from the active API-key entry in
`~/.local/share/opencode/opencode.db`, using a read-only SQLite connection.
`XDG_DATA_HOME` and an explicit `OPENCODE_DB` path are respected. Legacy
`auth.json` is used only when no v2 credential table exists. Inactive, ambiguous, OAuth,
and unrelated-provider credentials are refused. No key is exported, persisted
in AutoResearch, printed, or written back to OpenCode.

This reuses authentication for the provider already configured in AutoResearch;
it does not select the current OpenCode chat model or convert a subscription
into general API access. Custom endpoints require the configured environment
variable. Other OpenCode database schemas may require an adapter update.

`autoresearch skill doctor` checks both research-role assignment and local
credential availability. A passing check does not establish live authentication,
quota, billing entitlement, or model compatibility. After `/connect` changes the
matching API key in OpenCode, the next CLI request reads the updated value.

## Integration paths

Two integration paths exist:

1. **Agent skills (recommended):** install the skill bridges with
   `scripts/install_skills.sh` (Claude Code) or
   `autoresearch skill install --target all` (Claude Code and OpenCode).
   The skills invoke the stable `autoresearch skill …` CLI and interpret its
   JSON output — see `src/autoresearch/skills/` for the canonical definitions.
2. **Agent-mode bridge:** the durable conversational bridge below, which any
   agent host can drive over the local API.

The bridge provides three research modes:

* `/review <request>` starts Deep Literature Review, including evidence verification, ranking, and cited research-gap analysis.
* `/novelty <request>` starts evidence-backed novelty candidate generation for a validated research gap.
* `/experiment <request>` starts experiment design for an existing novelty candidate.

Each mode uses `autoresearch agent`, which creates or reuses a durable session per repository and mode, starts the backend turn without waiting for completion, and prints the session and turn identifiers. Session state is stored under AutoResearch's local state directory, not in the repository and never contains provider credentials.

With the package installed:

```bash
cd /path/to/autoresearch
autoresearch agent --mode review --prompt "<request>" --wait
```

Use one mode at a time. A mode remains part of Claude's conversation context after activation; later requests in that mode should go through the same `autoresearch agent --mode ... --prompt ...` bridge. Add `--wait` when the completed durable events and artifacts are needed in the current terminal response.

Review needs a discovered paper set. Novelty needs a validated gap. Experiment planning needs a novelty candidate. Experiment execution remains separately approval-gated in the backend and is never started by these skills.
