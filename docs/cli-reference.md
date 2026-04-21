# CLI Reference

Everything goes through `engram.py`, installed at `~/.claude/tools/engram.py` (or `${CLAUDE_PLUGIN_ROOT}/tools/engram.py` when used as a Claude Code plugin).

All invocations use `uv run` — the script declares its dependencies inline (`# /// script`), so no venv activation needed.

## Everyday commands

```bash
# Inspect what claude-engram knows
uv run ~/.claude/tools/engram.py --version          # print installed version
uv run ~/.claude/tools/engram.py verify-install     # SHA256-check repo tools vs ~/.claude/tools (drift guard)
uv run ~/.claude/tools/engram.py stats              # counts, durability split, projects
uv run ~/.claude/tools/engram.py memories           # list all learned memories
uv run ~/.claude/tools/engram.py search <query>     # FTS5 search over captured facts
uv run ~/.claude/tools/engram.py log --tail 20      # tail ~/.claude/engram.log (background LLM failures)
uv run ~/.claude/tools/engram.py usage              # count agent/skill/plugin invocations (stalest first)

# Forget memories (single or bulk; exactly one mode required)
uv run ~/.claude/tools/engram.py forget <topic>                 # delete one memory by topic
uv run ~/.claude/tools/engram.py forget --expired               # delete ephemeral memories older than 7 days
uv run ~/.claude/tools/engram.py forget --project <key>         # delete ephemerals for a project (cwd substring)
uv run ~/.claude/tools/engram.py forget --expired --dry-run     # preview without deleting
uv run ~/.claude/tools/engram.py forget --project <key> --dry-run

# Self-check: detect YOUR prompting habits (vague prompt → tool cascade)
uv run ~/.claude/tools/engram.py self-check         # last 10 vague-prompt cascades
uv run ~/.claude/tools/engram.py self-check --limit 20

# Friction signals (memdoctor). Top signals also surface on the SessionStart banner.
uv run ~/.claude/tools/engram.py doctor              # detect correction-heavy, error-loop, keep-going, rapid-corrections, restart-cluster
uv run ~/.claude/tools/engram.py doctor --per-project   # one scoped rule block per project

# Executive summary (per-project cache, rebuilt in background)
uv run ~/.claude/tools/engram.py preview             # print current summary (builds if missing)
uv run ~/.claude/tools/engram.py preview --prev      # print rotated previous summary (safety net — never rebuilds)

# Pattern wiki (emergent, opt-in exploration)
uv run ~/.claude/tools/engram.py patterns --report   # detected file co-edits, tool bias, recurring errors
uv run ~/.claude/tools/engram.py patterns --status   # wiki stats
uv run ~/.claude/tools/engram.py patterns --update   # rebuild wiki (normally happens on PreCompact)
```

`verify-install` must be run from the repo (never from the install path). It reports `drift` (SHA mismatch), `missing` (installed but deleted), or `OK` (all in sync). Exit code 0 = sync, 1 = drift/missing. Remedy: re-run `./install.sh`.

## Capture (manual)

Capture normally runs on PreCompact, but you can run it by hand for backfill or testing:

```bash
uv run ~/.claude/tools/engram.py capture                     # capture current session
uv run ~/.claude/tools/engram.py capture --all               # backfill all uncaptured transcripts
uv run ~/.claude/tools/engram.py capture --transcript FILE   # capture a specific .jsonl
```

## Inject (what SessionStart sees)

```bash
uv run ~/.claude/tools/engram.py inject                           # durable only (global)
uv run ~/.claude/tools/engram.py inject --project my-proj-key     # durable + ephemeral + snapshot
```

## Digest / snapshot (plumbing)

These normally run as fire-and-forget subprocesses spawned by the PreCompact hook. You'd only run them manually if you're piping an external digest:

```bash
# Ingest a digest produced outside the hook (format: `topic | durability | content` lines, blank line, "HANDOFF: ...")
cat digest.txt | uv run ~/.claude/tools/engram.py digest --session-id <sid> --project <project-key>

# Ingest a snapshot (JSON)
cat snapshot.json | uv run ~/.claude/tools/engram.py snapshot --session-id <sid> --project <project-key>
```

## Hooks (called by Claude Code, not by you)

```bash
uv run ~/.claude/tools/engram.py on-precompact      # called by Claude Code on compaction
uv run ~/.claude/tools/engram.py on-session-start   # called by Claude Code on session start
```

If you ever need to test hook logic by hand, pipe the hook payload to stdin:

```bash
echo '{"session_id":"abc","cwd":"/path/to/project"}' | \
  uv run ~/.claude/tools/engram.py on-session-start
```

## Skills (on-demand, zero cost until invoked)

| Command | What it does |
|---|---|
| `/reflect`  | Consolidate memory files (writes) + propose CLAUDE.md rules (advisory) from recent sessions |
| `/patterns` | Browse the `~/.claude/patterns/` wiki from inside Claude Code |

## Token budget

| Component | Tokens | When |
|---|---|---|
| MEMORY.md | ~150 | Every session (native Claude Code memory) |
| SessionStart inject | ~350 | Every session (durable + ephemeral + snapshot) |
| `on-precompact` capture | 0 | Background, no LLM |
| `on-precompact` digest | ~2-5K input | Background, detached Sonnet 4.6 subprocess |
| `on-precompact` snapshot | ~2-3K input | Background, detached Sonnet 4.6 subprocess |
| `on-precompact` executive | ~1-2K input | Background, detached Sonnet 4.6 subprocess |
| `on-user-prompt` digest + executive | ~2-5K input | Every 25 prompts, detached Sonnet 4.6 |
| `on-precompact` patterns | 0 | Background, no LLM |
| `/reflect`  | ~900 | Only when invoked |
| `/patterns` | ~300 | Only when invoked |
| **Ambient total** | **~350** | **Per session** |

## Manual install

If you skip `install.sh`:

```bash
# Copy tools
cp tools/engram.py        ~/.claude/tools/
cp tools/memcapture.py    ~/.claude/tools/
cp tools/mempatterns.py   ~/.claude/tools/
cp tools/memdoctor.py     ~/.claude/tools/
chmod +x ~/.claude/tools/engram.py

# Copy skills
cp -r skills/reflect   ~/.claude/skills/
cp -r skills/patterns  ~/.claude/skills/
```

Then add these two hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "uv run $HOME/.claude/tools/engram.py on-precompact"}
      ]}
    ],
    "SessionStart": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "uv run $HOME/.claude/tools/engram.py on-session-start"}
      ]}
    ],
    "UserPromptSubmit": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "uv run $HOME/.claude/tools/engram.py on-user-prompt"}
      ]}
    ]
  }
}
```

Initial backfill of existing transcripts (optional):

```bash
uv run ~/.claude/tools/engram.py capture --all
```

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ENGRAM_SHOW_BANNER` | `1` | Set to `0` to suppress the SessionStart banner (context still injects) |
| `ENGRAM_SKIP_LLM` | unset | Set to `1` to skip all Sonnet calls (useful for offline testing) |
| `ENGRAM_DIGEST_EVERY` | `25` | Mid-session digest + executive rebuild cadence (UserPromptSubmit) |

## Upgrading from v0.1

v0.1 used 5 shell hooks (`memcapture-hook.sh`, `memcapture-inject.sh`, `memdigest-hook.sh`, `memcompact-hook.sh`, `mempatterns-hook.sh`). v1 consolidates them into two `engram.py` subcommands.

Re-run `./install.sh`: it strips the legacy `.sh` entries from `settings.json` and writes the unified hook entries. `memory.db` and `~/.claude/patterns/` are preserved.
