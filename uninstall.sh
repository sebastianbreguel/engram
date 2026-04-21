#!/usr/bin/env bash
# claude-engram uninstaller
# Removes tools, skills, and hook registrations. Does NOT delete memory.db (your data).

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"

echo "claude-engram uninstaller"
echo "================================"
echo ""

echo "[1/3] Removing files..."
rm -f "$CLAUDE_DIR/tools/engram.py"
rm -f "$CLAUDE_DIR/tools/memcapture.py"
rm -f "$CLAUDE_DIR/tools/memcompile.py"
rm -f "$CLAUDE_DIR/tools/mempatterns.py"
rm -f "$CLAUDE_DIR/tools/memdoctor.py"
rm -f "$CLAUDE_DIR/tools/memdashboard.py"  # legacy, may not exist
# Legacy shell hooks (v0.1) — remove if present from older installs
rm -f "$CLAUDE_DIR/hooks/memcapture-hook.sh"
rm -f "$CLAUDE_DIR/hooks/memcapture-inject.sh"
rm -f "$CLAUDE_DIR/hooks/memdigest-hook.sh"
rm -f "$CLAUDE_DIR/hooks/memcompact-hook.sh"
rm -f "$CLAUDE_DIR/hooks/mempatterns-hook.sh"
rm -rf "$CLAUDE_DIR/skills/reflect"
rm -rf "$CLAUDE_DIR/skills/patterns"
echo "  Removed tools and skills."

echo "[2/3] Removing hook configuration..."
python3 << 'PYEOF'
import json
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.json"
if not settings_path.exists():
    raise SystemExit(0)

settings = json.loads(settings_path.read_text())
hooks = settings.get("hooks", {})

ENGRAM_MARKERS = ("engram.py", "memcapture", "memdigest", "memcompact", "mempatterns")


def strip(event_name):
    event = hooks.get(event_name, [])
    for entry in event:
        entry["hooks"] = [h for h in entry.get("hooks", []) if not any(m in h.get("command", "") for m in ENGRAM_MARKERS)]
    hooks[event_name] = [e for e in event if e.get("hooks")]
    if not hooks[event_name]:
        del hooks[event_name]


for ev in ("PreCompact", "SessionStart", "UserPromptSubmit"):
    strip(ev)

settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print("  Hooks removed from settings.json")
PYEOF

echo "[3/3] Done."
echo ""
echo "Note: ~/.claude/memory.db was NOT deleted (contains your session history)."
echo "To delete it: rm ~/.claude/memory.db"
echo "Note: ~/.claude/patterns/ was NOT deleted (contains your pattern wiki)."
echo "To delete it: rm -rf ~/.claude/patterns/"
