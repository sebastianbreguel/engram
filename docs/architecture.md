# Architecture

claude-engram is a Claude Code plugin with **two hooks, one Python entrypoint**, and a local SQLite store. No daemon, no network I/O, no API keys.

## File layout (after install)

```
~/.claude/
├── memory.db                    # SQLite — sessions, facts, memories, compactions
├── patterns/                    # Obsidian-compatible wiki (mempatterns output)
├── session-env/                 # Pre-compact work-state snapshots (markdown)
│
├── tools/
│   ├── engram.py                # Unified CLI + hook orchestrator (single entrypoint)
│   ├── memcapture.py            # JSONL parser + SQLite writer + FTS5 search
│   ├── memcompile.py            # Cross-project concept compiler (deprecated, see CHANGELOG)
│   ├── mempatterns.py           # Pattern detection (file co-edits, tool bias, errors)
│   └── memdashboard.py          # HTML dashboard generator
│
└── skills/
    ├── memclean/SKILL.md        # /memclean — consolidation
    ├── reflect/SKILL.md         # /reflect — advisory pattern detection
    └── patterns/SKILL.md        # /patterns — browse the wiki
```

## Hook wiring

Registered in `.claude-plugin/plugin.json` + `hooks/hooks.json`. Two events, two commands:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "uv run ${CLAUDE_PLUGIN_ROOT}/tools/engram.py on-session-start"}]}],
    "PreCompact":   [{"hooks": [{"type": "command",
      "command": "uv run ${CLAUDE_PLUGIN_ROOT}/tools/engram.py on-precompact"}]}]
  }
}
```

Manual installs use `$HOME/.claude` instead of `$CLAUDE_PLUGIN_ROOT` — both paths work.

## Orchestration

### PreCompact (`engram.py on-precompact`)

1. **Synchronous:** parse transcript + upsert to `memory.db` (sessions, facts, files, tools).
2. **Fire-and-forget:** spawn detached Haiku 4.5 subprocess for **digest** (preferences, practices, handoff paragraph) — result ingested back via stdin into `memories`.
3. **Fire-and-forget:** spawn detached Haiku 4.5 subprocess for **snapshot** (JSON: task, files, last_error, summary) — stored in `compactions`.
4. **Synchronous:** run `mempatterns --update` over the *previous* session's memories to refresh `~/.claude/patterns/`.

The fire-and-forget subprocesses use `start_new_session=True` so they survive the parent hook's exit. No lockfile — concurrent compactions are absorbed by `PRAGMA busy_timeout=5000` + `UNIQUE(topic)` on `memories`.

### SessionStart (`engram.py on-session-start`)

1. Read `cwd` from Claude Code's hook payload, derive `project_key`.
2. Call `memcapture --inject --inject-project=<key>` to build ~350 tokens: durable memories (all projects) + ephemeral memories (this project only) + latest snapshot + per-project handoff paragraph.
3. Optionally emit a banner (`ENGRAM_SHOW_BANNER=1` by default) via `systemMessage`.
4. Return JSON with `hookSpecificOutput.additionalContext` — Claude Code injects this silently into the new session.

## SQLite schema

```sql
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT UNIQUE NOT NULL,
    project TEXT NOT NULL,
    cwd TEXT, branch TEXT, topic TEXT,
    message_count INTEGER, tool_count INTEGER,
    captured_at TEXT NOT NULL,
    transcript_path TEXT
);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    type TEXT CHECK(type IN ('decision', 'correction', 'error', 'topic')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,        -- MD5[0:12] for dedup
    source_line INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    -- v2 widening (nullable, added via idempotent ALTER):
    subject TEXT, predicate TEXT, object TEXT, confidence REAL
);

CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    topic TEXT UNIQUE NOT NULL,        -- collision absorber: latest extraction wins
    content TEXT NOT NULL,
    durability TEXT CHECK(durability IN ('durable', 'ephemeral')),
    created_at TEXT, last_accessed TEXT,
    source_session TEXT
);

CREATE TABLE compactions (
    id INTEGER PRIMARY KEY,
    session_id TEXT, project TEXT NOT NULL,
    snapshot TEXT,                     -- JSON: {task, files, last_error, summary}
    compacted_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE files_touched (session_id, path, action, count);
CREATE TABLE tool_usage     (session_id, tool_name, count);

-- Full-text search (unicode tokenizer, standalone table)
CREATE VIRTUAL TABLE facts_fts USING fts5(content, type, project, tokenize='unicode61');

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;            -- collision absorber
```

## Design principles

1. **Near-zero ambient cost** — ~350 tokens injected at SessionStart. LLM work is offloaded to a detached subprocess that runs between sessions.
2. **100% local** — no external services, no API keys. LLM calls go through `claude --print` using the user's existing Claude Code auth.
3. **Tool-result signal over regex** — error detection relies on Claude Code's `is_error=true` on tool results, not text heuristics. Fewer false positives, zero regex maintenance.
4. **Dedup by default** — MD5 content hash prevents duplicate facts across sessions.
5. **Topic-keyed upsert** — `UNIQUE(topic)` on `memories`. Same topic = one row, latest wins. No contradictions, no merge logic.
6. **Durable vs ephemeral** — preferences persist indefinitely; project state expires in 7 days. Ephemeral memories are scoped to the current `cwd`.
7. **Collision absorber, not coordination** — two PreCompact hooks racing on the same session are absorbed by `PRAGMA busy_timeout=5000` + `UNIQUE(topic)`. No lockfile, no coordinator. Cost: occasional redundant Haiku call.
8. **Idempotent schema migrations** — `facts` widens via `ALTER TABLE ADD COLUMN` for nullable columns only. No `PRAGMA user_version` framework. v2 typed constraints will need one.
9. **Patterns runs on previous session's memories** — PreCompact order is: capture (sync) → digest (async) → patterns (sync). Patterns reflect what the last compaction wrote, not this one. By design.
10. **Advisory skills** — `/memclean`, `/reflect`, `/patterns` can suggest but never auto-write. You stay in control.
11. **Idempotent install** — re-running `install.sh` strips legacy `.sh` hook entries from `settings.json` and reinstalls the unified `engram.py` hooks. `memory.db` and `patterns/` are preserved.

## Why one entrypoint

The v0.1 architecture had 4-5 shell scripts calling individual Python modules. v1 collapsed that to `engram.py` with argparse subcommands. The hooks just invoke `engram.py on-precompact` / `engram.py on-session-start`; everything else (capture, digest dispatch, snapshot dispatch, patterns update, banner) is regular Python inside one process.

This trades a tiny bit of startup time for a simpler mental model, a single place to debug, and no shell-quoting landmines when user settings paths contain spaces.
