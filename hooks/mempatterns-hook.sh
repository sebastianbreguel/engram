#!/usr/bin/env bash
# mempatterns hook — detect and update pattern wiki on PreCompact.
# Runs in background. Zero token cost (pure SQL + file I/O).

set -euo pipefail

LOGFILE="$HOME/.claude/patterns/.error.log"
LOCKFILE="$HOME/.claude/patterns/.lock"

# Ensure patterns dir exists
mkdir -p "$HOME/.claude/patterns"

# Acquire lock (skip if another instance is running)
if ! mkdir "$LOCKFILE" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCKFILE" 2>/dev/null' EXIT

# Run pattern detection in background, log errors
TOOL="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude}/tools/mempatterns.py"
(uv run "$TOOL" --update 2>>"$LOGFILE") &

exit 0
