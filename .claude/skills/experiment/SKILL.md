---
description: Start and keep an AutoResearch experiment-design session. It creates a falsifiable experiment specification from a selected novelty candidate and enforces execution approvals.
argument-hint: "<candidate ID or experiment request>"
disable-model-invocation: true
---

Activate **AutoResearch Experiment mode** for this Claude Code conversation.

Run the local bridge from the project root with the user's request as a safely escaped `--prompt` value:

```bash
"${CLAUDE_PROJECT_DIR}/.venv/bin/python" -m autoresearch.cli.app agent --mode experiment --prompt "<user request>"
```

The command starts a durable backend turn in the background. Report its `session_id` and `turn_id`; use `--wait` only when the user asks to inspect the completed plan or report. Keep Experiment mode active for later experiment-design prompts in this conversation by sending them through the same bridge with `--mode experiment`.

Experiment mode requires an existing novelty candidate. Planning is allowed, but execution is never implicit: exact backend approval is still required before any environment installation, repository modification, privileged/destructive action, remote worker, or billed execution. Provider keys stay in the backend environment. Never bypass an approval requirement.

Initial request: $ARGUMENTS
