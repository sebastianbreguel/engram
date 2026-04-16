"""Tests for mempatterns.py — PatternDetector and WikiWriter."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from mempatterns import PatternDetector, PatternsOrchestrator, WikiWriter, _slugify


@pytest.fixture
def tmp_db(tmp_path):
    """Create a memory.db with schema."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
            project TEXT NOT NULL, cwd TEXT, branch TEXT, topic TEXT,
            message_count INTEGER DEFAULT 0, tool_count INTEGER DEFAULT 0,
            captured_at TEXT NOT NULL, transcript_path TEXT
        );
        CREATE TABLE files_touched (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
            path TEXT NOT NULL, action TEXT NOT NULL, count INTEGER DEFAULT 1
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
            type TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
            source_line INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE tool_usage (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL, count INTEGER DEFAULT 1,
            UNIQUE(session_id, tool_name)
        );
    """)
    conn.commit()
    return db_path, conn


@pytest.fixture
def wiki_dir(tmp_path):
    return tmp_path / "patterns"


# ---------------------------------------------------------------------------
# detect_co_edits
# ---------------------------------------------------------------------------


def test_co_edits_above_threshold(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for i in range(5):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            (i * 2, sid, "a.py", "edit", 1),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            (i * 2 + 1, sid, "b.py", "write", 1),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_co_edits(threshold=5)

    assert len(results) == 1
    assert set(results[0]["files"]) == {"a.py", "b.py"}
    assert results[0]["count"] == 5
    assert results[0]["kind"] == "co_edit"


def test_co_edits_below_threshold_ignored(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for i in range(3):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            (i * 2, sid, "a.py", "edit", 1),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            (i * 2 + 1, sid, "b.py", "edit", 1),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_co_edits(threshold=5)

    assert results == []


def test_co_edits_readonly_actions_ignored(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for i in range(6):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            (i * 2, sid, "a.py", "read", 1),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            (i * 2 + 1, sid, "b.py", "read", 1),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_co_edits(threshold=5)

    assert results == []


# ---------------------------------------------------------------------------
# detect_error_recurrence
# ---------------------------------------------------------------------------


def test_error_recurrence_detected(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for i in range(3):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO facts VALUES (?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "error",
                "TypeError: NoneType",
                "hash-abc",
                None,
                f"2024-01-0{i + 1}T10:00:00",
            ),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_error_recurrence(threshold=3)

    assert len(results) == 1
    assert results[0]["content"] == "TypeError: NoneType"
    assert results[0]["hash"] == "hash-abc"
    assert results[0]["count"] == 3
    assert results[0]["kind"] == "error_recurrence"


def test_error_recurrence_below_threshold_ignored(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for i in range(2):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO facts VALUES (?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "error",
                "TypeError: NoneType",
                "hash-abc",
                None,
                f"2024-01-0{i + 1}T10:00:00",
            ),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_error_recurrence(threshold=3)

    assert results == []


# ---------------------------------------------------------------------------
# detect_project_streaks
# ---------------------------------------------------------------------------


def test_project_streaks_detected(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    project = "myproject"
    for i in range(5):
        sid = f"sess-{i}"
        day = f"2024-01-{i + 1:02d}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, sid, project, None, None, None, 0, 0, f"{day}T10:00:00", None),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_project_streaks(threshold=5)

    assert len(results) == 1
    assert results[0]["project"] == project
    assert results[0]["streak"] == 5
    assert results[0]["kind"] == "project_streak"


def test_project_streaks_gap_breaks_streak(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    project = "myproject"
    # Days 1,2,3 — gap — 5,6,7,8,9  (streak of 3 then 5)
    days = [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-05",
        "2024-01-06",
        "2024-01-07",
        "2024-01-08",
        "2024-01-09",
    ]
    for i, day in enumerate(days):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, sid, project, None, None, None, 0, 0, f"{day}T10:00:00", None),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_project_streaks(threshold=5)

    assert len(results) == 1
    assert results[0]["streak"] == 5


def test_project_streaks_below_threshold_ignored(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for i in range(3):
        sid = f"sess-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_project_streaks(threshold=5)

    assert results == []


# ---------------------------------------------------------------------------
# detect_tool_anomalies
# ---------------------------------------------------------------------------


def test_tool_anomalies_detected(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    # Project A: high Bash usage (avg 100)
    for i in range(3):
        sid = f"sess-a-{i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                sid,
                "proj-a",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-0{i + 1}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO tool_usage VALUES (?,?,?,?)", (i * 2, sid, "Bash", 100)
        )
    # Projects B, C, D: low Bash usage (avg 1) — global avg = (100+1+1+1)/4 = 25.75, ratio proj-a = 100/25.75 > 2
    for proj_idx, proj in enumerate(["proj-b", "proj-c", "proj-d"]):
        for i in range(3):
            sid = f"sess-{proj}-{i}"
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    10 + proj_idx * 10 + i,
                    sid,
                    proj,
                    None,
                    None,
                    None,
                    0,
                    0,
                    f"2024-01-0{i + 1}T10:00:00",
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO tool_usage VALUES (?,?,?,?)",
                (50 + proj_idx * 10 + i * 2, sid, "Bash", 1),
            )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_tool_anomalies(factor=2.0)

    projects = [r["project"] for r in results]
    assert "proj-a" in projects
    anomaly = next(r for r in results if r["project"] == "proj-a")
    assert anomaly["tool"] == "Bash"
    assert anomaly["ratio"] > 2.0
    assert anomaly["kind"] == "tool_anomaly"


def test_tool_anomalies_similar_usage_not_flagged(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    for proj_idx, proj in enumerate(["proj-a", "proj-b", "proj-c"]):
        for i in range(3):
            sid = f"sess-{proj_idx}-{i}"
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    proj_idx * 10 + i,
                    sid,
                    proj,
                    None,
                    None,
                    None,
                    0,
                    0,
                    f"2024-01-0{i + 1}T10:00:00",
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO tool_usage VALUES (?,?,?,?)",
                (proj_idx * 10 + i + 1, sid, "Bash", 10),
            )
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        results = pd.detect_tool_anomalies(factor=2.0)

    assert results == []


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_closes_connection(tmp_db, wiki_dir):
    db_path, conn = tmp_db
    conn.commit()

    with PatternDetector(db_path=db_path, wiki_dir=wiki_dir) as pd:
        internal_conn = pd.conn

    # After exit, connection should be closed — cursor ops should fail
    with pytest.raises(Exception):
        internal_conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


def test_slugify_simple_path():
    assert _slugify("src/auth.py") == "src-auth-py"


def test_slugify_dots_and_slashes():
    assert _slugify("src/models/user.py") == "src-models-user-py"


def test_slugify_spaces():
    assert _slugify("my file.py") == "my-file-py"


def test_slugify_no_path_separators():
    assert _slugify("auth.py") == "auth-py"


# ---------------------------------------------------------------------------
# WikiWriter — entity pages
# ---------------------------------------------------------------------------


def test_entity_page_creation(wiki_dir):
    w = WikiWriter(wiki_dir=wiki_dir)
    w.write_entity_page(
        "src/auth.py",
        sessions=34,
        co_edits=[("src/middleware.py", 12), ("src/models/user.py", 5)],
        errors=["ImportError: cannot import 'TokenStore' — 3 occurrences"],
    )
    page = (wiki_dir / "entities" / "src-auth-py.md").read_text()
    assert "sessions: 34" in page
    assert "[[src-middleware-py]]" in page
    assert "[[src-models-user-py]]" in page
    assert "ImportError" in page


def test_entity_page_merge(wiki_dir):
    """Calling write_entity_page twice must merge co_edits, not overwrite."""
    w = WikiWriter(wiki_dir=wiki_dir)
    w.write_entity_page(
        "src/auth.py",
        sessions=10,
        co_edits=[("src/middleware.py", 12)],
        errors=[],
    )
    w.write_entity_page(
        "src/auth.py",
        sessions=20,
        co_edits=[("src/models.py", 5)],
        errors=["TypeError: foo"],
    )
    page = (wiki_dir / "entities" / "src-auth-py.md").read_text()
    assert "[[src-middleware-py]]" in page
    assert "[[src-models-py]]" in page
    assert "TypeError: foo" in page
    assert "sessions: 20" in page


def test_entity_page_preserves_first_seen(wiki_dir):
    w = WikiWriter(wiki_dir=wiki_dir)
    w.write_entity_page("src/auth.py", sessions=1, co_edits=[], errors=[])
    first_content = (wiki_dir / "entities" / "src-auth-py.md").read_text()
    # Extract first_seen line
    first_seen_line = next(
        line for line in first_content.splitlines() if "first_seen" in line
    )

    w.write_entity_page("src/auth.py", sessions=2, co_edits=[], errors=[])
    second_content = (wiki_dir / "entities" / "src-auth-py.md").read_text()
    assert first_seen_line in second_content


# ---------------------------------------------------------------------------
# WikiWriter — pattern pages
# ---------------------------------------------------------------------------


def test_pattern_page_creation(wiki_dir):
    w = WikiWriter(wiki_dir=wiki_dir)
    w.write_pattern_page(
        name="auth-middleware-pair",
        kind="co-edit",
        confidence=12,
        threshold=5,
        description="auth and middleware edited together.",
        files=["src/auth.py"],
    )
    page = (wiki_dir / "patterns" / "auth-middleware-pair.md").read_text()
    assert "kind: co-edit" in page
    assert "confidence: 12" in page
    assert "[[src-auth-py]]" in page
    assert "first detected" in page


def test_pattern_page_update_preserves_first_detected_and_history(wiki_dir):
    w = WikiWriter(wiki_dir=wiki_dir)
    w.write_pattern_page(
        name="auth-middleware-pair",
        kind="co-edit",
        confidence=5,
        threshold=5,
        description="desc",
    )
    first_content = (wiki_dir / "patterns" / "auth-middleware-pair.md").read_text()
    first_detected = next(
        line for line in first_content.splitlines() if "first_detected" in line
    )

    w.write_pattern_page(
        name="auth-middleware-pair",
        kind="co-edit",
        confidence=12,
        threshold=5,
        description="desc updated",
    )
    second_content = (wiki_dir / "patterns" / "auth-middleware-pair.md").read_text()
    assert first_detected in second_content
    # Should have two history entries
    assert second_content.count("reinforced") >= 1
    assert "first detected" in second_content


# ---------------------------------------------------------------------------
# WikiWriter — index
# ---------------------------------------------------------------------------


def test_write_index(wiki_dir):
    w = WikiWriter(wiki_dir=wiki_dir)
    w.write_entity_page("src/auth.py", sessions=5, co_edits=[], errors=[])
    w.write_pattern_page(
        name="auth-middleware-pair",
        kind="co-edit",
        confidence=5,
        threshold=5,
        description="desc",
    )
    w.write_index()
    index = (wiki_dir / "index.md").read_text()
    assert "[[src-auth-py]]" in index
    assert "[[auth-middleware-pair]]" in index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_co_edit_sessions(
    conn, n: int, file_a: str = "a.py", file_b: str = "b.py", start_id: int = 0
):
    for i in range(n):
        sid = f"sess-coedit-{start_id + i}"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                start_id + i,
                sid,
                "proj",
                None,
                None,
                None,
                0,
                0,
                f"2024-01-{(i % 28) + 1:02d}T10:00:00",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            ((start_id + i) * 2, sid, file_a, "edit", 1),
        )
        conn.execute(
            "INSERT INTO files_touched VALUES (?,?,?,?,?)",
            ((start_id + i) * 2 + 1, sid, file_b, "edit", 1),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# TestOrchestrator
# ---------------------------------------------------------------------------


class TestOrchestrator:
    def test_full_update_creates_wiki(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        new_patterns = orch.update()
        assert len(new_patterns) >= 1
        assert (wiki_dir / "index.md").exists()
        assert any((wiki_dir / "patterns").glob("*.md"))

    def test_meta_tracks_runs(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        orch2 = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch2.update()
        assert orch2.meta["total_runs"] == 2

    def test_stale_patterns_marked(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        # Manually backdate a pattern page to 35 days ago
        old_date = str(date.today() - timedelta(days=35))
        pattern_files = list((wiki_dir / "patterns").glob("*.md"))
        assert pattern_files, "No pattern files created"
        pf = pattern_files[0]
        content = pf.read_text()
        content = content.replace(
            f"last_reinforced: {date.today()}", f"last_reinforced: {old_date}"
        )
        pf.write_text(content)
        # Run update again (no new data — same DB)
        orch2 = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch2._prune_stale()
        updated = pf.read_text()
        assert "status: stale" in updated

    def test_stale_patterns_deleted(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        pattern_files = list((wiki_dir / "patterns").glob("*.md"))
        assert pattern_files
        pf = pattern_files[0]
        old_date = str(date.today() - timedelta(days=61))
        content = pf.read_text()
        content = content.replace("status: active", "status: stale")
        content = content.replace(
            f"last_reinforced: {date.today()}", f"last_reinforced: {old_date}"
        )
        pf.write_text(content)
        orch2 = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch2._prune_stale()
        assert not pf.exists()

    def test_returns_only_new_patterns(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        first = orch.update()
        assert len(first) >= 1
        orch2 = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        second = orch2.update()
        assert second == []

    def test_suggestions_written(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        # Insert 10 co-edits (threshold=5, SUGGESTION_FACTOR=2 → need confidence > 10)
        _insert_co_edit_sessions(conn, 11)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        pending = wiki_dir / "suggestions" / "pending.md"
        assert pending.exists()
        assert pending.read_text().strip() != ""

    def test_forget_deletes_pattern(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        pattern_files = list((wiki_dir / "patterns").glob("*.md"))
        assert pattern_files
        name = pattern_files[0].stem
        result = orch.forget(name)
        assert result is True
        assert not (wiki_dir / "patterns" / f"{name}.md").exists()

    def test_forget_returns_false_for_missing(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        assert orch.forget("nonexistent-pattern") is False

    def test_report_formats_correctly(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        report = orch.report()
        assert "co_edit" in report or "co-edit" in report
        assert any(c.isdigit() for c in report)

    def test_status_shows_counts(self, tmp_db, wiki_dir):
        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        orch = PatternsOrchestrator(db_path=db_path, wiki_dir=wiki_dir)
        orch.update()
        status = orch.status()
        assert "pattern" in status.lower()
        assert "run" in status.lower()


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_status_flag(self, tmp_db, wiki_dir, capsys):
        import sys

        from mempatterns import main

        db_path, conn = tmp_db
        _insert_co_edit_sessions(conn, 5)
        sys.argv = [
            "mempatterns",
            "--status",
            "--db-path",
            str(db_path),
            "--wiki-dir",
            str(wiki_dir),
        ]
        main()
        captured = capsys.readouterr()
        assert "pattern" in captured.out.lower() or "run" in captured.out.lower()
