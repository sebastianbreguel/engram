"""Tests for memdoctor.py — conversational signal detectors.

Fixtures ported from millionco/claude-doctor (MIT).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from memdoctor import (
    detect_correction_heavy,
    detect_error_loop,
    detect_keep_going,
    detect_signals,
    format_rules,
    parse_jsonl,
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
