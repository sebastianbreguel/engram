#!/usr/bin/env python3
"""mempatterns — Detect emergent patterns from memory.db and maintain an Obsidian wiki."""
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "memory.db"
WIKI_DIR = Path.home() / ".claude" / "patterns"

CO_EDIT_THRESHOLD = 5
ERROR_RECURRENCE_THRESHOLD = 3
PROJECT_STREAK_THRESHOLD = 5
TOOL_ANOMALY_FACTOR = 2.0


def _slugify(path: str, max_len: int = 80) -> str:
    """Convert a file path to a wiki-safe slug: src/auth.py → src-auth-py.

    Truncates and appends a hash suffix if the result exceeds max_len.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-")
    if len(slug) <= max_len:
        return slug
    import hashlib

    h = hashlib.md5(path.encode()).hexdigest()[:8]
    return slug[: max_len - 9] + "-" + h


class WikiWriter:
    """Writes and maintains Obsidian-compatible wiki pages for patterns."""

    def __init__(self, wiki_dir: Path = WIKI_DIR):
        self.wiki_dir = wiki_dir
        for subdir in ("entities", "patterns", "suggestions", "corrections"):
            (wiki_dir / subdir).mkdir(parents=True, exist_ok=True)

    def write_entity_page(
        self,
        filepath: str,
        sessions: int,
        co_edits: list[tuple[str, int]],
        errors: list[str],
    ) -> None:
        """Write or merge entity page for a file."""
        slug = _slugify(filepath)
        page_path = self.wiki_dir / "entities" / f"{slug}.md"
        today = str(date.today())

        # Defaults — overridden if page already exists
        first_seen = today
        existing_co_edits: dict[str, int] = {}
        existing_errors: list[str] = []

        if page_path.exists():
            content = page_path.read_text()
            # Parse first_seen
            m = re.search(r"first_seen:\s*(\S+)", content)
            if m:
                first_seen = m.group(1)
            # Parse existing co_edits: lines like "- [[slug]] — N sessions"
            for line in content.splitlines():
                cm = re.match(r"-\s+\[\[([^\]]+)\]\]\s+[—-]+\s+(\d+)\s+sessions?", line)
                if cm:
                    existing_co_edits[cm.group(1)] = int(cm.group(2))
            # Parse existing errors: lines under "## Common errors"
            in_errors = False
            for line in content.splitlines():
                if line.strip() == "## Common errors":
                    in_errors = True
                    continue
                if in_errors:
                    if line.startswith("## "):
                        break
                    if line.startswith("- "):
                        existing_errors.append(line[2:])

        # Merge co_edits
        for partner, count in co_edits:
            partner_slug = _slugify(partner)
            existing_co_edits[partner_slug] = count

        # Merge errors (deduplicate)
        all_errors = list(existing_errors)
        for e in errors:
            if e not in all_errors:
                all_errors.append(e)

        # Build page
        co_edit_lines = "\n".join(
            f"- [[{s}]] — {c} sessions" for s, c in existing_co_edits.items()
        )
        error_lines = "\n".join(f"- {e}" for e in all_errors)

        content = f"""---
type: file
first_seen: {first_seen}
last_seen: {today}
sessions: {sessions}
---

# {filepath}

## Co-edited with
{co_edit_lines}

## Common errors
{error_lines}
"""
        page_path.write_text(content)

    def write_pattern_page(
        self,
        name: str,
        kind: str,
        confidence: int,
        threshold: int,
        description: str,
        files: list[str] | None = None,
    ) -> None:
        """Write or update a pattern page, preserving history."""
        page_path = self.wiki_dir / "patterns" / f"{name}.md"
        today = str(date.today())

        first_detected = today
        history_lines: list[str] = []

        if page_path.exists():
            content = page_path.read_text()
            m = re.search(r"first_detected:\s*(\S+)", content)
            if m:
                first_detected = m.group(1)
            # Parse existing history entries
            in_history = False
            for line in content.splitlines():
                if line.strip() == "## History":
                    in_history = True
                    continue
                if in_history:
                    if line.startswith("## "):
                        break
                    if line.startswith("- "):
                        history_lines.append(line[2:])
            # Prepend new reinforcement entry
            history_lines.insert(0, f"{today}: reinforced (confidence {confidence})")
        else:
            history_lines.append(f"{first_detected}: first detected")

        files_section = ""
        if files:
            file_lines = "\n".join(f"- [[{_slugify(f)}]]" for f in files)
            files_section = f"\n## Files\n{file_lines}\n"

        history_text = "\n".join(f"- {h}" for h in history_lines)

        content = f"""---
type: pattern
kind: {kind}
confidence: {confidence}
threshold: {threshold}
first_detected: {first_detected}
last_reinforced: {today}
status: active
---

# {name}

{description}
{files_section}
## History
{history_text}
"""
        page_path.write_text(content)

    def write_index(self) -> None:
        """Write index.md with wikilinks to all entities and patterns."""
        entity_links = "\n".join(
            f"- [[{p.stem}]]" for p in sorted((self.wiki_dir / "entities").glob("*.md"))
        )
        pattern_links = "\n".join(
            f"- [[{p.stem}]]" for p in sorted((self.wiki_dir / "patterns").glob("*.md"))
        )
        content = f"""# Patterns Wiki Index

## Entities
{entity_links}

## Patterns
{pattern_links}
"""
        (self.wiki_dir / "index.md").write_text(content)


class PatternDetector:
    """Detects patterns from memory.db data."""

    def __init__(self, db_path: Path = DB_PATH, wiki_dir: Path = WIKI_DIR):
        self.db_path = db_path
        self.wiki_dir = wiki_dir
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def detect_co_edits(self, threshold: int = CO_EDIT_THRESHOLD) -> list[dict]:
        """Find file pairs frequently edited together in the same session."""
        sql = """
            SELECT a.path AS file_a, b.path AS file_b, COUNT(*) AS cnt
            FROM files_touched a
            JOIN files_touched b
                ON a.session_id = b.session_id
               AND a.path < b.path
            WHERE a.action IN ('edit', 'write', 'create')
              AND b.action IN ('edit', 'write', 'create')
            GROUP BY a.path, b.path
            HAVING cnt >= ?
        """
        rows = self.conn.execute(sql, (threshold,)).fetchall()
        return [
            {
                "files": [row["file_a"], row["file_b"]],
                "count": row["cnt"],
                "kind": "co_edit",
            }
            for row in rows
        ]

    def detect_error_recurrence(
        self, threshold: int = ERROR_RECURRENCE_THRESHOLD
    ) -> list[dict]:
        """Find errors appearing across multiple sessions."""
        sql = """
            SELECT content, content_hash, COUNT(*) AS cnt
            FROM facts
            WHERE type = 'error'
            GROUP BY content_hash
            HAVING cnt >= ?
        """
        rows = self.conn.execute(sql, (threshold,)).fetchall()
        return [
            {
                "content": row["content"],
                "hash": row["content_hash"],
                "count": row["cnt"],
                "kind": "error_recurrence",
            }
            for row in rows
        ]

    def detect_project_streaks(
        self, threshold: int = PROJECT_STREAK_THRESHOLD
    ) -> list[dict]:
        """Find consecutive days of activity per project."""
        sql = """
            SELECT project, DATE(captured_at) AS day
            FROM sessions
            GROUP BY project, day
            ORDER BY project, day
        """
        rows = self.conn.execute(sql).fetchall()

        # Group dates by project
        project_days: dict[str, list[date]] = defaultdict(list)
        for row in rows:
            project_days[row["project"]].append(date.fromisoformat(row["day"]))

        results = []
        for project, days in project_days.items():
            days_sorted = sorted(set(days))
            # Find longest consecutive run
            max_streak = 1
            current_streak = 1
            for i in range(1, len(days_sorted)):
                if days_sorted[i] - days_sorted[i - 1] == timedelta(days=1):
                    current_streak += 1
                    max_streak = max(max_streak, current_streak)
                else:
                    current_streak = 1
            if max_streak >= threshold:
                results.append(
                    {"project": project, "streak": max_streak, "kind": "project_streak"}
                )
        return results

    def detect_tool_anomalies(self, factor: float = TOOL_ANOMALY_FACTOR) -> list[dict]:
        """Find projects with unusual tool usage compared to global average."""
        sql = """
            SELECT s.project, tu.tool_name, AVG(tu.count) AS proj_avg
            FROM tool_usage tu
            JOIN sessions s ON tu.session_id = s.session_id
            GROUP BY s.project, tu.tool_name
        """
        proj_rows = self.conn.execute(sql).fetchall()

        global_sql = """
            SELECT tu.tool_name, AVG(tu.count) AS global_avg
            FROM tool_usage tu
            GROUP BY tu.tool_name
        """
        global_rows = self.conn.execute(global_sql).fetchall()
        global_avgs = {row["tool_name"]: row["global_avg"] for row in global_rows}

        results = []
        for row in proj_rows:
            g_avg = global_avgs.get(row["tool_name"], 0)
            if g_avg == 0:
                continue
            ratio = row["proj_avg"] / g_avg
            if ratio > factor:
                results.append(
                    {
                        "project": row["project"],
                        "tool": row["tool_name"],
                        "project_avg": row["proj_avg"],
                        "global_avg": g_avg,
                        "ratio": ratio,
                        "kind": "tool_anomaly",
                    }
                )
        return results


STALE_DAYS = 30
DELETE_DAYS = 60
SUGGESTION_FACTOR = 2


class PatternsOrchestrator:
    """Orchestrates detection, wiki maintenance, and lifecycle of patterns."""

    def __init__(self, db_path: Path = DB_PATH, wiki_dir: Path = WIKI_DIR):
        self.db_path = db_path
        self.wiki_dir = wiki_dir
        self.meta_path = self.wiki_dir / ".meta.json"
        self.detector = PatternDetector(db_path=db_path, wiki_dir=wiki_dir)
        self.writer = WikiWriter(wiki_dir=wiki_dir)
        self.meta = self._load_meta()

    def _load_meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text())
        return {"total_runs": 0, "total_pruned": 0, "last_run": None}

    def _save_meta(self) -> None:
        self.meta_path.write_text(json.dumps(self.meta, indent=2))

    def _pattern_name(self, pattern: dict) -> str:
        kind = pattern["kind"]
        if kind == "co_edit":
            files = sorted(pattern["files"])
            name = "co-edit-" + "-".join(_slugify(f) for f in files)
            if len(name) > 200:
                import hashlib

                h = hashlib.md5("-".join(files).encode()).hexdigest()[:12]
                name = name[:187] + "-" + h
            return name
        if kind == "error_recurrence":
            return "error-" + pattern["hash"][:12]
        if kind == "project_streak":
            return "streak-" + _slugify(pattern["project"])
        if kind == "tool_anomaly":
            return (
                "tool-anomaly-"
                + _slugify(pattern["project"])
                + "-"
                + _slugify(pattern["tool"])
            )
        return _slugify(str(pattern))

    def _pattern_description(self, pattern: dict) -> str:
        kind = pattern["kind"]
        if kind == "co_edit":
            return f"Files {pattern['files'][0]} and {pattern['files'][1]} are edited together frequently ({pattern['count']} sessions)."
        if kind == "error_recurrence":
            return f"Error recurs across sessions ({pattern['count']} times): {pattern['content']}"
        if kind == "project_streak":
            return f"Project {pattern['project']} had a streak of {pattern['streak']} consecutive active days."
        if kind == "tool_anomaly":
            return (
                f"Project {pattern['project']} uses {pattern['tool']} at {pattern['ratio']:.1f}x the global average"
                f" ({pattern['project_avg']:.1f} vs {pattern['global_avg']:.1f})."
            )
        return str(pattern)

    def _pattern_confidence_and_threshold(self, pattern: dict) -> tuple[int, int]:
        kind = pattern["kind"]
        if kind == "co_edit":
            return pattern["count"], CO_EDIT_THRESHOLD
        if kind == "error_recurrence":
            return pattern["count"], ERROR_RECURRENCE_THRESHOLD
        if kind == "project_streak":
            return pattern["streak"], PROJECT_STREAK_THRESHOLD
        if kind == "tool_anomaly":
            return int(pattern["ratio"] * 10), int(TOOL_ANOMALY_FACTOR * 10)
        return 0, 1

    def update(self) -> list[dict]:
        """Run all detectors, update wiki, prune stale, return NEW patterns only."""
        all_patterns: list[dict] = []
        with self.detector:
            all_patterns.extend(self.detector.detect_co_edits())
            all_patterns.extend(self.detector.detect_error_recurrence())
            all_patterns.extend(self.detector.detect_project_streaks())
            all_patterns.extend(self.detector.detect_tool_anomalies())

        # Re-open detector for next calls (context manager closed it)
        self.detector = PatternDetector(db_path=self.db_path, wiki_dir=self.wiki_dir)

        patterns_dir = self.wiki_dir / "patterns"
        existing_names = {p.stem for p in patterns_dir.glob("*.md")}

        new_patterns = []
        for pattern in all_patterns:
            name = self._pattern_name(pattern)
            confidence, threshold = self._pattern_confidence_and_threshold(pattern)
            description = self._pattern_description(pattern)
            files = pattern.get("files")
            self.writer.write_pattern_page(
                name=name,
                kind=pattern["kind"],
                confidence=confidence,
                threshold=threshold,
                description=description,
                files=files,
            )
            if name not in existing_names:
                new_patterns.append(pattern)

            # Write entity pages for co-edits
            if pattern["kind"] == "co_edit":
                for f in pattern["files"]:
                    partners = [
                        (other, pattern["count"])
                        for other in pattern["files"]
                        if other != f
                    ]
                    self.writer.write_entity_page(
                        f, sessions=pattern["count"], co_edits=partners, errors=[]
                    )

        self._prune_stale()
        self._write_suggestions()
        self.writer.write_index()

        self.meta["total_runs"] += 1
        self.meta["last_run"] = str(date.today())
        self._save_meta()

        return new_patterns

    def _prune_stale(self) -> None:
        """Mark old patterns stale (>30 days), delete very old (>60 days stale)."""
        today = date.today()
        for pf in (self.wiki_dir / "patterns").glob("*.md"):
            content = pf.read_text()
            m = re.search(r"last_reinforced:\s*(\S+)", content)
            if not m:
                continue
            last_reinforced = date.fromisoformat(m.group(1))
            age = (today - last_reinforced).days

            status_m = re.search(r"status:\s*(\S+)", content)
            current_status = status_m.group(1) if status_m else "active"

            if current_status == "stale" and age > DELETE_DAYS:
                pf.unlink()
                self.meta["total_pruned"] = self.meta.get("total_pruned", 0) + 1
            elif current_status != "stale" and age > STALE_DAYS:
                updated = re.sub(r"status:\s*\S+", "status: stale", content)
                pf.write_text(updated)

    def _write_suggestions(self) -> None:
        """Write suggestions/pending.md for patterns with confidence > threshold * SUGGESTION_FACTOR."""
        high_confidence = []
        for pf in (self.wiki_dir / "patterns").glob("*.md"):
            content = pf.read_text()
            conf_m = re.search(r"^confidence:\s*(\d+)", content, re.MULTILINE)
            thresh_m = re.search(r"^threshold:\s*(\d+)", content, re.MULTILINE)
            kind_m = re.search(r"^kind:\s*(\S+)", content, re.MULTILINE)
            if conf_m and thresh_m:
                conf = int(conf_m.group(1))
                thresh = int(thresh_m.group(1))
                if conf > thresh * SUGGESTION_FACTOR:
                    kind = kind_m.group(1) if kind_m else "unknown"
                    high_confidence.append(
                        {"name": pf.stem, "confidence": conf, "kind": kind}
                    )

        if not high_confidence:
            return

        lines = ["# Suggested Actions\n", f"Generated: {date.today()}\n\n"]
        for p in sorted(high_confidence, key=lambda x: -x["confidence"]):
            lines.append(
                f"- **{p['name']}** ({p['kind']}) — confidence {p['confidence']}\n"
            )

        pending = self.wiki_dir / "suggestions" / "pending.md"
        pending.write_text("".join(lines))

    def forget(self, name: str) -> bool:
        """Delete a pattern page, update index. Return True if found."""
        pf = self.wiki_dir / "patterns" / f"{name}.md"
        if not pf.exists():
            return False
        pf.unlink()
        self.writer.write_index()
        return True

    def report(self) -> str:
        """Formatted report of all patterns ranked by confidence."""
        entries = []
        for pf in (self.wiki_dir / "patterns").glob("*.md"):
            content = pf.read_text()
            conf_m = re.search(r"^confidence:\s*(\d+)", content, re.MULTILINE)
            kind_m = re.search(r"^kind:\s*(\S+)", content, re.MULTILINE)
            status_m = re.search(r"^status:\s*(\S+)", content, re.MULTILINE)
            conf = int(conf_m.group(1)) if conf_m else 0
            kind = kind_m.group(1) if kind_m else "unknown"
            status = status_m.group(1) if status_m else "active"
            entries.append((conf, pf.stem, kind, status))

        entries.sort(key=lambda x: -x[0])
        lines = ["# Pattern Report\n"]
        for conf, name, kind, status in entries:
            lines.append(f"- [{status}] {name}  kind={kind}  confidence={conf}")
        return "\n".join(lines)

    def status(self) -> str:
        """Wiki stats: entity count, pattern count, runs, pruned."""
        entities = len(list((self.wiki_dir / "entities").glob("*.md")))
        patterns = list((self.wiki_dir / "patterns").glob("*.md"))
        active = sum(1 for p in patterns if "status: active" in p.read_text())
        stale = len(patterns) - active
        runs = self.meta.get("total_runs", 0)
        pruned = self.meta.get("total_pruned", 0)
        return (
            f"Patterns wiki: {entities} entities | {len(patterns)} patterns ({active} active, {stale} stale)"
            f" | {runs} runs | {pruned} pruned"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="mempatterns — detect patterns from memory.db"
    )
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--suggest", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--forget", type=str, metavar="NAME")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--db-path", type=Path, default=DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument(
        "--wiki-dir", type=Path, default=WIKI_DIR, help=argparse.SUPPRESS
    )
    args = parser.parse_args()

    orch = PatternsOrchestrator(db_path=args.db_path, wiki_dir=args.wiki_dir)

    if args.update or args.rebuild:
        new = orch.update()
        if new:
            print(f"New patterns detected: {len(new)}")
            for p in new:
                name = orch._pattern_name(p)
                print(f"  + {name} ({p['kind']})")

    if args.report:
        print(orch.report())

    if args.suggest:
        pending = args.wiki_dir / "suggestions" / "pending.md"
        if pending.exists():
            print(pending.read_text())
        else:
            print("No suggestions.")

    if args.status:
        print(orch.status())

    if args.forget:
        found = orch.forget(args.forget)
        print(f"Deleted: {args.forget}" if found else f"Not found: {args.forget}")


if __name__ == "__main__":
    main()
