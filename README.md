# claude-memory-stack

Give Claude Code persistent memory across sessions — near-zero ambient token cost (~200 tokens/session).

Every session is automatically captured into a local SQLite database. Files touched, tool usage, errors, git branch, and session topics are extracted from transcripts — no LLM calls, no external services, no API costs.

```
$ uv run ~/.claude/tools/memcapture.py --stats
Sessions captured: 2347
Unique files touched: 1486
Facts by type: {'correction': 75, 'decision': 64, 'error': 361}
```

## Why this exists

There are several Claude Code memory repos out there. Most add significant ambient token cost per session, require external services or API keys, or install MCP servers with many tool descriptions that load into every session.

This stack takes the best ideas from the ecosystem and combines them into 4 lightweight files with **~200 tokens ambient overhead**:

| Source repo | What we took | What we skipped |
|---|---|---|
| [claude-mem](https://github.com/thedotmack/claude-mem) | Auto-capture concept | LLM worker, Agent SDK, web viewer |
| [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) | compile.py + lint.py architecture | SessionEnd hooks, ambient injection |
| [claude-diary](https://github.com/rlancemartin/claude-diary) | /reflect and /diary patterns | — |
| [OpenMemory](https://github.com/CaviraOSS/OpenMemory) | Temporal decay concept | Docker, MCP server, dashboard |
| [claude-subconscious](https://github.com/letta-ai/claude-subconscious) | Periodic reflection idea | Letta Cloud dependency, background agent |
| [cortex](https://github.com/gambletan/cortex) | Token-budget concept | 27 MCP tools, Rust binary |

## How it works

```
YOUR NORMAL DAY
===============

  Open Claude Code ──► Work normally ──► Context fills up
       │                                        │
       │ Reads:                      PreCompact fires (automatic):
       │ • MEMORY.md (~150 tokens)              │
       │ • CLAUDE.md (your rules)    ┌──────────┴──────────┐
       │                             │                      │
       │                     precompact-save.sh    memcapture-hook.sh
       │                     (saves snapshot)      (background, fire-and-forget)
       │                                                    │
       │                                           memcapture.py parses JSONL:
       │                                           • errors (real, filtered)
       │                                           • files touched
       │                                           • tool usage counts
       │                                           • session topic + branch
       │                                                    │
       │                                                    ▼
  Next session starts ◄─── SessionStart hook ◄─── ~/.claude/memory.db
       │                   injects ~200 tokens            (SQLite)
       │                   of recent context
       ▼
  Claude knows what you did last session
```

### What gets captured automatically

Every time your context compacts, `memcapture.py` parses the session transcript (JSONL) and extracts:

| Type | How | Example |
|---|---|---|
| **Errors** | Real runtime errors only (tracebacks, EACCES...) | "ModuleNotFoundError: No module named 'foo'" |
| **Files touched** | From Read/Edit/Write tool inputs | `src/main.py` (edit: 5, read: 12) |
| **Tool usage** | Count per tool per session | Bash: 6060, Read: 3953, Edit: 3095... |
| **Session topic** | First real user message | "Necesito hacer una query en sql..." |
| **Git branch** | From git commands in transcripts | `feat/business-context-prompts` |

Facts are **deduplicated** (MD5 hash) — the same error won't appear twice.

### What gets injected at SessionStart

~200 tokens of context, scoped to the current project:

```xml
<session-memory>
Recent sessions:
- 2026-04-13 (feat/auth) Add OAuth2 flow to login endpoint
- 2026-04-12 Fix broken migration on users table
- 2026-04-11 Refactor pipeline stages into separate modules
Recent commits:
- a1b2c3d Add OAuth2 flow to login endpoint
- d4e5f6g Fix broken migration on users table
- h7i8j9k Refactor pipeline stages
Recent errors:
- ModuleNotFoundError: No module named 'foo'
</session-memory>
```

### Experimental: Semantic Fact Extraction

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

## Advanced / Power Users

These tools provide deeper memory management but are not required for day-to-day use.

### Manual skills (on-demand, zero cost until invoked)

| Command | What it does |
|---|---|
| `/dream` | Consolidates memory files, prunes stale entries, mines transcripts for missed facts |
| `/reflect` | Analyzes last 5 sessions, detects patterns (2+ = pattern, 3+ = strong), proposes CLAUDE.md rules. **Advisory only — never writes.** |

### Cross-project compilation

```bash
uv run ~/.claude/tools/memcompile.py              # compile concepts + health
uv run ~/.claude/tools/memcompile.py --lint-only   # health check only
uv run ~/.claude/tools/memcompile.py --dry-run     # show what would compile
```

Walks all `~/.claude/projects/*/memory/*.md` directories and generates:
- `~/.claude/compiled-knowledge/health.md` — stale, duplicate, oversized, empty memories
- `~/.claude/compiled-knowledge/concepts.md` — recurring themes across projects (one LLM call)

## Token budget

| Component | Tokens | When |
|---|---|---|
| MEMORY.md | ~150 | Every session (already exists) |
| SessionStart inject | ~200 | Every session (sessions + commits + errors) |
| memcapture hook | 0 | Background, no LLM |
| `/dream` skill | ~700 | Only when invoked |
| `/reflect` skill | ~500 | Only when invoked |
| memcompile concepts | ~300 | Only when run + 1 API call |
| **Ambient total** | **~200** | **Per session** |

Most other memory solutions add significantly more ambient cost due to MCP tool descriptions, SessionStart knowledge injection, or background LLM workers.

## Install

### Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and working
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [jq](https://jqlang.github.io/jq/) (`brew install jq`)

### One-liner

```bash
git clone https://github.com/sebastianbreguel/claude-memory-stack.git
cd claude-memory-stack
./install.sh
```

The installer:
1. Copies tools, hooks, and skills to `~/.claude/`
2. Wires PreCompact and SessionStart hooks into `settings.json`
3. Runs initial capture of all existing sessions

### Manual install

If you prefer to do it yourself:

```bash
# Copy files
cp tools/memcapture.py ~/.claude/tools/
cp tools/memcompile.py ~/.claude/tools/
cp hooks/memcapture-hook.sh ~/.claude/hooks/
cp hooks/memcapture-inject.sh ~/.claude/hooks/
cp -r skills/dream ~/.claude/skills/
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

### Uninstall

```bash
cd claude-memory-stack
./uninstall.sh
```

Removes all files and hook configurations. Does **not** delete `~/.claude/memory.db` (your data).

## CLI Reference

### memcapture.py

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

# Inject
uv run ~/.claude/tools/memcapture.py --inject                              # all projects
uv run ~/.claude/tools/memcapture.py --inject --inject-project="my-proj"   # scoped
```

### memcompile.py

```bash
uv run ~/.claude/tools/memcompile.py              # full compile (concepts + health)
uv run ~/.claude/tools/memcompile.py --lint-only  # health checks only
uv run ~/.claude/tools/memcompile.py --dry-run    # show what would compile
```

Requires `ANTHROPIC_API_KEY` for concept compilation (uses claude-sonnet for one call).

## Architecture

```
~/.claude/
├── memory.db                    # SQLite — auto-captured session data
│   ├── sessions                 # id, project, branch, topic, timestamps
│   ├── facts                    # decisions, corrections, errors (deduped)
│   ├── files_touched            # path + action + count per session
│   └── tool_usage               # tool name + count per session
│
├── hooks/
│   ├── memcapture-hook.sh       # PreCompact → background capture
│   └── memcapture-inject.sh     # SessionStart → inject ~200 tokens
│
├── tools/
│   ├── memcapture.py            # JSONL parser + SQLite writer + FTS5 search
│   └── memcompile.py            # Cross-project compiler + health checks
│
├── skills/
│   ├── dream/SKILL.md           # /dream — memory consolidation
│   └── reflect/SKILL.md         # /reflect — pattern detection (advisory)
│
├── compiled-knowledge/          # Generated by memcompile.py
│   ├── health.md                # Stale, duplicate, oversized memories
│   └── concepts.md              # Cross-project recurring themes
│
└── projects/
    └── */memory/*.md            # Native Claude Code memory files
```

## SQLite Schema

```sql
-- Sessions table
CREATE TABLE sessions (
    session_id TEXT UNIQUE NOT NULL,
    project TEXT NOT NULL,
    cwd TEXT,
    branch TEXT,
    topic TEXT,
    message_count INTEGER,
    tool_count INTEGER,
    captured_at TEXT
);

-- Facts table (decisions, corrections, errors)
CREATE TABLE facts (
    session_id TEXT REFERENCES sessions(session_id),
    type TEXT CHECK(type IN ('decision', 'correction', 'error', 'topic')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL  -- MD5 for dedup
);

-- FTS5 virtual table for full-text search
CREATE VIRTUAL TABLE facts_fts USING fts5(content, type, project);

-- Files and tool usage
CREATE TABLE files_touched (session_id, path, action, count);
CREATE TABLE tool_usage (session_id, tool_name, count);
```

## Design Principles

1. **Zero ambient cost** — hooks run in background with no LLM calls. Only ~200 tokens injected at SessionStart.
2. **100% local** — no external services, no API keys for capture, no cloud sync.
3. **Regex over LLM** — fact extraction uses pattern matching, not AI. Fast, free, deterministic.
4. **Dedup by default** — MD5 hashing prevents duplicate facts across sessions.
5. **FTS5 search** — full-text search with unicode tokenizer, not SQL LIKE.
6. **Advisory skills** — `/reflect` proposes rules but never writes. You stay in control.
7. **Idempotent install** — running `install.sh` twice won't duplicate hooks or break things.

## License

MIT
