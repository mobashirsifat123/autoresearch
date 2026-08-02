---
description: Start and keep an AutoResearch novelty-generation session. It produces evidence-backed candidate methods, prior-art comparisons, and scientific-critic decisions.
argument-hint: "<gap ID or novelty request>"
disable-model-invocation: true
---

Activate **AutoResearch Novelty mode** for this Claude Code conversation.

Run the local bridge from the project root with the user's request as a safely escaped `--prompt` value:

```bash
"${CLAUDE_PROJECT_DIR}/.venv/bin/python" -m autoresearch.cli.app agent --mode novelty --prompt "<user request>"
```

The command starts a durable backend turn in the background. Report its `session_id` and `turn_id`; do not claim results before backend artifacts exist. Keep Novelty mode active for later novelty requests: send each later request through the same command with `--mode novelty`. Add `--wait` only when the user asks to inspect the completed portfolio.

Novelty mode requires a validated research gap from a completed evidence-backed review. Never call something proven novel: use the backend’s cautious statuses, such as potentially novel, overlapping, incremental, or expert review required. Provider keys stay in the backend environment. Do not start experiments unless the user explicitly switches to `/experiment`.

Initial request: $ARGUMENTS
