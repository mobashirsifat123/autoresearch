---
description: Start and keep an AutoResearch deep literature-review session. It runs evidence-verified paper review, ranking, and cited research-gap analysis in the local AutoResearch backend.
argument-hint: "<review request or top-k instruction>"
disable-model-invocation: true
---

Activate **AutoResearch Review mode** for this Claude Code conversation.

Run the local bridge from the project root with the user's request as a safely escaped `--prompt` value:

```bash
"${CLAUDE_PROJECT_DIR}/.venv/bin/python" -m autoresearch.cli.app agent --mode review --prompt "<user request>"
```

The command returns immediately after starting a durable backend turn. Report its `session_id` and `turn_id`; do not invent findings. Keep Review mode active for later research messages in this conversation: send each later review request through the same command with `--mode review`. To inspect the finished evidence, repeat the command with the same request plus `--wait`.

Review mode requires a discovered paper set. If AutoResearch reports that no paper set exists, tell the user to run Study Search first. Provider keys stay in the backend environment. Cite artifact paths and evidence IDs returned by AutoResearch. Never call novelty or experiment pipelines from this mode unless the user explicitly switches modes.

Initial request: $ARGUMENTS
