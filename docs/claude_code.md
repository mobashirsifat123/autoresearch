# Claude Code research modes

When Claude Code starts from this repository, it discovers three project skills:

* `/review <request>` starts Deep Literature Review, including evidence verification, ranking, and cited research-gap analysis.
* `/novelty <request>` starts evidence-backed novelty candidate generation for a validated research gap.
* `/experiment <request>` starts experiment design for an existing novelty candidate.

Each skill uses `python -m autoresearch.cli.app agent`, which creates or reuses a durable session per repository and mode, starts the backend turn without waiting for completion, and prints the session and turn identifiers. Session state is stored under AutoResearch's local state directory, not in the repository and never contains provider credentials.

Start Claude Code from the backend project after installing its virtual environment:

```bash
cd /Users/mobashirsifat/autoresearch
source .venv/bin/activate
claude
```

Use one mode at a time. A mode remains part of Claude's conversation context after activation; later requests in that mode should go through the same `autoresearch agent --mode ... --prompt ...` bridge. Add `--wait` when the completed durable events and artifacts are needed in the current terminal response.

Review needs a discovered paper set. Novelty needs a validated gap. Experiment planning needs a novelty candidate. Experiment execution remains separately approval-gated in the backend and is never started by these skills.
