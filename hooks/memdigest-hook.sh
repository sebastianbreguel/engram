#!/usr/bin/env bash
# memdigest hook — extracts atomic memories from session transcript via claude CLI.
# Runs in background on PreCompact. Requires claude CLI.

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

# Check claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# Extract last ~20% of user/assistant messages and run digest in background
(
    CHUNK=$(python3 -c "
import json, sys
lines = open('$TRANSCRIPT', encoding='utf-8', errors='replace').readlines()
total = len(lines)
start = max(0, int(total * 0.8))
msgs = []
for line in lines[start:]:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except:
        continue
    t = obj.get('type', '')
    if t == 'user':
        content = obj.get('message', {}).get('content', '')
        if isinstance(content, str) and len(content) > 5:
            msgs.append('USER: ' + content[:500])
    elif t == 'assistant':
        blocks = obj.get('message', {}).get('content', [])
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and b.get('type') == 'text':
                    msgs.append('ASSISTANT: ' + b['text'][:500])
                    break
# Keep within ~6K chars to stay under token limits
output = '\n'.join(msgs)
if len(output) > 6000:
    output = output[:3000] + '\n...\n' + output[-3000:]
print(output)
" 2>/dev/null)

    if [ -z "$CHUNK" ]; then
        exit 0
    fi

    PROMPT='Analyze this coding session transcript. Extract concrete, reusable facts as atomic memories.

Each memory has:
- topic: a stable snake_case identifier (e.g., "package_manager", "test_style", "current_refactor")
- durability: "durable" for preferences/lessons/practices that persist, "ephemeral" for current project state and pending work
- content: one specific sentence

Rules:
- One fact per line, format: topic | durability | content
- Be specific, not generic. "prefers uv over pip" not "has package manager preferences"
- Skip routine actions (file reads, git commits, navigation)
- If a fact contradicts a likely prior preference, still extract it — upsert will handle it
- Max 10 facts per session
- Output ONLY the facts, no commentary'

    DIGEST=$(echo "$CHUNK" | claude --print -p "$PROMPT" 2>/dev/null || true)

    if [ -n "$DIGEST" ]; then
        echo "$DIGEST" | uv run "$HOME/.claude/tools/memcapture.py" --ingest-digest --session-id="$SESSION_ID" 2>/dev/null || true
    fi
) &

exit 0
