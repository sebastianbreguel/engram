#!/usr/bin/env bash
# engram uninstaller
# Removes tools, hooks, and skills. Does NOT delete memory.db (your data).

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"

echo "engram uninstaller"
echo "================================"
echo ""

# Remove files
echo "[1/3] Removing files..."
rm -f "$CLAUDE_DIR/tools/memcapture.py"
rm -f "$CLAUDE_DIR/tools/memcompile.py"
rm -f "$CLAUDE_DIR/hooks/memcapture-hook.sh"
rm -f "$CLAUDE_DIR/hooks/memcapture-inject.sh"
rm -f "$CLAUDE_DIR/hooks/memdigest-hook.sh"
rm -f "$CLAUDE_DIR/hooks/memcompact-hook.sh"
rm -rf "$CLAUDE_DIR/skills/dream"
rm -rf "$CLAUDE_DIR/skills/reflect"
echo "  Removed tools, hooks, and skills."

# Remove hooks from settings.json
echo "[2/3] Removing hook configuration..."
python3 << 'PYEOF'
import json
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.json"
if not settings_path.exists():
    exit(0)

settings = json.loads(settings_path.read_text())
hooks = settings.get("hooks", {})

# Remove memcapture hooks from PreCompact
for entry in hooks.get("PreCompact", []):
    entry["hooks"] = [h for h in entry.get("hooks", []) if "memcapture" not in h.get("command", "") and "memdigest" not in h.get("command", "") and "memcompact" not in h.get("command", "")]

# Remove SessionStart entirely if only memcapture
session_start = hooks.get("SessionStart", [])
for entry in session_start:
    entry["hooks"] = [h for h in entry.get("hooks", []) if "memcapture" not in h.get("command", "")]
hooks["SessionStart"] = [e for e in session_start if e.get("hooks")]
if not hooks["SessionStart"]:
    del hooks["SessionStart"]

settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print("  Hooks removed from settings.json")
PYEOF

echo "[3/3] Done."
echo ""
echo "Note: ~/.claude/memory.db was NOT deleted (contains your session history)."
echo "To delete it: rm ~/.claude/memory.db"
echo "To also delete compiled knowledge: rm -rf ~/.claude/compiled-knowledge/"
