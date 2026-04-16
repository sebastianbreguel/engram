"""CLI surface tests for engram.py."""

from __future__ import annotations

import json as _json
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
ENGRAM = REPO / "tools" / "engram.py"


def _run(args: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", str(ENGRAM), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
        **kw,
    )


def test_help_lists_all_subcommands():
    result = _run(["--help"])
    assert result.returncode == 0
    for cmd in [
        "capture",
        "inject",
        "digest",
        "snapshot",
        "patterns",
        "stats",
        "memories",
        "forget",
        "search",
        "log",
        "on-precompact",
        "on-session-start",
        "on-user-prompt",
    ]:
        assert cmd in result.stdout, f"missing subcommand in help: {cmd}"


def test_stats_runs(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["stats"])
    assert result.returncode == 0


def test_inject_runs(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["inject"])
    assert result.returncode == 0


def test_on_session_start_emits_valid_json(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    payload = _json.dumps({"cwd": str(tmp_path)})
    result = _run(["on-session-start"], input=payload)
    assert result.returncode == 0
    out = _json.loads(result.stdout)
    assert out.get("continue") is True
    assert out.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"


def test_on_precompact_captures_session(tmp_path, monkeypatch):
    """on-precompact: reads session_id from stdin, captures transcript, skips LLM in test mode."""
    fake_home = tmp_path / "home"
    proj_dir = fake_home / ".claude" / "projects" / "test-proj"
    proj_dir.mkdir(parents=True)
    transcript = proj_dir / "abc123.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"hello"}}\n{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ENGRAM_SKIP_LLM", "1")
    payload = _json.dumps({"session_id": "abc123"})
    result = _run(["on-precompact"], input=payload)
    assert result.returncode == 0, f"on-precompact failed: {result.stderr}"


def test_score_turn_prioritizes_corrections_over_acks():
    import importlib.util

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    correction = mod._score_turn("user", "No, eso está mal, usa uv en vez de pip")
    ack = mod._score_turn("user", "dale")
    assistant_narr = mod._score_turn("assistant", "OK")
    error_mention = mod._score_turn("user", "got a Traceback on this run")

    assert correction > 0.8, f"correction should be high-salience, got {correction}"
    assert ack < 0.2, f"bare ack should be low-salience, got {ack}"
    assert assistant_narr < 0.5, f"short assistant turn should be penalized, got {assistant_narr}"
    assert error_mention > 0.6, f"error mention should boost score, got {error_mention}"


def test_extract_chunk_keeps_recency_and_salience(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    transcript = tmp_path / "t.jsonl"
    lines = []
    # 60 filler turns (low salience)
    for i in range(60):
        lines.append(_json.dumps({"type": "user", "message": {"content": f"dale {i}"}}))
        lines.append(_json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}))
    # 1 early high-salience correction buried in filler
    lines.insert(
        10,
        _json.dumps({"type": "user", "message": {"content": "No, nunca mockees la DB en estos tests — fallamos antes"}}),
    )
    # last 20 turns: distinct markers so we can assert recency
    for i in range(10):
        lines.append(_json.dumps({"type": "user", "message": {"content": f"RECENT_USER_{i} discussing deploy plan here"}}))
        lines.append(
            _json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"RECENT_ASSISTANT_{i} acknowledged"}]}})
        )
    transcript.write_text("\n".join(lines))

    out = mod._extract_chunk(transcript, tail_lines=500, max_chars=1500)

    assert len(out) <= 1500
    assert "RECENT_USER_9" in out, "last turns must survive compression"
    assert "RECENT_ASSISTANT_9" in out, "last turns must survive compression"
    assert "nunca mockees la DB" in out, "early high-salience correction must survive"
    assert "..." in out, "compressed output should contain gap marker"


def test_search_runs_without_crash(tmp_path, monkeypatch):
    """`engram search <query>` exits 0 even against an empty DB."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["search", "anything"])
    assert result.returncode == 0, f"search failed: {result.stderr}"


def test_log_tail_reports_missing_log(tmp_path, monkeypatch):
    """`engram log` reports a clean message when the log file doesn't exist."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["log", "--tail", "5"])
    assert result.returncode == 0
    assert "no log yet" in result.stdout


def test_log_tail_reads_last_n_lines(tmp_path, monkeypatch):
    """`engram log --tail N` returns the last N lines of ~/.claude/engram.log."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    log = fake_home / ".claude" / "engram.log"
    log.write_text("\n".join(f"line-{i}" for i in range(20)) + "\n")
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["log", "--tail", "3"])
    assert result.returncode == 0
    # Last 3 lines: line-17, line-18, line-19
    assert "line-19" in result.stdout
    assert "line-17" in result.stdout
    assert "line-10" not in result.stdout


def test_hooks_json_uses_engram_inline():
    """After Task 8, hooks.json references engram.py, not .sh."""
    config = _json.loads((REPO / "hooks" / "hooks.json").read_text())
    for event in ("PreCompact", "SessionStart", "UserPromptSubmit"):
        for entry in config.get("hooks", {}).get(event, []):
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                assert "engram.py" in cmd, f"hook should reference engram.py: {cmd}"
                assert ".sh" not in cmd, f"hook should not reference a shell script: {cmd}"
