"""Tests for memdoctor.py — conversational signal detectors.

Fixtures ported from millionco/claude-doctor (MIT).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from memdoctor import (
    _session_meta,
    count_restart_clusters,
    detect_correction_heavy,
    detect_error_loop,
    detect_keep_going,
    detect_rapid_corrections,
    detect_signals,
    enrich_from_memory,
    extract_error_context,
    format_rules,
    normalize_error,
    parse_jsonl,
    signals_for_executive,
)

FIXTURES = Path(__file__).parent / "fixtures" / "doctor"


@pytest.fixture
def correction_heavy_session() -> list[dict]:
    return parse_jsonl(FIXTURES / "correction-heavy-session.jsonl")


@pytest.fixture
def error_loop_session() -> list[dict]:
    return parse_jsonl(FIXTURES / "error-loop-session.jsonl")


@pytest.fixture
def keep_going_session() -> list[dict]:
    return parse_jsonl(FIXTURES / "keep-going-session.jsonl")


@pytest.fixture
def happy_session() -> list[dict]:
    return parse_jsonl(FIXTURES / "happy-session.jsonl")


class TestParseJsonl:
    def test_returns_list_of_events(self, correction_heavy_session):
        assert isinstance(correction_heavy_session, list)
        assert len(correction_heavy_session) > 0

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"type":"user"}\nnot json\n{"type":"assistant"}\n')
        events = parse_jsonl(path)
        assert len(events) == 2

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_jsonl(tmp_path / "nope.jsonl") == []


class TestCorrectionHeavy:
    def test_detects_correction_heavy_session(self, correction_heavy_session):
        assert detect_correction_heavy(correction_heavy_session) == "correction-heavy"

    def test_ignores_happy_session(self, happy_session):
        assert detect_correction_heavy(happy_session) is None

    def test_ignores_keep_going_session(self, keep_going_session):
        assert detect_correction_heavy(keep_going_session) is None


class TestErrorLoop:
    def test_detects_error_loop_session(self, error_loop_session):
        assert detect_error_loop(error_loop_session) == "error-loop"

    def test_ignores_happy_session(self, happy_session):
        assert detect_error_loop(happy_session) is None

    def test_ignores_correction_heavy_session(self, correction_heavy_session):
        assert detect_error_loop(correction_heavy_session) is None

    def test_needs_consecutive_failures(self, tmp_path):
        path = tmp_path / "intermittent.jsonl"
        path.write_text(
            '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true}]}}\n'
            '{"type":"user","message":{"content":[{"type":"tool_result","is_error":false}]}}\n'
            '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true}]}}\n'
            '{"type":"user","message":{"content":[{"type":"tool_result","is_error":false}]}}\n'
            '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true}]}}\n'
        )
        assert detect_error_loop(parse_jsonl(path)) is None


class TestKeepGoing:
    def test_detects_keep_going_session(self, keep_going_session):
        assert detect_keep_going(keep_going_session) == "keep-going-loop"

    def test_ignores_happy_session(self, happy_session):
        assert detect_keep_going(happy_session) is None

    def test_single_keep_going_not_enough(self, tmp_path):
        path = tmp_path / "one.jsonl"
        path.write_text('{"type":"user","message":{"content":"keep going"}}\n{"type":"user","message":{"content":"now do X"}}\n')
        assert detect_keep_going(parse_jsonl(path)) is None


class TestDetectSignals:
    def test_aggregates_all_detectors(self, correction_heavy_session):
        signals = detect_signals(correction_heavy_session)
        assert "correction-heavy" in signals

    def test_empty_for_happy_session(self, happy_session):
        assert detect_signals(happy_session) == []

    def test_error_loop_fixture_flagged(self, error_loop_session):
        assert "error-loop" in detect_signals(error_loop_session)


class TestFormatRules:
    def test_returns_markdown_bullets_for_detected_signals(self):
        result = format_rules({"correction-heavy", "error-loop"})
        assert result.count("- ") == 2
        assert "approach" in result.lower()
        assert "re-read" in result.lower()

    def test_empty_for_no_signals(self):
        assert format_rules(set()) == ""

    def test_ignores_unknown_signals(self):
        result = format_rules({"does-not-exist"})
        assert result == ""


class TestExtractErrorContext:
    def test_extracts_last_error_from_fixture(self, error_loop_session):
        err = extract_error_context(error_loop_session)
        assert err is not None
        assert "Connection refused" in err

    def test_returns_none_when_no_errors(self, happy_session):
        assert extract_error_context(happy_session) is None

    def test_extracts_text_blocks_from_list_content(self):
        events = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "BOOM"}]},
                    ]
                },
            }
        ]
        assert extract_error_context(events) == "BOOM"


class TestNormalizeError:
    def test_strips_absolute_paths(self):
        text = "ModuleNotFoundError at /Users/foo/bar/main.py line 42"
        assert "/Users/foo" not in normalize_error(text)
        assert "<path>" in normalize_error(text)

    def test_skips_leading_file_lines(self):
        text = 'File "/Users/a/b.py", line 10\nActualError: boom'
        assert normalize_error(text).startswith("ActualError")

    def test_caps_at_200_chars(self):
        assert len(normalize_error("x" * 500)) == 200


class TestEnrichFromMemory:
    @pytest.fixture
    def seeded_db(self, tmp_path):
        import sqlite3

        path = tmp_path / "memory.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project TEXT);
            CREATE TABLE facts (session_id TEXT, type TEXT, content TEXT, content_hash TEXT, source_line INTEGER);
            CREATE VIRTUAL TABLE facts_fts USING fts5(content, type, project, tokenize='unicode61');
            INSERT INTO sessions VALUES ('s1', 'datascience');
            INSERT INTO sessions VALUES ('s2', 'ml-exp');
            INSERT INTO facts VALUES ('s1', 'error', 'ModuleNotFoundError: No module named numpy', 'h1', 1);
            INSERT INTO facts VALUES ('s2', 'error', 'ModuleNotFoundError: No module named numpy', 'h2', 1);
            INSERT INTO facts_fts VALUES ('ModuleNotFoundError: No module named numpy', 'error', 'datascience');
            INSERT INTO facts_fts VALUES ('ModuleNotFoundError: No module named numpy', 'error', 'ml-exp');
        """)
        conn.commit()
        conn.close()
        return path

    def test_finds_prior_error_via_fts(self, seeded_db):
        result = enrich_from_memory("ModuleNotFoundError: No module named numpy", db_path=seeded_db)
        assert result is not None
        assert result["count"] == 2
        assert "datascience" in result["projects"]

    def test_returns_none_when_db_missing(self, tmp_path):
        assert enrich_from_memory("anything", db_path=tmp_path / "nope.db") is None

    def test_returns_none_when_no_match(self, seeded_db):
        assert enrich_from_memory("TotallyUnrelatedZzzError xxxxx", db_path=seeded_db) is None


class TestRapidCorrections:
    @staticmethod
    def _mk(path: Path, entries: list[tuple[str, str]]) -> list[dict]:
        """entries = [(text, iso-timestamp), ...] — write JSONL and parse."""
        lines = [json.dumps({"type": "user", "timestamp": ts, "message": {"content": text}}) for text, ts in entries]
        path.write_text("\n".join(lines) + "\n")
        return parse_jsonl(path)

    def test_flags_when_two_corrections_within_60s(self, tmp_path):
        events = self._mk(
            tmp_path / "a.jsonl",
            [
                ("no, wrong", "2026-04-15T10:00:00.000Z"),
                ("stop that", "2026-04-15T10:00:30.000Z"),
            ],
        )
        assert detect_rapid_corrections(events) == "rapid-corrections"

    def test_ignores_slow_corrections(self, tmp_path):
        events = self._mk(
            tmp_path / "b.jsonl",
            [
                ("no, wrong", "2026-04-15T10:00:00.000Z"),
                ("stop that", "2026-04-15T10:10:00.000Z"),
            ],
        )
        assert detect_rapid_corrections(events) is None

    def test_ignores_single_correction(self, tmp_path):
        events = self._mk(
            tmp_path / "c.jsonl",
            [
                ("no, wrong", "2026-04-15T10:00:00.000Z"),
                ("ok thanks", "2026-04-15T10:00:10.000Z"),
            ],
        )
        assert detect_rapid_corrections(events) is None

    def test_ignores_happy_session(self, happy_session):
        assert detect_rapid_corrections(happy_session) is None


class TestRestartClusters:
    def test_counts_single_cluster(self):
        base = datetime(2026, 4, 15, 10, 0, 0)
        starts = [base, base + timedelta(minutes=5), base + timedelta(minutes=10)]
        assert count_restart_clusters(starts) == 1

    def test_non_overlapping_windows(self):
        base = datetime(2026, 4, 15, 10, 0, 0)
        starts = [
            base,
            base + timedelta(minutes=5),
            base + timedelta(minutes=10),  # cluster 1
            base + timedelta(minutes=60),
            base + timedelta(minutes=65),
            base + timedelta(minutes=70),  # cluster 2 (outside first window)
        ]
        assert count_restart_clusters(starts) == 2

    def test_gap_too_large(self):
        base = datetime(2026, 4, 15, 10, 0, 0)
        starts = [base, base + timedelta(minutes=45), base + timedelta(minutes=90)]
        assert count_restart_clusters(starts) == 0

    def test_below_minimum_size(self):
        base = datetime(2026, 4, 15, 10, 0, 0)
        assert count_restart_clusters([base, base + timedelta(minutes=1)]) == 0

    def test_empty_list(self):
        assert count_restart_clusters([]) == 0

    def test_session_meta_counts_only_real_user_msgs(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            '{"type":"user","timestamp":"2026-04-15T10:00:00.000Z","message":{"content":"real message"}}\n'
            '{"type":"user","isMeta":true,"timestamp":"2026-04-15T10:00:05.000Z","message":{"content":"meta"}}\n'
            '{"type":"user","timestamp":"2026-04-15T10:00:10.000Z","message":{"content":"<system-reminder>skip</system-reminder>"}}\n'
            '{"type":"assistant","timestamp":"2026-04-15T10:00:15.000Z","message":{"content":"reply"}}\n'
            '{"type":"user","timestamp":"2026-04-15T10:00:20.000Z","message":{"content":"another real"}}\n'
        )
        first_ts, n_user = _session_meta(parse_jsonl(path))
        assert first_ts is not None
        assert n_user == 2


class TestPerProjectRules:
    def test_per_project_rules_output(self, capsys):
        from memdoctor import _print_rules_per_project

        report = {
            "sessions": 10,
            "projects": {
                "/proj/a": {"error-loop": 5, "correction-heavy": 3},
                "/proj/b": {"keep-going-loop": 2},
                "/proj/c": {"error-loop": 1},  # below threshold, skip
            },
            "totals": {},
        }
        _print_rules_per_project(report)
        out = capsys.readouterr().out
        assert "/proj/a" in out
        assert "/proj/b" in out
        assert "/proj/c" not in out  # 1 < MIN_CORRECTIONS_TO_FLAG
        # /proj/a comes first (higher total)
        assert out.index("/proj/a") < out.index("/proj/b")

    def test_per_project_empty_report(self, capsys):
        from memdoctor import _print_rules_per_project

        _print_rules_per_project({"sessions": 0, "projects": {}, "totals": {}})
        assert "No per-project rules" in capsys.readouterr().out


class TestSignalsForExecutive:
    def test_empty_filter_returns_empty(self):
        assert signals_for_executive("") == ""

    def test_nonmatching_project_returns_empty(self):
        """A cwd that doesn't match any session path yields no signals."""
        assert signals_for_executive("/nonexistent/path/xyz") == ""

    def test_formats_lines_when_signals_present(self, monkeypatch):
        """When _analyze reports signals ≥ threshold, output is formatted bullet lines."""
        import memdoctor

        def fake_analyze(project_filter=None):
            return {
                "sessions": 5,
                "projects": {project_filter: {"error-loop": 3, "rapid-corrections": 2}},
                "totals": {"error-loop": 3, "rapid-corrections": 2},
                "error_samples": [],
            }

        monkeypatch.setattr(memdoctor, "_analyze", fake_analyze)
        monkeypatch.setattr(memdoctor, "MIN_CORRECTIONS_TO_FLAG", 2)
        out = signals_for_executive("/my/project")
        assert "error-loop (3x)" in out
        assert "rapid-corrections (2x)" in out
        # error-loop (3x) must rank first
        assert out.index("error-loop") < out.index("rapid-corrections")

    def test_below_threshold_returns_empty(self, monkeypatch):
        """Signals below MIN_CORRECTIONS_TO_FLAG are dropped (noise filter)."""
        import memdoctor

        def fake_analyze(project_filter=None):
            return {
                "sessions": 1,
                "projects": {project_filter: {"error-loop": 1}},
                "totals": {"error-loop": 1},
                "error_samples": [],
            }

        monkeypatch.setattr(memdoctor, "_analyze", fake_analyze)
        monkeypatch.setattr(memdoctor, "MIN_CORRECTIONS_TO_FLAG", 2)
        assert signals_for_executive("/my/project") == ""


class TestMetaFiltering:
    def test_filters_system_reminder_meta_messages(self, tmp_path):
        path = tmp_path / "meta.jsonl"
        path.write_text(
            '{"type":"user","message":{"content":"no wrong stop"}}\n'
            '{"type":"user","isMeta":true,"message":{"content":"no wrong stop"}}\n'
            '{"type":"user","message":{"content":"<system-reminder>no wrong</system-reminder>"}}\n'
            '{"type":"user","message":{"content":"actually do X"}}\n'
            '{"type":"user","message":{"content":"wait stop"}}\n'
        )
        events = parse_jsonl(path)
        # 3 real corrections of 3 non-meta messages → 100% → should flag
        assert detect_correction_heavy(events) == "correction-heavy"
