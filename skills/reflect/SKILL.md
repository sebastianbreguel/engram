---
name: reflect
description: "Unified reflection pass — consolidate memory files, prune the index, and propose CLAUDE.md rule updates from recent sessions. Use when asked to 'reflect', 'consolidate memory', 'clean up memories', 'prune memories', 'find patterns', 'what have I been doing', 'update my rules', or after accumulating 5+ sessions. Memory files: writes/merges/prunes with judgment. CLAUDE.md: advisory only — never writes without explicit approval."
---
# Reflect: Memory Consolidation + Rule Proposals

You are performing a reflection. Two outputs:
1. **Memory pass** — consolidate, merge, prune memory files directly.
2. **Rules pass** — propose CLAUDE.md updates (advisory; wait for approval).

Memory directory: `${MEMORY_DIR}`
${MEMORY_DIR_CONTEXT}

Session snapshots: `~/.claude/session-env/`
Session transcripts: `${TRANSCRIPTS_DIR}` (large JSONL — grep narrowly, don't read whole files)

---

## Phase 1 — Orient

- `ls` the memory directory; skim existing topic files so you improve rather than duplicate.
- Read `${INDEX_FILE}` to understand the current index.
- Read current rules: project `.claude/CLAUDE.md` and global `~/.claude/CLAUDE.md`. You'll check them for violations and gaps.
- If `logs/` or `sessions/` subdirectories exist, review recent entries.

## Phase 2 — Gather signal

Sources in rough priority order:

1. **Session snapshots** — `ls -t ~/.claude/session-env/precompact-*.md | head -10`, read the 5 most recent (skip `precompact-last.md`, it's a duplicate).
2. **Daily logs** (`logs/YYYY/MM/YYYY-MM-DD.md`) if present — append-only stream.
3. **Memories that drifted** — facts contradicted by the current codebase.
4. **Targeted transcript search** — only for specific context you already suspect matters:
   `grep -rn "<narrow term>" ${TRANSCRIPTS_DIR}/ --include="*.jsonl" | tail -50`

Extract: decisions, user corrections, tool preferences, architecture choices, rule violations.

## Phase 3 — Detect patterns

Score each signal across six categories:

- **Persistent preferences** — tool choices, language, code style not captured in CLAUDE.md.
- **Design decisions that stuck** — architecture, libraries, workflows chosen and not reverted.
- **Anti-patterns** — approaches corrected, abandoned, or that caused rework.
- **Efficiency lessons** — shortcuts found, unnecessary steps eliminated.
- **Project-specific patterns** — naming, file organization, testing habits.
- **Rule violations (highest priority)** — for every existing CLAUDE.md rule: violated, validated, or stale?

Frequency threshold:
- 1 occurrence → ignore (one-off).
- 2 occurrences → emerging (note).
- 3+ occurrences → strong (recommend).

Consider consistency (contradicted anywhere?) and scope (global vs project-specific).

## Phase 4 — Consolidate memory

Write or update memory files at the top level of the memory directory. Use the memory format and `type:` conventions from your system prompt's auto-memory section.

- **Merge** new signal into existing topic files; don't create near-duplicates.
- **Convert relative dates** ("yesterday", "last week") to absolute dates so entries stay interpretable.
- **Delete contradicted facts** at the source — fix the file, don't paper over.

Update `${INDEX_FILE}` (keep under ${INDEX_MAX_LINES} lines AND ~25KB):
- One-line entries: `- [Title](file.md) — one-line hook`.
- Never write memory content directly into the index.
- Remove stale pointers, add new ones, resolve contradictions.
- If a line exceeds ~200 chars, move the detail into its topic file.

**Temporal review** — scan `verified:` frontmatter fields:
- No `verified:` → add `verified: [today's date]`.
- Older than 30 days → check against current state. Update, fix, or delete. Flag as "needs human review" if uncertain.

## Phase 5 — Propose rule updates

Present findings as a structured report. **DO NOT write to CLAUDE.md** — propose and wait.

```markdown
## Reflection Report — [date]

### Strong patterns (3+ occurrences)
1. [Pattern] — evidence: [sessions/memories] — proposed rule: `[one-line imperative]`

### Emerging patterns (2 occurrences)
1. [Pattern] — evidence: [...] — proposed rule: `[...]`

### Rule violations detected
1. [Rule] violated in [context] — suggested strengthening: [how]

### Stale rules (consider removing)
1. [Rule] — not relevant recently because [reason]

### Memory health
- Consolidated: [list]
- Pruned: [list]
- Flagged for review: [list]
```

---

## Rules

1. **Memory files**: write/update/delete with judgment — this is the consolidation job.
2. **CLAUDE.md**: NEVER write directly. Propose rules and wait for approval.
3. **Proposed rules are one-line, imperative.** No verbose explanations in CLAUDE.md.
4. **Distinguish global vs project-scoped.** A React pattern does not belong in global CLAUDE.md.
5. **Propose rules only for 2+ occurrences.** Ignore one-offs.
6. **Credit evidence.** Every proposal cites sessions or memories.
7. **Convert relative dates to absolute** at write time.

Return a brief summary: what was consolidated/pruned and what rules were proposed. If nothing changed, say so.${ADDITIONAL_CONTEXT?`

## Additional context

${ADDITIONAL_CONTEXT}`:""}
