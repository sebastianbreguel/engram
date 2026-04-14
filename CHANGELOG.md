# Changelog

## Unreleased

### Changed
- Consolidated 5 shell hooks into 2 inline `engram.py` invocations (`on-precompact`, `on-session-start`). Net -381 lines.
- Pass A LLM calls now use Haiku 4.5 (was Sonnet).
- Removed semantic error regex from session capture; now relies only on Claude Code's `is_error=true` tool-result signal.

### Deprecated
- `engram compile` and `~/.claude/compiled-knowledge/` markdown artifact — planned removal in v2, to be replaced by an automatic cross-project `concepts` table in `memory.db`. Use `engram export-concepts` as the migration bridge.

### Migration
Existing installs: run `./install.sh` again to migrate `settings.json` from the 5 legacy `.sh` hook entries to the 2 new `engram.py` entries. Old shell scripts are removed automatically. `memory.db` and `patterns/` are preserved.

### Design notes — v1 constraints
Explicit bets baked into this release, so users can judge them before reporting them as bugs:

- **Concurrency is a collision absorber, not coordination.** Two PreCompact hooks firing on the same session spawn up to 2 Haiku subprocesses each; `PRAGMA busy_timeout=5000` + `UNIQUE(topic)` on `memories` absorb the rare race at the cost of occasional redundant LLM calls. No lockfile. Acceptable at Haiku 4.5 prices and single-user scale.
- **Schema evolution is idempotent-ALTER only.** `facts` widens via `ALTER TABLE ADD COLUMN` for nullable typed fields. No `PRAGMA user_version` migration framework. v2 typed constraints will need one.
- **`mempatterns` runs on the *previous* session's memories.** PreCompact orchestration is: sync capture → fire-and-forget Haiku digest → sync patterns. Patterns reflect what Haiku wrote last compaction, not this one. By design, not a bug.
- **`engram compile` + `compiled-knowledge/` markdown artifact is deprecated.** v2 replaces it with an automatic cross-project `concepts` table in `memory.db`. `engram export-concepts` is the migration bridge.
