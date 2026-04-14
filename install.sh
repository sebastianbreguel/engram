#!/usr/bin/env bash
# engram installer
# Copies tools, hooks, and skills into ~/.claude/ and wires up settings.json

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "engram installer"
echo "============================="
echo ""

# Check prerequisites
command -v uv >/dev/null 2>&1 || { echo "Error: uv is required. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "Error: jq is required. Install: brew install jq"; exit 1; }

if [ ! -d "$CLAUDE_DIR" ]; then
    echo "Error: ~/.claude/ not found. Install Claude Code first."
    exit 1
fi

# Create directories
echo "[1/4] Creating directories..."
mkdir -p "$CLAUDE_DIR/tools"
mkdir -p "$CLAUDE_DIR/hooks"
mkdir -p "$CLAUDE_DIR/skills/memclean"
mkdir -p "$CLAUDE_DIR/skills/reflect"
mkdir -p "$CLAUDE_DIR/skills/patterns"

# Copy files
echo "[2/4] Installing files..."

cp "$SCRIPT_DIR/tools/memcapture.py" "$CLAUDE_DIR/tools/memcapture.py"
cp "$SCRIPT_DIR/tools/memcompile.py" "$CLAUDE_DIR/tools/memcompile.py"
cp "$SCRIPT_DIR/tools/mempatterns.py" "$CLAUDE_DIR/tools/mempatterns.py"
cp "$SCRIPT_DIR/tools/memdashboard.py" "$CLAUDE_DIR/tools/memdashboard.py"
echo "  -> tools/memcapture.py (SQLite session capture)"
echo "  -> tools/memcompile.py (cross-project compiler)"
echo "  -> tools/mempatterns.py (pattern detection + wiki)"
echo "  -> tools/memdashboard.py (visual dashboard)"

cp "$SCRIPT_DIR/hooks/memcapture-hook.sh" "$CLAUDE_DIR/hooks/memcapture-hook.sh"
cp "$SCRIPT_DIR/hooks/memcapture-inject.sh" "$CLAUDE_DIR/hooks/memcapture-inject.sh"
cp "$SCRIPT_DIR/hooks/memdigest-hook.sh" "$CLAUDE_DIR/hooks/memdigest-hook.sh"
cp "$SCRIPT_DIR/hooks/memcompact-hook.sh" "$CLAUDE_DIR/hooks/memcompact-hook.sh"
chmod +x "$CLAUDE_DIR/hooks/memcapture-hook.sh"
chmod +x "$CLAUDE_DIR/hooks/memcapture-inject.sh"
chmod +x "$CLAUDE_DIR/hooks/memdigest-hook.sh"
chmod +x "$CLAUDE_DIR/hooks/memcompact-hook.sh"
echo "  -> hooks/memcapture-hook.sh (PreCompact auto-capture)"
echo "  -> hooks/memcapture-inject.sh (SessionStart inject)"
echo "  -> hooks/memdigest-hook.sh (PreCompact LLM memory extraction)"
echo "  -> hooks/memcompact-hook.sh (PreCompact work-state snapshot)"

cp "$SCRIPT_DIR/hooks/mempatterns-hook.sh" "$CLAUDE_DIR/hooks/mempatterns-hook.sh"
chmod +x "$CLAUDE_DIR/hooks/mempatterns-hook.sh"
echo "  -> hooks/mempatterns-hook.sh (PreCompact pattern detection)"

cp "$SCRIPT_DIR/skills/memclean/SKILL.md" "$CLAUDE_DIR/skills/memclean/SKILL.md"
cp "$SCRIPT_DIR/skills/reflect/SKILL.md" "$CLAUDE_DIR/skills/reflect/SKILL.md"
cp "$SCRIPT_DIR/skills/patterns/SKILL.md" "$CLAUDE_DIR/skills/patterns/SKILL.md"
echo "  -> skills/memclean/SKILL.md (memory consolidation)"
echo "  -> skills/reflect/SKILL.md (pattern detection)"
echo "  -> skills/patterns/SKILL.md (pattern explorer)"

# Wire hooks into settings.json
echo "[3/4] Configuring hooks..."

SETTINGS="$CLAUDE_DIR/settings.json"

if [ ! -f "$SETTINGS" ]; then
    echo '{}' > "$SETTINGS"
fi

# Check if hooks are already configured
if grep -q "memcapture-hook.sh" "$SETTINGS" 2>/dev/null; then
    echo "  Hooks already configured, skipping."
else
    # Use Python to safely merge hooks into settings.json
    python3 << 'PYEOF'
import json
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.json"
settings = json.loads(settings_path.read_text())

hooks = settings.setdefault("hooks", {})

# Add memcapture-hook to PreCompact
precompact = hooks.setdefault("PreCompact", [])
# Find or create the catch-all matcher entry
entry = None
for e in precompact:
    if e.get("matcher", "") == "":
        entry = e
        break
if entry is None:
    entry = {"matcher": "", "hooks": []}
    precompact.append(entry)

hook_list = entry.setdefault("hooks", [])
if not any("memcapture-hook.sh" in h.get("command", "") for h in hook_list):
    hook_list.append({"type": "command", "command": "$HOME/.claude/hooks/memcapture-hook.sh"})
    print("  Added PreCompact hook: memcapture-hook.sh")

if not any("memdigest-hook.sh" in h.get("command", "") for h in hook_list):
    hook_list.append({"type": "command", "command": "$HOME/.claude/hooks/memdigest-hook.sh"})
    print("  Added PreCompact hook: memdigest-hook.sh")

if not any("memcompact-hook.sh" in h.get("command", "") for h in hook_list):
    hook_list.append({"type": "command", "command": "$HOME/.claude/hooks/memcompact-hook.sh"})
    print("  Added PreCompact hook: memcompact-hook.sh")

if not any("mempatterns-hook.sh" in h.get("command", "") for h in hook_list):
    hook_list.append({"type": "command", "command": "$HOME/.claude/hooks/mempatterns-hook.sh"})
    print("  Added PreCompact hook: mempatterns-hook.sh")

# Add memcapture-inject to SessionStart
session_start = hooks.setdefault("SessionStart", [])
entry2 = None
for e in session_start:
    if e.get("matcher", "") == "":
        entry2 = e
        break
if entry2 is None:
    entry2 = {"matcher": "", "hooks": []}
    session_start.append(entry2)

hook_list2 = entry2.setdefault("hooks", [])
if not any("memcapture-inject.sh" in h.get("command", "") for h in hook_list2):
    hook_list2.append({"type": "command", "command": "$HOME/.claude/hooks/memcapture-inject.sh"})
    print("  Added SessionStart hook: memcapture-inject.sh")

settings_path.write_text(json.dumps(settings, indent=2) + "\n")
PYEOF
fi

# Initial capture
echo "[4/4] Running initial capture..."
CAPTURED=$(uv run "$CLAUDE_DIR/tools/memcapture.py" --all 2>&1)
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
echo "  uv run ~/.claude/tools/memcapture.py --memories     # list learned memories"
echo "  uv run ~/.claude/tools/memcapture.py --forget TOPIC # forget a memory"
echo "  uv run ~/.claude/tools/memcapture.py --stats        # capture stats"
echo "  uv run ~/.claude/tools/memcapture.py --recent 5     # recent sessions"
echo "  uv run ~/.claude/tools/memcapture.py -q 'query'     # search facts"
echo "  uv run ~/.claude/tools/memcapture.py --dashboard     # visual dashboard"
