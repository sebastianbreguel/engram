# Architecture

claude-engram is a Claude Code plugin with **three hooks, one Python entrypoint**, and a local SQLite store. No daemon, no network I/O, no API keys.

## File layout (after install)

```
~/.claude/
├── memory.db                    # SQLite — sessions, facts, memories, compactions
├── patterns/                    # Obsidian-compatible wiki (mempatterns output)
├── session-env/                 # Pre-compact work-state snapshots (markdown)
├── engram/
│   └── executive/<cwd-slug>.md       # Per-project executive summary cache (3 bullets)
│   └── executive/<cwd-slug>.md.prev  # Rotated previous summary (safety net)
│
├── tools/
│   ├── engram.py                # Unified CLI + hook orchestrator (single entrypoint)
│   ├── memcapture.py            # JSONL parser + SQLite writer + FTS5 search
│   ├── mempatterns.py           # Pattern detection (file co-edits, tool bias, errors)
│   └── memdoctor.py             # Friction signal detector (correction-heavy, error-loop, restart-cluster, ...)
│
└── skills/
    ├── reflect/SKILL.md         # /reflect — memory consolidation + advisory rule proposals
    └── patterns/SKILL.md        # /patterns — browse the wiki
```

## Hook wiring

Registered in `.claude-plugin/plugin.json` + `hooks/hooks.json`. Three events, three commands:

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command",
      "command": "uv run ${CLAUDE_PLUGIN_ROOT}/tools/engram.py on-session-start"}]}],
    "PreCompact":       [{"hooks": [{"type": "command",
      "command": "uv run ${CLAUDE_PLUGIN_ROOT}/tools/engram.py on-precompact"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "uv run ${CLAUDE_PLUGIN_ROOT}/tools/engram.py on-user-prompt"}]}]
  }
}
```

Manual installs use `$HOME/.claude` instead of `$CLAUDE_PLUGIN_ROOT` — both paths work.

## Orchestration

### PreCompact (`engram.py on-precompact`)

1. **Synchronous:** parse transcript + upsert to `memory.db` (sessions, facts, files, tools).
2. **Fire-and-forget:** spawn detached Sonnet 4.6 subprocess for **digest** (preferences, practices, handoff paragraph) — result ingested back via stdin into `memories`.
3. **Fire-and-forget:** spawn detached Sonnet 4.6 subprocess for **snapshot** (JSON: task, files, last_error, summary) — stored in `compactions`.
4. **Synchronous:** run `mempatterns --update` over the *previous* session's memories to refresh `~/.claude/patterns/`.
5. **Fire-and-forget:** spawn detached Sonnet 4.6 subprocess for **executive** — merges Claude Code's `※ recap` + engram's inject_context + `memdoctor` friction signals + git state (branch + dirty files) into a 3-bullet summary (`status` / `last change` / `next`) cached at `~/.claude/engram/executive/<cwd-slug>.md`. Previous cache is rotated to `<cwd-slug>.md.prev` before overwrite as a safety net.

The fire-and-forget subprocesses use `start_new_session=True` so they survive the parent hook's exit. No lockfile — concurrent compactions are absorbed by `PRAGMA busy_timeout=5000` + `UNIQUE(topic)` on `memories`. The executive cache is overwrite-only (latest wins), with one-step rollback via `engram preview --prev`.

### UserPromptSubmit (`engram.py on-user-prompt`)

1. Read `session_id` from payload; bump a per-session counter in `~/.claude/engram/counter.json`.
2. If count `>= ENGRAM_DIGEST_EVERY` (default 25), fire-and-forget a **digest** subprocess + **executive** rebuild, then reset the counter.
3. Returns immediately; the active session is never blocked.

This keeps long sessions from going stale: if you hit compaction rarely, mid-session digests still refresh memories and the executive cache every ~25 turns.

### SessionStart (`engram.py on-session-start`)

1. Read `cwd` from Claude Code's hook payload, derive `project_key`.
2. **Fast path:** if `~/.claude/engram/executive/<cwd-slug>.md` exists, read it (~90 chars) and inject as `additionalContext`. Zero LLM call, zero latency.
3. **Fallback:** call `memcapture --inject --inject-project=<key>` to build ~350 tokens: durable memories (all projects) + ephemeral memories (this project only) + latest snapshot + per-project handoff paragraph.
4. Optionally emit a banner (`ENGRAM_SHOW_BANNER=1` by default) via `systemMessage`.
5. Return JSON with `hookSpecificOutput.additionalContext` — Claude Code injects this silently into the new session.

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
    -- v2 columns (nullable, unused in v1):
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
7. **Collision absorber, not coordination** — two PreCompact hooks racing on the same session are absorbed by `PRAGMA busy_timeout=5000` + `UNIQUE(topic)`. No lockfile, no coordinator. Cost: occasional redundant Sonnet call.
8. **Schema baseline stamped at `user_version=1`** — columns live in `CREATE TABLE IF NOT EXISTS`. Future typed constraints hook into a `PRAGMA user_version` migration ladder.
9. **Patterns runs on previous session's memories** — PreCompact order is: capture (sync) → digest (async) → patterns (sync). Patterns reflect what the last compaction wrote, not this one. By design.
10. **Advisory skills** — `/reflect` consolidates memory (writes) and proposes CLAUDE.md rules (advisory); `/patterns` is read-only. CLAUDE.md is never auto-written.
11. **Idempotent install** — re-running `install.sh` strips legacy `.sh` hook entries from `settings.json` and reinstalls the unified `engram.py` hooks. `memory.db` and `patterns/` are preserved.

## Why one entrypoint

The v0.1 architecture had 4-5 shell scripts calling individual Python modules. v1 collapsed that to `engram.py` with argparse subcommands. The hooks just invoke `engram.py on-precompact` / `engram.py on-session-start` / `engram.py on-user-prompt`; everything else (capture, digest dispatch, snapshot dispatch, executive merge, patterns update, banner) is regular Python inside one process.

This trades a tiny bit of startup time for a simpler mental model, a single place to debug, and no shell-quoting landmines when user settings paths contain spaces.

## Why the executive cache

Claude Code emits its own `※ recap` (one-line summary of the current session, stored as `type:system, subtype:away_summary` in the session JSONL). engram extracts the latest recap matching the current `cwd`, merges it with its own inject_context, `memdoctor` friction signals, and git state, and asks Sonnet for a **3-bullet summary** (`status` / `last change` / `next`). The merge happens between sessions (PreCompact or every `ENGRAM_DIGEST_EVERY` prompts, default 25) so SessionStart stays latency-free.

The `next:` bullet prioritizes friction signals when present (e.g. if memdoctor flags an error-loop, `next:` addresses it before feature work). Inputs grow over time (more memories, newer recap, new signals); the output stays 3 bullets. Sonnet re-compresses from scratch each rebuild — no stale bullets accumulate. If a rebuild compresses badly, `engram preview --prev` prints the rotated previous cache.
