#!/usr/bin/env bash
# memcapture hook — captures session facts into SQLite on PreCompact.
# Runs in background so it never blocks the session.

set -euo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')

# Find the transcript for this session
TRANSCRIPT=""
for dir in "$HOME/.claude/projects"/*/; do
    candidate="$dir$SESSION_ID.jsonl"
    if [ -f "$candidate" ]; then
        TRANSCRIPT="$candidate"
        break
    fi
done

if [ -z "$TRANSCRIPT" ]; then
    exit 0
fi

# Run capture in background — fire and forget
TOOLS_DIR="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude}/tools"
nohup uv run "$TOOLS_DIR/memcapture.py" --transcript "$TRANSCRIPT" >/dev/null 2>&1 &

exit 0
