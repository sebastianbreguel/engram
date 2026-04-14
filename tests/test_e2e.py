"""End-to-end contract tests — lock user-visible behavior.

These tests assert on stdout, exit codes, and injected context strings. They do
NOT assert on SQLite column contents or row counts — schema is internal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.jsonl"
REPO = Path(__file__).parent.parent


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Isolate ~/.claude to a tmp dir per test."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _memcap(args: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", str(REPO / "tools" / "memcapture.py"), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        **kw,
    )


def test_capture_transcript_exits_zero(tmp_home):
    """Feeding a transcript succeeds — no crash, no schema assertion."""
    result = _memcap(["--transcript", str(FIXTURE)])
    assert result.returncode == 0, f"capture failed: {result.stderr}"


def test_capture_then_stats_reports_activity(tmp_home):
    """After capture, --stats reports non-zero sessions. Contract: the user sees a summary."""
    _memcap(["--transcript", str(FIXTURE)])
    result = _memcap(["--stats"])
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "session" in combined.lower(), f"stats should mention sessions: {combined!r}"


def test_ingest_digest_then_inject_surfaces_the_memory(tmp_home):
    """End-to-end contract: a digest line about `uv` appears in injected context.

    This is the core user-visible promise of engram: what the LLM learned comes
    back in the next SessionStart context.
    """
    # Capture a session (needed so --ingest-digest has a session_id to attach to).
    _memcap(["--transcript", str(FIXTURE)])

    digest_text = (
        "package_manager | durable | prefers uv over pip\n"
        "current_refactor | ephemeral | removing Docker references from repo\n"
        "\n"
        "HANDOFF: we decided to use uv and drop Docker. Next session should verify install.sh."
    )
    ingest = _memcap(
        ["--ingest-digest", "--session-id", "test-session", "--project", "engram-test"],
        input=digest_text,
    )
    assert ingest.returncode == 0, f"ingest failed: {ingest.stderr}"

    # Contract: the memory surfaces in the injected context string.
    inject = _memcap(["--inject"])
    assert inject.returncode == 0
    assert "uv" in inject.stdout.lower(), f"expected 'uv' in injected context, got: {inject.stdout!r}"


def test_project_scoped_inject_surfaces_handoff(tmp_home):
    """A project-scoped digest with a HANDOFF surfaces when --inject-project matches."""
    _memcap(["--transcript", str(FIXTURE)])
    digest = "test_topic | ephemeral | working on auth refactor\n\nHANDOFF: halfway through extracting auth middleware into its own module."
    _memcap(
        ["--ingest-digest", "--session-id", "s1", "--project", "my-project"],
        input=digest,
    )
    result = _memcap(["--inject", "--inject-project", "my-project"])
    assert result.returncode == 0
    # Handoff content should reach the user's context. Exact wording/placement is free.
    assert "auth" in result.stdout.lower(), f"project-scoped handoff should surface, got: {result.stdout!r}"


def test_semantic_error_regex_removed_from_module():
    """Task 4 contract: the regex lists that distinguish 'real errors' from
    'code mentioning errors' are gone. The LLM digest handles semantic judgment.

    Fails against current code (both attrs exist), passes after Task 4 deletes them.
    """
    import importlib

    memcap = importlib.import_module("memcapture")
    assert not hasattr(memcap, "ACTUAL_ERROR_PATTERNS"), "ACTUAL_ERROR_PATTERNS should be removed — LLM digest handles error judgment"
    assert not hasattr(memcap, "ERROR_FALSE_POSITIVES"), "ERROR_FALSE_POSITIVES should be removed — no longer needed without regex matching"


def test_non_error_tool_result_with_traceback_does_not_capture_fact(tmp_home, tmp_path):
    """Task 4 behavioral contract: a tool_result with is_error=False containing a
    Traceback string should NOT produce a facts.type='error' row.

    Current code matches ACTUAL_ERROR_PATTERNS on the Traceback line and captures it.
    After Task 4, only is_error=True triggers capture. LLM digest handles the rest.
    """
    import sqlite3

    fake_transcript = tmp_path / "fake.jsonl"
    fake_transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "run the script please"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "running the script now"}]},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "Traceback (most recent call last):\n  File \"/tmp/x.py\", line 3, in <module>\n    raise ValueError('demo')",
                            "is_error": False,
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    result = _memcap(["--transcript", str(fake_transcript)])
    assert result.returncode == 0

    db = tmp_home / ".claude" / "memory.db"
    conn = sqlite3.connect(str(db))
    error_facts = conn.execute("SELECT content FROM facts WHERE type='error'").fetchall()
    conn.close()
    assert error_facts == [], f"non-error tool_result should not produce error facts, got: {error_facts!r}"


def test_facts_table_has_typed_columns(tmp_home):
    """v1 schema widen: facts has nullable subject/predicate/object/confidence.

    v1 never populates them. v2 will. This test guards that the columns exist.
    """
    import sqlite3

    _memcap(["--transcript", str(FIXTURE)])
    db = tmp_home / ".claude" / "memory.db"
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
    conn.close()
    assert {"subject", "predicate", "object", "confidence"}.issubset(cols), f"facts table missing typed columns, has: {cols}"


def test_ingest_digest_is_idempotent(tmp_home):
    """Same digest ingested twice produces the same --memories output (no duplicates)."""
    _memcap(["--transcript", str(FIXTURE)])
    digest = "package_manager | durable | prefers uv over pip\n\nHANDOFF: uv only."
    _memcap(["--ingest-digest", "--session-id", "s1", "--project", "p1"], input=digest)
    first = _memcap(["--memories"]).stdout
    _memcap(["--ingest-digest", "--session-id", "s1", "--project", "p1"], input=digest)
    second = _memcap(["--memories"]).stdout
    assert first.count("prefers uv over pip") == second.count("prefers uv over pip"), (
        "repeated ingest of identical digest produced duplicate memories"
    )
