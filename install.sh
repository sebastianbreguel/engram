#!/usr/bin/env bash
# claude-engram installer
# Copies tools + single hook into ~/.claude/ and wires up settings.json.

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "claude-engram installer"
echo "============================="
echo ""

command -v uv >/dev/null 2>&1 || { echo "Error: uv is required. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

if [ ! -d "$CLAUDE_DIR" ]; then
    echo "Error: ~/.claude/ not found. Install Claude Code first."
    exit 1
fi

echo "[1/4] Creating directories..."
mkdir -p "$CLAUDE_DIR/tools"
mkdir -p "$CLAUDE_DIR/skills/reflect"
mkdir -p "$CLAUDE_DIR/skills/patterns"

echo "[2/4] Installing files..."

cp "$SCRIPT_DIR/tools/engram.py" "$CLAUDE_DIR/tools/engram.py"
cp "$SCRIPT_DIR/tools/memcapture.py" "$CLAUDE_DIR/tools/memcapture.py"
cp "$SCRIPT_DIR/tools/mempatterns.py" "$CLAUDE_DIR/tools/mempatterns.py"
cp "$SCRIPT_DIR/tools/memdoctor.py" "$CLAUDE_DIR/tools/memdoctor.py"
chmod +x "$CLAUDE_DIR/tools/engram.py"
echo "  -> tools/engram.py (unified CLI + hook orchestrators)"
echo "  -> tools/memcapture.py (SQLite session capture)"
echo "  -> tools/mempatterns.py (pattern detection + wiki)"
echo "  -> tools/memdoctor.py (friction signal detector)"

cp "$SCRIPT_DIR/skills/reflect/SKILL.md" "$CLAUDE_DIR/skills/reflect/SKILL.md"
cp "$SCRIPT_DIR/skills/patterns/SKILL.md" "$CLAUDE_DIR/skills/patterns/SKILL.md"
echo "  -> skills/reflect/SKILL.md (memory consolidation + rule proposals)"
echo "  -> skills/patterns/SKILL.md (pattern explorer)"

echo "[3/4] Configuring hooks..."

SETTINGS="$CLAUDE_DIR/settings.json"

if [ ! -f "$SETTINGS" ]; then
    echo '{}' > "$SETTINGS"
fi

python3 << 'PYEOF'
import json
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.json"
settings = json.loads(settings_path.read_text())
hooks = settings.setdefault("hooks", {})

LEGACY_NAMES = ("memcapture-hook.sh", "memcapture-inject.sh", "memdigest-hook.sh", "memcompact-hook.sh", "mempatterns-hook.sh")

def strip_legacy(entry_list):
    """Drop any hook entries pointing to the old .sh scripts."""
    for entry in entry_list:
        entry["hooks"] = [h for h in entry.get("hooks", []) if not any(name in h.get("command", "") for name in LEGACY_NAMES)]
    return [e for e in entry_list if e.get("hooks")]


def ensure_hook(event_name, command):
    event = hooks.setdefault(event_name, [])
    event[:] = strip_legacy(event)
    # Check ALL matchers for existing engram.py registration
    already = any(
        "engram.py" in h.get("command", "")
        for entry in event
        for h in entry.get("hooks", [])
    )
    if already:
        print(f"  {event_name} hook already configured")
        return
    # Add to the catch-all (empty matcher) entry
    entry = next((e for e in event if e.get("matcher", "") == ""), None)
    if entry is None:
        entry = {"matcher": "", "hooks": []}
        event.append(entry)
    entry.setdefault("hooks", []).append({"type": "command", "command": command})
    print(f"  Added {event_name} hook: engram.py")


ensure_hook("PreCompact", "uv run $HOME/.claude/tools/engram.py on-precompact")
ensure_hook("SessionStart", "uv run $HOME/.claude/tools/engram.py on-session-start")
ensure_hook("UserPromptSubmit", "uv run $HOME/.claude/tools/engram.py on-user-prompt")

settings_path.write_text(json.dumps(settings, indent=2) + "\n")
PYEOF

echo "[4/4] Running initial capture..."
CAPTURED=$(uv run "$CLAUDE_DIR/tools/engram.py" capture --all 2>&1)
echo "  $CAPTURED"

echo ""
echo "Installation complete!"
echo ""
echo "What happens now:"
echo "  - Every PreCompact: session captured + LLM extracts memories (preferences, lessons, state)"
echo "  - Every SessionStart: learned memories injected (~350 tokens)"
echo "  - Memories auto-update via topic upsert (latest wins)"
echo "  - Ephemeral memories expire after 7 days"
echo ""
echo "Commands:"
echo "  uv run ~/.claude/tools/engram.py memories     # list learned memories"
echo "  uv run ~/.claude/tools/engram.py forget TOPIC # forget a memory"
echo "  uv run ~/.claude/tools/engram.py stats        # capture stats"
echo "  uv run ~/.claude/tools/engram.py patterns --report   # pattern wiki"
