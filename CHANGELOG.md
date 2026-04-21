# Changelog

## Unreleased

### Changed
- **Skills merged**: `/memclean` absorbed into `/reflect`. One unified reflection pass: Phase 1 orient → Phase 2 gather → Phase 3 detect patterns → Phase 4 consolidate memory (writes) → Phase 5 propose CLAUDE.md rules (advisory). Users who ran both now run one. Memory writes stay agentic; CLAUDE.md stays advisory.

### Removed
- `/memclean` skill (merged into `/reflect`). Delete `~/.claude/skills/memclean/` manually if present from an older install.

### Added
- `engram --version` top-level flag and `engram verify-install` subcommand: SHA256-compares `tools/*.py` in the repo against `~/.claude/tools/` and flags drift, missing files, or sync. Catches "forgot to re-run install.sh" silently breaking behavior.
- **SessionStart banner surfaces friction**: when memdoctor detects active signals (correction-heavy, error-loop, etc.) for the current project, banner appends `friction: <signal>(Nx), ... (run: engram doctor)`. Top 2 signals, ranked by count.
- `engram forget --expired`: delete ephemeral memories older than 7 days.
- `engram forget --project KEY`: delete ephemerals whose session project LIKE `%KEY%`.
- `engram forget --dry-run`: preview either bulk mode without deleting. Mode validation enforces exactly one of `{topic, --expired, --project}`.
- **Executive summary** at SessionStart: Sonnet merges Claude Code's `※ recap` (`away_summary`) with engram's inject_context into a **3-bullet summary** (`status` / `last change` / `next`), cached per-project at `~/.claude/engram/executive/<cwd-slug>.md`. Read on SessionStart with zero latency; rebuilt in background on PreCompact and every 25 prompts.
- **UserPromptSubmit hook** (`engram.py on-user-prompt`): counts prompts per session and fires mid-session digest + executive rebuild every `ENGRAM_DIGEST_EVERY` prompts (default 25). Keeps long sessions from going stale even without a PreCompact event.
- `engram preview` subcommand: prints (and builds if missing) the executive cache for the current `cwd`.
- `engram preview --prev`: prints the rotated previous cache (`.prev` safety net). Never triggers a rebuild. Recovers the last good summary when a rebuild compresses badly.
- `engram search <query>`: FTS5 search over captured facts (delegates to `memcapture`).
- `engram log --tail N`: tails `~/.claude/engram.log` — background LLM failures with UTC timestamps. Default N=20.
- `engram doctor` (memdoctor): detects friction signals across sessions — `correction-heavy`, `error-loop`, `keep-going-loop`, `rapid-corrections`, `restart-cluster`. `--per-project` emits one scoped rule block per project.
- **Friction-aware executive**: `memdoctor.signals_for_executive()` injects the top active signals into the executive prompt, and Sonnet prioritizes them in the `next:` bullet.
- **Git-aware snapshots**: snapshot subprocess now prepends branch + dirty file count (2s subprocess timeout, handles non-repo cleanly).
- **Executive cache `.prev` rotation**: before overwriting `<cwd-slug>.md`, the existing file is moved to `<cwd-slug>.md.prev` via `os.replace`. One-step rollback via `preview --prev`.
- `PRAGMA user_version = 1` stamped on first migration — baseline for future schema changes.

### Changed
- LLM calls now use Sonnet 4.6 (was Haiku 4.5). Haiku hit `Prompt is too long` on large contexts; Sonnet handles the merge reliably.
- Executive output format: `next: <line>` → **3-bullet punteo** (`status` / `last change` / `next`). Single-line was tried and reverted after user feedback — the 3-bullet variant survives compaction better at SessionStart.

### Fixed
- Fire-and-forget subprocesses pass arguments as `--flag=value` (inline form) instead of `--flag value` (separate tokens). Project slugs like `-Users-sebabreguel-...` start with `-` and were mis-parsed as another flag by argparse, producing `expected one argument` errors on every PreCompact / UserPromptSubmit rebuild.
- **`_run_llm` name collision**: `tools/engram.py` had two `def _run_llm` (helper at line 350, argparse handler at line 569). The second shadowed the first, so every call to the helper crashed with `TypeError: got an unexpected keyword argument 'chunk'`. Renamed helper to `_run_claude`. Regression guard added in `test_engram_cli.py`. Never shipped to production (`~/.claude/tools/engram.py` was on an older `_run_haiku` variant).

### Changed (previous drop)
- Consolidated 5 shell hooks into 2 inline `engram.py` invocations (`on-precompact`, `on-session-start`). Net -381 lines.
- Pass A LLM calls now use Haiku 4.5 (was Sonnet).
- Removed semantic error regex from session capture; now relies only on Claude Code's `is_error=true` tool-result signal.
- Collapsed 11 `_dispatch_*` wrapper functions into argparse's native `set_defaults(func=...)` pattern. `memcapture.run()` gained an explicit `out: TextIO` parameter so callers control stdout without `sys.stdout` swapping.
- Unified `_run_digest` / `_run_snapshot` into a single `_run_llm` driven by `_LLM_MODES` dict. Internal `_run-llm --mode {digest,snapshot}` subcommand replaces two separate ones.
- `_extract_chunk` now streams with `collections.deque(maxlen=tail_lines)` instead of loading the entire file into memory. Prevents OOM on large transcripts.

### Fixed
- All timestamps now use `datetime.now(timezone.utc)` — previously mixed naive and aware datetimes.
- Project-scoped LIKE queries now escape `%` and `_` wildcards via `_like_escape()` + `ESCAPE '\'`. Prevents cross-project memory leaks on paths like `my_project`.
- `parse_digest_output` deduplicates same-topic lines within a single batch (last wins). Prevents duplicate memories when Haiku emits the same topic twice.
- HANDOFF paragraphs capped at 2000 chars to prevent runaway injection.
- `install.sh` `ensure_hook` now scans all matcher entries (not just empty-matcher) before adding a hook, preventing duplicates when users have custom matchers.
- Silent LLM failures now log to `~/.claude/engram.log` with UTC timestamps (missing `claude` binary, timeouts, non-zero exit).

### Removed
- `engram dashboard` subcommand and `memdashboard.py` — 1,610 lines of HTML generation removed. Second-order concern; may return as a lightweight standalone tool.
- `engram compile` and `engram export-concepts` subcommands. `memcompile.py` is no longer installed.
- `jq` is no longer a runtime dependency (all shell scripts replaced by Python).

### Migration
Existing installs: run `./install.sh` again to migrate `settings.json` from the 5 legacy `.sh` hook entries to the 2 new `engram.py` entries. Old shell scripts are removed automatically. `memory.db` and `patterns/` are preserved.

### Design notes — v1 constraints
Explicit bets baked into this release, so users can judge them before reporting them as bugs:

- **Concurrency is a collision absorber, not coordination.** Two PreCompact hooks firing on the same session spawn up to 2 Sonnet subprocesses each; `PRAGMA busy_timeout=5000` + `UNIQUE(topic)` on `memories` absorb the rare race at the cost of occasional redundant LLM calls. No lockfile. Acceptable at single-user scale.
- **Schema baseline stamped at `user_version=1`.** `facts` widens via `ALTER TABLE ADD COLUMN` for nullable typed fields. Further typed constraints hook into the `PRAGMA user_version` migration ladder.
- **`mempatterns` runs on the *previous* session's memories.** PreCompact orchestration is: sync capture → fire-and-forget Sonnet digest → sync patterns. Patterns reflect what Sonnet wrote last compaction, not this one. By design, not a bug.
