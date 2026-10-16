#!/bin/zsh
# Detached AutoResearch skill runner (permanent fix for silent run deaths).
#
# Problem it fixes: skill runs launched as children of an agent-session shell
# get SIGTERM'd when the session reaps its background shell tree. The CLI dies
# with no output, the pipeline_runs row stays "running" forever, and work
# appears lost (it is not — reviews/claims are durable and reused on relaunch).
#
# What it does:
#   1. Resolves the Alibaba Token Plan API key from the OpenCode credential
#      store (never printed, never inlined into files).
#   2. Marks stale "running" pipeline runs (>30 min, no live process) as
#      failed so status stays truthful.
#   3. Launches `autoresearch skill <args...>` under nohup + setsid: a new
#      session detached from this shell tree — harness lifecycle events cannot
#      kill it. Full stdout/stderr → timestamped log under ./.autoresearch/logs/.
#
# Usage (from the project working directory, e.g. ~/Desktop/Test):
#   ~/autoresearch/scripts/run_skill_detached.sh skill review --project <id> --top-k 5
#   ~/autoresearch/scripts/run_skill_detached.sh skill novelty --project <id> --gap-id <gap>
#   ~/autoresearch/scripts/run_skill_detached.sh resume --run <run_id>
#
# Monitor:
#   tail -f .autoresearch/logs/<log>.log                 # live output
#   sqlite3 .autoresearch/data/autoresearch.sqlite3 \
#     "SELECT status FROM pipeline_runs ORDER BY rowid DESC LIMIT 1;"

set -euo pipefail

AUTORESEARCH_BIN="${AUTORESEARCH_BIN:-autoresearch}"
OPENCODE_DB="$HOME/.local/share/opencode/opencode.db"
DB="./.autoresearch/data/autoresearch.sqlite3"
LOG_DIR="./.autoresearch/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/skill-${STAMP}.log"

# 1) Credential: Alibaba Token Plan (China) glm-5.3 + flash, from OpenCode store.
export ALIBABA_TOKEN_PLAN_API_KEY="$(sqlite3 "$OPENCODE_DB" \
  "SELECT value FROM credential WHERE integration_id='alibaba-token-plan-cn' AND active=1 LIMIT 1;" \
  | jq -r '.key')"
if [ -z "${ALIBABA_TOKEN_PLAN_API_KEY:-}" ]; then
  echo "ERROR: could not resolve ALIBABA_TOKEN_PLAN_API_KEY from OpenCode credential store" >&2
  exit 1
fi
# Reasoning-budget cap: glm-5.3 thinking is unbounded by default and blows the
# 600s total timeout in gap-analysis calls (proven failure mode). Cap it.
export AUTORESEARCH_THINKING_BUDGET="${AUTORESEARCH_THINKING_BUDGET:-2048}"

# 2) Stale-run hygiene: runs older than 30 minutes still marked "running"
#    belong to dead processes (no skill process survives that long idle).
if [ -f "$DB" ]; then
  sqlite3 "$DB" "UPDATE pipeline_runs SET status='failed', error='stale: owning process died (recovered by run_skill_detached)' WHERE status='running' AND started_at < datetime('now', '-30 minutes');" 2>/dev/null || true
fi

# 3) Detached launch: new session via os.setsid() (macOS has no setsid(1)),
#    immune to SIGHUP (nohup) and to shell-tree lifecycle kills. The Python
#    trampoline detaches the session, redirects stdout/stderr into the durable
#    log, then execs the CLI in place (same PID).
echo "launching: $AUTORESEARCH_BIN $* (log: $LOG)"
nohup /usr/bin/python3 -c '
import os, sys
os.setsid()
log = open(sys.argv[1], "a")
os.dup2(log.fileno(), 1)
os.dup2(log.fileno(), 2)
os.execvp(sys.argv[2], sys.argv[2:])
' "$LOG" "$AUTORESEARCH_BIN" "$@" &
PID=$!
echo "pid: $PID"
echo "log: $LOG"
