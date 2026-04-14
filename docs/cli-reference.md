# CLI Reference

## memcapture.py

```bash
# Capture
uv run ~/.claude/tools/memcapture.py                    # capture current session
uv run ~/.claude/tools/memcapture.py --all              # capture all uncaptured sessions
uv run ~/.claude/tools/memcapture.py --transcript FILE  # capture specific transcript

# Query
uv run ~/.claude/tools/memcapture.py -q "react"        # FTS5 full-text search
uv run ~/.claude/tools/memcapture.py -q "prefiero"     # works in any language
uv run ~/.claude/tools/memcapture.py --stats            # global statistics
uv run ~/.claude/tools/memcapture.py --recent 10        # last N sessions with topics

# Dashboard
uv run ~/.claude/tools/memcapture.py --dashboard         # open visual dashboard in browser
uv run ~/.claude/tools/memdashboard.py                   # direct (same result)
uv run ~/.claude/tools/memdashboard.py -o /tmp/dash.html # custom output path
uv run ~/.claude/tools/memdashboard.py --no-open         # generate without opening

# Memory management
uv run ~/.claude/tools/memcapture.py --memories          # list all memories
uv run ~/.claude/tools/memcapture.py --memories "test_*"  # filter by topic
uv run ~/.claude/tools/memcapture.py --forget "pkg_mgr"   # delete by topic
uv run ~/.claude/tools/memcapture.py --forget --ephemeral  # clear all ephemeral

# Inject
uv run ~/.claude/tools/memcapture.py --inject                              # all projects
uv run ~/.claude/tools/memcapture.py --inject --inject-project="my-proj"   # scoped

# Compaction
uv run ~/.claude/tools/memcapture.py --compactions           # list compaction events
uv run ~/.claude/tools/memcapture.py --compactions "my-proj"  # filter by project
```

## memcompile.py

```bash
uv run ~/.claude/tools/memcompile.py              # full compile (concepts + health)
uv run ~/.claude/tools/memcompile.py --lint-only  # health checks only
uv run ~/.claude/tools/memcompile.py --dry-run    # show what would compile
```

Requires `ANTHROPIC_API_KEY` for concept compilation (uses claude-sonnet for one call).

## Skills (on-demand, zero cost until invoked)

| Command | What it does |
|---|---|
| `/memclean` | Consolidates memory files, prunes stale entries, mines transcripts for missed facts |
| `/reflect` | Analyzes last 5 sessions, detects patterns (2+ = pattern, 3+ = strong), proposes CLAUDE.md rules. **Advisory only — never writes.** |

## Token Budget

| Component | Tokens | When |
|---|---|---|
| MEMORY.md | ~150 | Every session (already exists) |
| SessionStart inject | ~350 | Every session (learned memories) |
| memcapture hook | 0 | Background, no LLM |
| memdigest hook | ~2-5K input | Background, via claude --print |
| memcompact hook | ~2-3K input | Background, via claude --print |
| Snapshot inject | ~100-150 | Only post-compaction resumes |
| `/memclean` skill | ~700 | Only when invoked |
| `/reflect` skill | ~500 | Only when invoked |
| memcompile concepts | ~300 | Only when run + 1 API call |
| **Ambient total** | **~350** | **Per session** |

## Manual Install

If you prefer to do it yourself:

```bash
# Copy files
cp tools/memcapture.py ~/.claude/tools/
cp tools/memcompile.py ~/.claude/tools/
cp hooks/memcapture-hook.sh ~/.claude/hooks/
cp hooks/memcapture-inject.sh ~/.claude/hooks/
cp -r skills/memclean ~/.claude/skills/
cp -r skills/reflect ~/.claude/skills/
chmod +x ~/.claude/hooks/memcapture-hook.sh ~/.claude/hooks/memcapture-inject.sh
```

Then add these hooks to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/memcapture-hook.sh"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/memcapture-inject.sh"
          }
        ]
      }
    ]
  }
}
```

Run initial capture:

```bash
uv run ~/.claude/tools/memcapture.py --all
```

## Experimental: Semantic Fact Extraction

Opt-in regex-based extraction of decisions and corrections from conversations. Disabled by default — the heuristics have a meaningful false positive rate (~30-50%).

Enable with:
```bash
# Per invocation
uv run ~/.claude/tools/memcapture.py --extract-facts

# Always on (add to your shell profile)
export MEMCAPTURE_EXTRACT_FACTS=1
```

When enabled, captures:

| Type | How | Example |
|---|---|---|
| **Decisions** | Regex: "decided", "let's go with", "vamos con"... | "Decided async migration strategy: dual-engine" |
| **Corrections** | Regex: "no,", "not that", "don't", "eso no"... | "no, me refiero que si total es 1k..." |
