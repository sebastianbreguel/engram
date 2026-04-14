#!/usr/bin/env bash
# Record a 30s asciinema demo of claude-engram for the README.
#
# Prerequisites:
#   brew install asciinema agg   # agg converts .cast → .gif
#
# Usage:
#   bash docs/record-demo.sh
#
# This records you running the key commands. After recording,
# convert to GIF with:
#   agg docs/demo.cast docs/demo.gif --cols 90 --rows 24 --speed 1.5
#
# Or upload to asciinema.org:
#   asciinema upload docs/demo.cast

set -euo pipefail

CAST="docs/demo.cast"

echo "Recording claude-engram demo → $CAST"
echo "Run these commands during recording:"
echo ""
echo "  1. uv run ~/.claude/tools/engram.py stats"
echo "  2. uv run ~/.claude/tools/engram.py memories"
echo "  3. uv run ~/.claude/tools/engram.py inject --project 'claude-engram'"
echo ""
echo "Press Ctrl+D or type 'exit' to stop recording."
echo ""

asciinema rec "$CAST" --cols 90 --rows 24 --title "claude-engram — session memory for Claude Code"

echo ""
echo "Saved: $CAST"
echo "Convert to GIF:  agg $CAST docs/demo.gif --cols 90 --rows 24 --speed 1.5"
echo "Upload:          asciinema upload $CAST"
