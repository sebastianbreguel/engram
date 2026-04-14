#!/usr/bin/env bash
# memcompact-hook.sh — PreCompact hook: capture work-in-progress snapshot
# Extracts last ~15% of transcript and uses claude --print to summarize work state.
# Runs in background (fire and forget). Cost: ~2-3K tokens input.

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
TOOL="${CLAUDE_PLUGIN_ROOT:-$CLAUDE_DIR}/tools/memcapture.py"

# Find current session transcript
CWD="$(pwd)"
PROJECT_KEY="${CWD//\//-}"
PROJECT_DIR="$CLAUDE_DIR/projects/$PROJECT_KEY"

if [ ! -d "$PROJECT_DIR" ]; then
    exit 0
fi

# Find most recent JSONL transcript
TRANSCRIPT=$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1)
if [ -z "$TRANSCRIPT" ]; then
    exit 0
fi

SESSION_ID=$(basename "$TRANSCRIPT" .jsonl)

# Extract last 15% of lines (user + assistant text only, skip tool_results)
TOTAL_LINES=$(wc -l < "$TRANSCRIPT")
TAIL_LINES=$(( TOTAL_LINES * 15 / 100 ))
[ "$TAIL_LINES" -lt 20 ] && TAIL_LINES=20
[ "$TAIL_LINES" -gt 500 ] && TAIL_LINES=500

CHUNK=$(tail -n "$TAIL_LINES" "$TRANSCRIPT" | jq -r '
    select(.type == "user" or .type == "assistant") |
    if .message.content | type == "string" then .message.content
    elif .message.content | type == "array" then
        [.message.content[] | select(.type == "text") | .text] | join("\n")
    else empty end
' 2>/dev/null | head -c 12000)

if [ -z "$CHUNK" ] || [ ${#CHUNK} -lt 50 ]; then
    # Still record compaction event even without snapshot
    echo "" | uv run "$TOOL" --ingest-snapshot --session-id "$SESSION_ID" --project "$PROJECT_KEY"
    exit 0
fi

PROMPT='Analyze this coding session transcript. Extract the current work state as JSON:

{
  "task": "what the user is currently working on (one sentence)",
  "files": ["files actively being edited"],
  "last_error": "last error encountered, or null",
  "summary": "2-3 sentence context: what just happened, decisions made, what is next"
}

Rules:
- Be specific and concise
- Files: only those actively being worked on, not just read
- Summary: focus on decisions and next steps, not history
- Output ONLY valid JSON, no commentary'

# Run in background — fire and forget
(
    echo "$CHUNK" | claude --print --model claude-haiku-4-5 -p "$PROMPT" 2>/dev/null | uv run "$TOOL" --ingest-snapshot --session-id "$SESSION_ID" --project "$PROJECT_KEY"
) &

exit 0
