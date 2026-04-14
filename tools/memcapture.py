#!/usr/bin/env python3
"""memcapture — Automatic session memory capture into SQLite.

Reads JSONL transcripts from Claude Code sessions and extracts useful facts
(decisions, corrections, files touched, tool patterns, errors) using heuristics.
No LLM calls — zero token cost.

Usage:
    uv run ~/.claude/tools/memcapture.py                    # capture current session
    uv run ~/.claude/tools/memcapture.py --all              # capture all uncaptured sessions
    uv run ~/.claude/tools/memcapture.py --query "react"    # FTS5 search captured facts
    uv run ~/.claude/tools/memcapture.py --stats            # show capture statistics
    uv run ~/.claude/tools/memcapture.py --recent 5         # show last N sessions
    uv run ~/.claude/tools/memcapture.py --inject           # output context for SessionStart (~200 tokens)
"""
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "memory.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# --- Heuristic patterns ---

DECISION_PATTERNS = [
    # Require context: "decided to/that/on", "let's go with X", "switching to X"
    re.compile(
        r"\b(decided\s+(to|that|on)|chose\s+\w|let'?s go with|we'?ll use|switching to|picked\s+\w)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(vamos con\s+\w|decidí\s+\w|elegí\s+\w|usemos\s+\w|mejor usar\s+\w)",
        re.IGNORECASE,
    ),
    # "prefer X over Y" or "prefer to" — not bare "prefer"
    re.compile(r"\bprefer\s+(to\s+\w|\w+\s+over)\b", re.IGNORECASE),
    re.compile(r"\bprefiero\s+\w", re.IGNORECASE),
]
CORRECTION_PATTERNS = [
    re.compile(r"^(no[,.\s]|not that|don'?t |stop |wrong |nope[,.\s])", re.IGNORECASE),
    # Spanish: "eso no", "no así", "mejor no" — removed bare "para" and "mal" (too broad)
    re.compile(r"^(no[,.\s]|eso no|no así|mejor no)", re.IGNORECASE),
]
FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
BRANCH_RE = re.compile(r"(?:On branch|branch\s+)(\S+)")


class MemoryDB:
    """SQLite-backed session memory store with FTS5 search."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._widen_facts()

    def _create_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                project TEXT NOT NULL,
                cwd TEXT,
                branch TEXT,
                topic TEXT,
                message_count INTEGER DEFAULT 0,
                tool_count INTEGER DEFAULT 0,
                captured_at TEXT NOT NULL,
                transcript_path TEXT
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                type TEXT NOT NULL CHECK(type IN ('decision', 'correction', 'error', 'topic')),
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_line INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS files_touched (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                path TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('read', 'edit', 'write', 'create')),
                count INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS tool_usage (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                tool_name TEXT NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(session_id, tool_name)
            );

            CREATE INDEX IF NOT EXISTS idx_facts_session ON facts(session_id);
            CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(type);
            CREATE INDEX IF NOT EXISTS idx_facts_hash ON facts(content_hash);
            CREATE INDEX IF NOT EXISTS idx_files_session ON files_touched(session_id);
            CREATE INDEX IF NOT EXISTS idx_files_path ON files_touched(path);
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                topic TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                durability TEXT NOT NULL CHECK(durability IN ('durable', 'ephemeral')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_accessed TEXT NOT NULL DEFAULT (datetime('now')),
                source_session TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_memories_durability ON memories(durability);
            CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON memories(last_accessed DESC);

            CREATE TABLE IF NOT EXISTS compactions (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                project TEXT NOT NULL,
                snapshot TEXT,
                compacted_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_compactions_project ON compactions(project);
        """)

        # FTS5 standalone table — we insert directly, no content sync needed
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(content, type, project, tokenize='unicode61')")
        except sqlite3.OperationalError:
            pass  # FTS5 not available on this SQLite build

        self.conn.commit()

    def _widen_facts(self) -> None:
        """Idempotent schema widen: add nullable typed columns for v2. v1 leaves them NULL."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(facts)").fetchall()}
        for col in ("subject", "predicate", "object"):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE facts ADD COLUMN {col} TEXT")
        if "confidence" not in existing:
            self.conn.execute("ALTER TABLE facts ADD COLUMN confidence REAL")
        self.conn.commit()

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def is_captured(self, session_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row is not None

    def fact_exists(self, content_hash: str) -> bool:
        """Dedup: check if a fact with this hash already exists."""
        row = self.conn.execute("SELECT 1 FROM facts WHERE content_hash = ?", (content_hash,)).fetchone()
        return row is not None

    def save_session(self, session: SessionData) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, project, cwd, branch, topic, message_count, tool_count, captured_at, transcript_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.project,
                session.cwd,
                session.branch,
                session.topic,
                session.message_count,
                session.tool_count,
                datetime.now().isoformat(),
                session.transcript_path,
            ),
        )

        for fact in session.facts:
            content_hash = self._content_hash(fact["content"])
            if self.fact_exists(content_hash):
                continue  # dedup
            self.conn.execute(
                "INSERT INTO facts (session_id, type, content, content_hash, source_line) VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    fact["type"],
                    fact["content"],
                    content_hash,
                    fact.get("source_line"),
                ),
            )
            # Incremental FTS insert — no full rebuild needed
            try:
                self.conn.execute(
                    "INSERT INTO facts_fts (content, type, project) VALUES (?, ?, ?)",
                    (fact["content"], fact["type"], session.project),
                )
            except sqlite3.OperationalError:
                pass

        for path, action_counts in session.files.items():
            for action, count in action_counts.items():
                self.conn.execute(
                    "INSERT INTO files_touched (session_id, path, action, count) VALUES (?, ?, ?, ?)",
                    (session.session_id, path, action, count),
                )

        for tool, count in session.tools.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO tool_usage (session_id, tool_name, count) VALUES (?, ?, ?)",
                (session.session_id, tool, count),
            )

        self.conn.commit()

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 search with fallback to LIKE."""
        try:
            rows = self.conn.execute(
                "SELECT content, type, project, rank FROM facts_fts WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            if rows:
                return [
                    {
                        "type": r["type"],
                        "content": r["content"],
                        "project": r["project"],
                        "created_at": "",
                    }
                    for r in rows
                ]
        except sqlite3.OperationalError:
            pass
        # Fallback: case-insensitive LIKE
        rows = self.conn.execute(
            "SELECT f.type, f.content, f.created_at, s.project FROM facts f JOIN sessions s ON f.session_id = s.session_id WHERE f.content LIKE ? COLLATE NOCASE ORDER BY f.created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        sessions = self.conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        facts = self.conn.execute("SELECT type, COUNT(*) as c FROM facts GROUP BY type").fetchall()
        files = self.conn.execute("SELECT COUNT(DISTINCT path) as c FROM files_touched").fetchone()["c"]
        top_tools = self.conn.execute(
            "SELECT tool_name, SUM(count) as total FROM tool_usage GROUP BY tool_name ORDER BY total DESC LIMIT 10"
        ).fetchall()
        top_files = self.conn.execute(
            "SELECT path, SUM(count) as total FROM files_touched GROUP BY path ORDER BY total DESC LIMIT 10"
        ).fetchall()
        return {
            "sessions": sessions,
            "facts_by_type": {r["type"]: r["c"] for r in facts},
            "unique_files": files,
            "top_tools": [(r["tool_name"], r["total"]) for r in top_tools],
            "top_files": [(r["path"], r["total"]) for r in top_files],
        }

    def recent_sessions(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.session_id, s.project, s.branch, s.topic, s.message_count, s.tool_count, s.captured_at, (SELECT COUNT(*) FROM facts f WHERE f.session_id = s.session_id) as fact_count FROM sessions s ORDER BY s.captured_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _git_recent_commits(self, cwd: str | None, limit: int = 5) -> list[str]:
        """Get recent commit onelines for a project directory."""
        if not cwd:
            return []
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", f"-{limit}"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return []

    @staticmethod
    def _relative_time(timestamp: str | None) -> str:
        """Short relative-time string: 'just now', '2h ago', '3d ago'."""
        if not timestamp:
            return "never"
        try:
            ts = timestamp.replace("T", " ").replace("Z", "").split(".")[0]
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return "recently"
        delta = datetime.now() - dt
        secs = delta.total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        if secs < 86400 * 7:
            return f"{int(secs / 86400)}d ago"
        return f"{int(secs / 86400 / 7)}w ago"

    def build_banner(self, project: str | None = None, display_name: str | None = None) -> str:
        """Compact banner for SessionStart systemMessage — shown between welcome box and prompt."""
        if display_name:
            project_name = display_name[:40]
        elif project:
            cleaned = project.rstrip("-").split("-")[-1] or project
            project_name = cleaned[:40]
        else:
            project_name = "engram"

        if project:
            row = self.conn.execute(
                "SELECT COUNT(*) as c, MAX(captured_at) as last FROM sessions WHERE project LIKE ?",
                (f"%{project}%",),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) as c, MAX(captured_at) as last FROM sessions").fetchone()
        session_count = row["c"] if row else 0
        last_seen = self._relative_time(row["last"] if row else None)

        pref_count = self.conn.execute("SELECT COUNT(*) as c FROM memories WHERE durability = 'durable'").fetchone()["c"]

        handoff_line = None
        if project:
            handoff_topic = "handoff_" + re.sub(r"[^a-z0-9_]", "_", project.lower()).strip("_")
            hrow = self.conn.execute(
                "SELECT content FROM memories WHERE topic = ? LIMIT 1",
                (handoff_topic,),
            ).fetchone()
            if hrow and hrow["content"]:
                content = hrow["content"].strip().replace("\n", " ")
                first_sentence = re.split(r"(?<=[.!?])\s+", content, maxsplit=1)[0]
                handoff_line = first_sentence[:160]

        use_color = os.environ.get("NO_COLOR", "") == "" and os.environ.get("TERM", "") != "dumb"
        if use_color:
            C_RESET = "\033[0m"
            C_BRAND = "\033[1;35m"
            C_PROJ = "\033[1;36m"
            C_NUM = "\033[1;33m"
            C_DIM = "\033[90m"
            C_HANDOFF = "\033[1;32m"
            sep = f"{C_DIM}·{C_RESET}"
            header = (
                f"{C_BRAND}engram{C_RESET} {sep} "
                f"{C_PROJ}{project_name}{C_RESET} {sep} "
                f"{C_NUM}{session_count}{C_RESET} sessions {sep} "
                f"last {C_NUM}{last_seen}{C_RESET} {sep} "
                f"{C_NUM}{pref_count}{C_RESET} memories"
            )
            if handoff_line:
                return f"{header}\n{C_HANDOFF}↳{C_RESET} {handoff_line}"
            return header

        header = f"engram · {project_name} · {session_count} sessions · last {last_seen} · {pref_count} memories"
        if handoff_line:
            return f"{header}\n↳ {handoff_line}"
        return header

    def inject_context(self, project: str | None = None) -> str:
        """Generate ~350 token context block from learned memories + optional compaction snapshot."""
        self.cleanup_ephemeral()

        if project:
            # Durable memories (preferences) stay global — general guidance applies everywhere.
            # Ephemeral memories (current context) are prioritized by project match.
            rows = self.conn.execute(
                """
                SELECT m.topic, m.content, m.durability, m.last_accessed,
                       CASE
                         WHEN m.durability = 'durable' THEN 1
                         WHEN s.project LIKE ? THEN 1
                         ELSE 0
                       END AS keep_priority
                FROM memories m
                LEFT JOIN sessions s ON m.source_session = s.session_id
                ORDER BY keep_priority DESC, m.last_accessed DESC
                """,
                (f"%{project}%",),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT topic, content, durability, last_accessed FROM memories ORDER BY last_accessed DESC"
            ).fetchall()

        if not rows:
            output = self._fallback_inject(project)
        else:
            # Update last_accessed for included memories
            topics = [r["topic"] for r in rows]
            if topics:
                placeholders = ",".join("?" * len(topics))
                self.conn.execute(
                    f"UPDATE memories SET last_accessed = datetime('now') WHERE topic IN ({placeholders})",
                    topics,
                )
                self.conn.commit()

            # Separate the project-specific handoff from regular memories
            handoff_topic = None
            if project:
                handoff_topic = "handoff_" + re.sub(r"[^a-z0-9_]", "_", project.lower()).strip("_")

            handoff_row = None
            regular_rows = []
            for r in rows:
                if handoff_topic and r["topic"] == handoff_topic:
                    handoff_row = r
                else:
                    regular_rows.append(r)

            durable = [r for r in regular_rows if r["durability"] == "durable"]
            ephemeral = [r for r in regular_rows if r["durability"] == "ephemeral" and not r["topic"].startswith("handoff_")]

            lines = []
            if handoff_row:
                lines.append("<handoff>")
                lines.append(handoff_row["content"])
                lines.append("</handoff>")

            lines.append("<session-memory>")
            char_budget = 1400
            used = 0

            if durable:
                lines.append("Learned preferences & practices:")
                for r in durable:
                    line = f"- {r['content'][:120]}"
                    if used + len(line) > char_budget:
                        break
                    lines.append(line)
                    used += len(line)

            if ephemeral:
                lines.append("Current context:")
                for r in ephemeral:
                    line = f"- {r['content'][:120]}"
                    if used + len(line) > char_budget:
                        break
                    lines.append(line)
                    used += len(line)

            lines.append("</session-memory>")
            output = "\n".join(lines)

        # Append compaction snapshot if available
        if project:
            snapshot = self.get_latest_snapshot(project)
            if snapshot and snapshot["snapshot"]:
                output += "\n" + self._format_snapshot(snapshot["snapshot"])

        return output

    def _format_snapshot(self, snapshot_json: str, max_chars: int = 600) -> str:
        """Format a compaction snapshot as an injection block (capped to ~150 tokens)."""
        try:
            data = json.loads(snapshot_json)
        except (json.JSONDecodeError, TypeError):
            return ""

        lines = ["<compaction-snapshot>"]
        if data.get("task"):
            lines.append(f"Resuming: {data['task'][:120]}")
        if data.get("files"):
            files = data["files"] if isinstance(data["files"], list) else [data["files"]]
            lines.append(f"Files in progress: {', '.join(str(f) for f in files[:5])}")
        if data.get("last_error"):
            lines.append(f"Last error: {data['last_error'][:120]}")
        if data.get("summary"):
            lines.append(f"Context: {data['summary'][:200]}")
        lines.append("</compaction-snapshot>")
        result = "\n".join(lines)
        return result[:max_chars]

    def _fallback_inject(self, project: str | None = None) -> str:
        """Generate ~200 token context block for SessionStart injection (v1 fallback)."""
        if project:
            where_s = "WHERE s.project LIKE ?"
            where_sf = "WHERE s.project LIKE ? AND"
            params: list = [f"{project}%"]
        else:
            where_s = ""
            where_sf = "WHERE"
            params = []

        # Last 3 sessions with topics
        sessions = self.conn.execute(
            f"SELECT s.topic, s.branch, s.captured_at, s.cwd FROM sessions s {where_s} ORDER BY s.captured_at DESC LIMIT 3",
            params,
        ).fetchall()

        # Recent errors (last 3)
        errors = self.conn.execute(
            f"SELECT DISTINCT f.content FROM facts f JOIN sessions s ON f.session_id = s.session_id {where_sf} f.type = 'error' ORDER BY f.created_at DESC LIMIT 3",
            params,
        ).fetchall()

        # Git commits from the project cwd
        cwd = None
        if sessions:
            cwd = sessions[0]["cwd"]
        if not cwd and project:
            cwd = project.replace("-", "/")
            if not cwd.startswith("/"):
                cwd = "/" + cwd

        commits = self._git_recent_commits(cwd, limit=5)

        lines = ["<session-memory>"]
        if sessions:
            lines.append("Recent sessions:")
            for s in sessions:
                topic = s["topic"] or "?"
                branch = s["branch"] or ""
                lines.append(f"- {s['captured_at'][:10]} {f'({branch}) ' if branch else ''}{topic[:100]}")

        if commits:
            lines.append("Recent commits:")
            for c in commits:
                lines.append(f"- {c[:120]}")

        if errors:
            lines.append("Recent errors:")
            for e in errors:
                lines.append(f"- {e['content'][:120]}")

        lines.append("</session-memory>")
        return "\n".join(lines)

    def upsert_memory(
        self,
        topic: str,
        content: str,
        durability: str,
        source_session: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO memories (topic, content, durability, source_session)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(topic) DO UPDATE SET
                   content = excluded.content,
                   durability = excluded.durability,
                   last_accessed = datetime('now'),
                   source_session = excluded.source_session""",
            (topic, content, durability, source_session),
        )
        self.conn.commit()

    def cleanup_ephemeral(self) -> int:
        cursor = self.conn.execute("DELETE FROM memories WHERE durability = 'ephemeral' AND created_at < datetime('now', '-7 days')")
        self.conn.commit()
        return cursor.rowcount

    def list_memories(self, topic_pattern: str | None = None) -> list[dict]:
        if topic_pattern:
            rows = self.conn.execute(
                "SELECT topic, content, durability, created_at, last_accessed FROM memories WHERE topic LIKE ? ORDER BY last_accessed DESC",
                (topic_pattern,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT topic, content, durability, created_at, last_accessed FROM memories ORDER BY last_accessed DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def forget_memory(self, topic: str) -> bool:
        cursor = self.conn.execute("DELETE FROM memories WHERE topic = ?", (topic,))
        self.conn.commit()
        return cursor.rowcount > 0

    def forget_all_ephemeral(self) -> int:
        cursor = self.conn.execute("DELETE FROM memories WHERE durability = 'ephemeral'")
        self.conn.commit()
        return cursor.rowcount

    def save_compaction(self, session_id: str | None, project: str, snapshot: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO compactions (session_id, project, snapshot) VALUES (?, ?, ?)",
            (session_id, project, snapshot),
        )
        self.conn.commit()

    def get_latest_snapshot(self, project: str, max_age_hours: int = 2) -> dict | None:
        row = self.conn.execute(
            "SELECT session_id, snapshot, compacted_at FROM compactions WHERE project = ? AND snapshot IS NOT NULL AND compacted_at > datetime('now', ? || ' hours') ORDER BY compacted_at DESC LIMIT 1",
            (project, f"-{max_age_hours}"),
        ).fetchone()
        return dict(row) if row else None

    def compaction_stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) as c FROM compactions").fetchone()["c"]
        by_project = self.conn.execute(
            "SELECT project, COUNT(*) as c FROM compactions GROUP BY project ORDER BY c DESC LIMIT 10"
        ).fetchall()
        return {
            "total": total,
            "by_project": [(r["project"], r["c"]) for r in by_project],
        }

    def close(self) -> None:
        self.conn.close()


class SessionData:
    """Extracted data from a single session transcript."""

    def __init__(self, session_id: str, project: str, transcript_path: str):
        self.session_id = session_id
        self.project = project
        self.transcript_path = transcript_path
        self.cwd: str | None = None
        self.branch: str | None = None
        self.topic: str | None = None
        self.message_count = 0
        self.tool_count = 0
        self.facts: list[dict] = []
        self.files: dict[str, dict[str, int]] = {}
        self.tools: Counter = Counter()
        self._seen_hashes: set[str] = set()

    def add_fact(self, fact_type: str, content: str, source_line: int | None = None) -> None:
        """Add a fact with inline dedup within the session."""
        h = hashlib.md5(content.encode()).hexdigest()[:12]
        if h in self._seen_hashes:
            return
        self._seen_hashes.add(h)
        self.facts.append({"type": fact_type, "content": content, "source_line": source_line})


class TranscriptParser:
    """Parses JSONL transcripts and extracts structured data."""

    def parse_file(self, path: Path, project: str, extract_facts: bool = False) -> SessionData | None:
        session_id = path.stem
        session = SessionData(session_id, project, str(path))
        self._extract_facts = extract_facts
        first_user_msg = True

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line_num, raw_line in enumerate(lines):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "")

            if msg_type == "user":
                is_first = first_user_msg
                self._process_user_message(obj, session, line_num, is_first)
                # Track if we consumed a real user message for topic
                if session.topic and is_first:
                    first_user_msg = False
            elif msg_type == "assistant":
                self._process_assistant_message(obj, session, line_num)

        if session.message_count < 2:
            return None

        return session

    def _process_user_message(self, obj: dict, session: SessionData, line_num: int, is_first: bool) -> None:
        content = obj.get("message", {}).get("content", "")

        # Handle tool_result blocks
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    self._process_tool_result(block, session, line_num)

        text = self._extract_text(obj)
        if not text:
            return

        if text.startswith("<local-command") or text.startswith("<command-name>"):
            return

        clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
        # Also strip skill/command expansion tags
        clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", clean, flags=re.DOTALL).strip()
        if not clean or len(clean) < 5:
            return

        session.message_count += 1

        # --- Topic: first real user message ---
        if is_first and not session.topic:
            topic = clean[:200].split("\n")[0].strip()
            if len(topic) > 5:
                session.topic = topic

        # --- Branch: extract from git context in user messages ---
        if not session.branch:
            branch_match = BRANCH_RE.search(clean)
            if branch_match:
                session.branch = branch_match.group(1)

        # --- Decisions (opt-in) ---
        if self._extract_facts:
            for pattern in DECISION_PATTERNS:
                if pattern.search(clean):
                    for sentence in re.split(r"[.!?\n]", clean):
                        if pattern.search(sentence) and len(sentence.strip()) > 10:
                            session.add_fact("decision", sentence.strip()[:500], line_num)
                            break
                    break

            # --- Corrections (opt-in) ---
            for pattern in CORRECTION_PATTERNS:
                if pattern.search(clean) and len(clean) < 300:
                    session.add_fact("correction", clean[:500], line_num)
                    break

    def _process_tool_result(self, block: dict, session: SessionData, line_num: int) -> None:
        """Capture errors from tool_result blocks. Only trusts is_error=True — semantic judgment is the LLM digest's job."""
        is_error = block.get("is_error", False)
        raw_content = block.get("content", "")

        if isinstance(raw_content, list):
            result_text = " ".join(b.get("text", "") for b in raw_content if isinstance(b, dict))
        elif isinstance(raw_content, str):
            result_text = raw_content
        else:
            return

        # Preserve branch extraction from git status/branch output.
        if not session.branch:
            branch_match = BRANCH_RE.search(result_text)
            if branch_match:
                session.branch = branch_match.group(1)

        if not is_error:
            return

        first_line = result_text.strip().split("\n")[0][:500]
        if len(first_line) >= 15:
            session.add_fact("error", first_line, line_num)

    def _process_assistant_message(self, obj: dict, session: SessionData, line_num: int) -> None:
        content = obj.get("message", {}).get("content", [])
        if not isinstance(content, list):
            return

        session.message_count += 1

        for block in content:
            if not isinstance(block, dict):
                continue

            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                session.tools[tool_name] += 1
                session.tool_count += 1

                tool_input = block.get("input", {})

                # --- File paths from file tools ---
                if tool_name in FILE_TOOLS:
                    file_path = tool_input.get("file_path", "")
                    if file_path:
                        action_map = {
                            "Read": "read",
                            "Edit": "edit",
                            "Write": "write",
                            "NotebookEdit": "edit",
                        }
                        action = action_map.get(tool_name, "read")
                        session.files.setdefault(file_path, Counter())
                        session.files[file_path][action] += 1

                # --- Branch from Bash git commands ---
                if tool_name == "Bash" and not session.branch:
                    cmd = tool_input.get("command", "")
                    branch_cmd = re.search(r"git\s+(?:checkout|switch)\s+(\S+)", cmd)
                    if branch_cmd:
                        session.branch = branch_cmd.group(1)

    def _extract_text(self, obj: dict) -> str:
        content = obj.get("message", {}).get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
            return "\n".join(texts)
        return ""


def parse_digest_output(text: str, project: str | None = None) -> list[dict]:
    """Parse LLM digest output into memory dicts.
    Expected format per line: topic | durability | content
    Also captures trailing "HANDOFF: ..." paragraph as a per-project ephemeral memory.
    """
    memories = []
    handoff_lines: list[str] = []
    in_handoff = False
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if in_handoff:
            if line.strip():
                handoff_lines.append(line.strip())
            continue
        stripped = line.strip()
        if stripped.upper().startswith("HANDOFF:"):
            in_handoff = True
            rest = stripped[len("HANDOFF:") :].strip()
            if rest:
                handoff_lines.append(rest)
            continue
        if not stripped or stripped.startswith("#"):
            continue
        parts = [p.strip() for p in stripped.split("|", 2)]
        if len(parts) != 3:
            continue
        topic, durability, content = parts
        if durability not in ("durable", "ephemeral"):
            continue
        if not topic or not content:
            continue
        topic = re.sub(r"[^a-z0-9_]", "_", topic.lower().strip()).strip("_")
        if topic:
            memories.append({"topic": topic, "content": content, "durability": durability})

    if handoff_lines and project:
        handoff_topic = "handoff_" + re.sub(r"[^a-z0-9_]", "_", project.lower()).strip("_")
        memories.append(
            {
                "topic": handoff_topic,
                "content": " ".join(handoff_lines),
                "durability": "ephemeral",
            }
        )
    return memories


def find_transcripts() -> list[tuple[Path, str]]:
    results = []
    if not PROJECTS_DIR.exists():
        return results
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        for jsonl in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            results.append((jsonl, project_name))
    return results


def find_current_session() -> tuple[Path, str] | None:
    cwd = os.getcwd()
    project_key = cwd.replace("/", "-")
    project_dir = PROJECTS_DIR / project_key
    if not project_dir.is_dir():
        all_projects = sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if all_projects:
            project_dir = all_projects[0]
        else:
            return None

    jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if jsonls:
        return jsonls[0], project_dir.name
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="engram — persistent memory for Claude Code",
        epilog="Advanced/internal flags (used by hooks) are hidden. See docs/cli-reference.md.",
    )
    # User-facing flags (4 essentials)
    parser.add_argument("--stats", action="store_true", help="Show what engram has learned")
    parser.add_argument("--query", "-q", type=str, metavar="TERM", help="Search captured facts")
    parser.add_argument(
        "--memories",
        nargs="?",
        const="*",
        metavar="PATTERN",
        help="List learned memories (optional topic pattern)",
    )
    parser.add_argument("--forget", type=str, metavar="TOPIC", help="Delete a memory by topic")
    parser.add_argument("--dashboard", action="store_true", help="Open visual dashboard in browser")

    # Hook-internal flags (hidden from --help but still functional)
    parser.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--recent", type=int, metavar="N", help=argparse.SUPPRESS)
    parser.add_argument("--inject", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--inject-project", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--banner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--banner-project", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--banner-name", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--transcript", type=str, help=argparse.SUPPRESS)
    parser.add_argument(
        "--extract-facts",
        action="store_true",
        default=bool(os.environ.get("MEMCAPTURE_EXTRACT_FACTS")),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--ingest-digest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session-id", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--ephemeral", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ingest-snapshot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--project", type=str, help=argparse.SUPPRESS)
    parser.add_argument(
        "--compactions",
        nargs="?",
        const="*",
        metavar="PROJECT",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    db = MemoryDB()
    transcript_parser = TranscriptParser()

    try:
        if args.dashboard:
            dashboard_tool = Path(__file__).parent / "memdashboard.py"
            subprocess.run(["uv", "run", str(dashboard_tool)], check=False)
            return

        if args.query:
            results = db.search(args.query)
            if not results:
                print(f"No facts matching '{args.query}'")
                return
            for r in results:
                print(f"  [{r['type']:10s}] [{r['project'][:30]}] {r['content'][:120]}")
            print(f"\n{len(results)} results")
            return

        if args.stats:
            s = db.stats()
            cs = db.compaction_stats()
            mem_rows = db.conn.execute("SELECT durability, COUNT(*) as c FROM memories GROUP BY durability").fetchall()
            mem_counts = {r["durability"]: r["c"] for r in mem_rows}
            durable = mem_counts.get("durable", 0)
            ephemeral = mem_counts.get("ephemeral", 0)

            top_projects = db.conn.execute(
                "SELECT project, COUNT(*) as c FROM sessions GROUP BY project ORDER BY c DESC LIMIT 5"
            ).fetchall()

            patterns_dir = Path.home() / ".claude" / "patterns" / "patterns"
            pattern_count = len(list(patterns_dir.glob("*.md"))) if patterns_dir.exists() else 0

            print("engram — what I've learned about you\n")
            print(f"  {s['sessions']:>5} sessions captured, {cs['total']} compactions processed")
            print(f"  {s['unique_files']:>5} unique files touched")
            print(f"  {durable:>5} preferences remembered (durable)")
            print(f"  {ephemeral:>5} context notes active (ephemeral)")
            print(f"  {pattern_count:>5} patterns detected in wiki")

            if top_projects:
                print("\nMost active projects:")
                for r in top_projects:
                    raw = r["project"] or "?"
                    # Claude Code project slugs use "-" as path separator; take last segment
                    proj = raw.rstrip("-").split("-")[-1][:40] or raw[:40]
                    print(f"  • {proj:40s} {r['c']} sessions")

            if s["top_tools"]:
                print("\nYour top tools:")
                for name, count in s["top_tools"][:5]:
                    print(f"  • {name:25s} {count}")
            return

        if args.recent:
            sessions = db.recent_sessions(args.recent)
            for s in sessions:
                topic = (s["topic"] or "")[:50]
                print(
                    f"  {s['captured_at'][:16]}  {s['project'][:35]:35s}  msgs={s['message_count']:3d}  facts={s['fact_count']:2d}  branch={s['branch'] or '-':15s}  {topic}"
                )
            return

        if args.inject:
            print(db.inject_context(args.inject_project))
            return

        if args.banner:
            print(db.build_banner(args.banner_project, args.banner_name))
            return

        if args.ingest_digest:
            text = sys.stdin.read()
            memories = parse_digest_output(text, project=args.project)
            for m in memories:
                db.upsert_memory(
                    m["topic"],
                    m["content"],
                    m["durability"],
                    source_session=args.session_id,
                )
            print(f"Ingested {len(memories)} memories")
            return

        if args.ingest_snapshot:
            text = sys.stdin.read().strip()
            project = args.project or "unknown"
            session_id = args.session_id
            snapshot = text if text else None
            db.save_compaction(session_id, project, snapshot)
            print(f"Compaction recorded for {project}" + (" with snapshot" if snapshot else ""))
            return

        if args.compactions is not None:
            if args.compactions == "*":
                rows = db.conn.execute(
                    "SELECT project, session_id, compacted_at, CASE WHEN snapshot IS NOT NULL THEN 'yes' ELSE 'no' END as has_snapshot FROM compactions ORDER BY compacted_at DESC LIMIT 20"
                ).fetchall()
            else:
                rows = db.conn.execute(
                    "SELECT project, session_id, compacted_at, CASE WHEN snapshot IS NOT NULL THEN 'yes' ELSE 'no' END as has_snapshot FROM compactions WHERE project LIKE ? ORDER BY compacted_at DESC LIMIT 20",
                    (f"%{args.compactions}%",),
                ).fetchall()
            if not rows:
                print("No compactions recorded")
                return
            for r in rows:
                sid = (r["session_id"] or "?")[:8]
                print(f"  {r['compacted_at'][:16]}  {r['project'][:40]:40s}  session={sid}  snapshot={r['has_snapshot']}")
            print(f"\n{len(rows)} compaction events")
            return

        if args.memories is not None:
            pattern = None if args.memories == "*" else args.memories.replace("*", "%")
            memories = db.list_memories(pattern)
            if not memories:
                print("No memories found")
                return
            for m in memories:
                dur = "D" if m["durability"] == "durable" else "E"
                print(f"  [{dur}] {m['topic']:30s} {m['content'][:80]}")
            print(
                f"\n{len(memories)} memories ({sum(1 for m in memories if m['durability'] == 'durable')} durable, {sum(1 for m in memories if m['durability'] == 'ephemeral')} ephemeral)"
            )
            return

        if args.forget:
            if args.ephemeral:
                count = db.forget_all_ephemeral()
                print(f"Deleted {count} ephemeral memories")
            else:
                if db.forget_memory(args.forget):
                    print(f"Forgot: {args.forget}")
                else:
                    print(f"No memory with topic: {args.forget}")
            return

        # Capture mode
        if args.transcript:
            transcripts = [(Path(args.transcript), Path(args.transcript).parent.name)]
        elif args.all:
            transcripts = find_transcripts()
        else:
            result = find_current_session()
            if result is None:
                print("No transcript found for current session")
                sys.exit(1)
            transcripts = [result]

        captured = 0
        skipped = 0
        for path, project in transcripts:
            session_id = path.stem
            if db.is_captured(session_id):
                skipped += 1
                continue

            session = transcript_parser.parse_file(path, project, extract_facts=args.extract_facts)
            if session is None:
                skipped += 1
                continue

            db.save_session(session)
            captured += 1
            if not args.all:
                print(
                    f"Captured: {session_id[:8]}  msgs={session.message_count}  tools={session.tool_count}  facts={len(session.facts)}  files={len(session.files)}  topic={session.topic or '-'}"
                )

        if args.all:
            print(f"Captured {captured} sessions, skipped {skipped} (already captured or trivial)")
        elif captured == 0 and skipped > 0:
            print("Session already captured")

    finally:
        db.close()


if __name__ == "__main__":
    main()
