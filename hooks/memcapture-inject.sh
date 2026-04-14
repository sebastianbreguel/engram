#!/usr/bin/env bash
# SessionStart hook — injects recent session context from SQLite (~200 tokens).
# Reads from memory.db and outputs a small context block.

set -euo pipefail

INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Derive project key from cwd (Claude Code convention)
PROJECT_KEY=""
if [ -n "$CWD" ]; then
    PROJECT_KEY=$(echo "$CWD" | sed 's|/|-|g')
fi

TOOL="${CLAUDE_PLUGIN_ROOT:-$HOME/.claude}/tools/memcapture.py"

# Generate inject context
if [ -n "$PROJECT_KEY" ]; then
    CONTEXT=$(uv run "$TOOL" --inject --inject-project="$PROJECT_KEY" 2>/dev/null || true)
else
    CONTEXT=$(uv run "$TOOL" --inject 2>/dev/null || true)
fi

if [ -n "$CONTEXT" ]; then
    echo "$CONTEXT"
fi

exit 0
