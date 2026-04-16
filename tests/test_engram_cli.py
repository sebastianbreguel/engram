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
        "verify-install",
        "on-precompact",
        "on-session-start",
        "on-user-prompt",
    ]:
        assert cmd in result.stdout, f"missing subcommand in help: {cmd}"


def test_version_flag_prints_version():
    result = _run(["--version"])
    assert result.returncode == 0
    assert "engram" in result.stdout
    import re

    assert re.search(r"\d+\.\d+\.\d+", result.stdout), f"no semver in output: {result.stdout!r}"


def test_version_matches_plugin_manifest():
    """Guard against drift between __version__ in engram.py and plugin.json."""
    import re

    engram_src = (REPO / "tools" / "engram.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', engram_src)
    assert m, "could not find __version__ in engram.py"
    engram_version = m.group(1)

    manifest = _json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == engram_version, f"version drift: plugin.json={manifest['version']!r} engram.py={engram_version!r}"


def test_verify_install_detects_drift(tmp_path, monkeypatch):
    """Drift case: one installed tool file has different bytes than repo."""
    fake_home = tmp_path / "home"
    installed = fake_home / ".claude" / "tools"
    installed.mkdir(parents=True)

    # Mirror every tool file, but corrupt one so SHA drifts.
    for p in (REPO / "tools").glob("*.py"):
        (installed / p.name).write_bytes(p.read_bytes())
    (installed / "memcapture.py").write_bytes(b"# drifted content\n")

    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["verify-install"])
    assert result.returncode == 1, f"expected drift exit=1, got {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "DRIFT" in result.stdout
    assert "memcapture.py" in result.stdout


def test_verify_install_reports_sync(tmp_path, monkeypatch):
    """No drift: every installed tool matches repo byte-for-byte."""
    fake_home = tmp_path / "home"
    installed = fake_home / ".claude" / "tools"
    installed.mkdir(parents=True)
    for p in (REPO / "tools").glob("*.py"):
        (installed / p.name).write_bytes(p.read_bytes())

    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["verify-install"])
    assert result.returncode == 0, f"expected sync exit=0, got {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
    assert "in sync" in result.stdout


def test_verify_install_flags_missing_file(tmp_path, monkeypatch):
    """Missing installed file counts as drift."""
    fake_home = tmp_path / "home"
    installed = fake_home / ".claude" / "tools"
    installed.mkdir(parents=True)
    # Install only engram.py; memcapture/mempatterns/memdoctor missing
    (installed / "engram.py").write_bytes((REPO / "tools" / "engram.py").read_bytes())

    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["verify-install"])
    assert result.returncode == 1
    assert "missing" in result.stdout


def test_verify_install_errors_when_no_install(tmp_path, monkeypatch):
    """No ~/.claude/tools/ at all → exit 1 with actionable message."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["verify-install"])
    assert result.returncode == 1
    assert "install.sh" in result.stderr


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


def test_git_state_reports_branch_and_dirty_count(tmp_path):
    """_git_state returns branch + dirty_files on a real repo."""
    import importlib.util
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    sp.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    sp.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("hello")
    sp.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    (repo / "b.txt").write_text("dirty")
    (repo / "c.txt").write_text("dirty")

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    state = mod._git_state(str(repo))
    assert state["branch"] == "main"
    assert state["dirty_files"] == 2


def test_git_state_handles_non_repo(tmp_path):
    """_git_state returns empty state for a non-git directory (no crash)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    state = mod._git_state(str(tmp_path))
    assert state["branch"] is None
    assert state["dirty_files"] == 0


def test_run_llm_helper_and_handler_are_distinct_names():
    """Guard against the name collision between the `_run_claude` helper and
    the `_run_llm` argparse handler. Before the fix, two module-level defs
    named `_run_llm` shadowed each other and broke every executive rebuild.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert hasattr(mod, "_run_claude"), "helper must exist under a non-colliding name"
    assert mod._run_claude.__code__.co_argcount >= 2, "_run_claude takes (prompt, chunk, ...)"
    assert mod._run_llm.__code__.co_argcount == 1, "_run_llm handler takes (args,) only"


def test_exec_prompt_includes_signals_slot():
    """EXEC_PROMPT must expose a {signals} placeholder so memdoctor friction flows in."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "{signals}" in mod.EXEC_PROMPT
    assert "ACTIVE FRICTION" in mod.EXEC_PROMPT


def test_executive_cache_rotates_to_prev(tmp_path, monkeypatch):
    """_on_executive rotates <slug>.md → <slug>.md.prev before overwriting."""
    import importlib.util

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Seed a cache file by hand, then invoke _on_executive with SKIP_LLM=1 so
    # it would normally no-op. We need a cache write to happen, so patch
    # _run_claude to return a canned string.
    cwd = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    cache = mod._executive_cache_path(cwd)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("OLD SUMMARY\n")

    monkeypatch.setattr(mod, "_run_claude", lambda prompt, chunk="", timeout=120: "NEW SUMMARY")
    # Feed a fake recap so the function doesn't early-exit on empty inputs.
    monkeypatch.setattr(mod, "_latest_recap", lambda c, max_files=20: "fake recap")

    ns = __import__("argparse").Namespace(cwd=cwd, project_key=cwd.replace("/", "-"))
    assert mod._on_executive(ns) == 0
    assert cache.read_text().strip() == "NEW SUMMARY"
    prev = cache.with_suffix(cache.suffix + ".prev")
    assert prev.exists()
    assert prev.read_text().strip() == "OLD SUMMARY"


def test_preview_prev_reads_rotated_cache(tmp_path, monkeypatch):
    """`engram preview --prev` prints the .prev file and never rebuilds."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    # Seed a .prev file for a known cwd; no main cache exists.
    cwd = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    slug = cwd.replace("/", "-").strip("-") or "default"
    exec_dir = fake_home / ".claude" / "engram" / "executive"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / f"{slug}.md.prev").write_text("PREV SUMMARY\n")

    result = _run(["preview", "--cwd", cwd, "--prev"])
    assert result.returncode == 0
    assert "PREV SUMMARY" in result.stdout


def test_preview_prev_reports_missing_cleanly(tmp_path, monkeypatch):
    """`engram preview --prev` with no .prev file returns a clean message."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    result = _run(["preview", "--cwd", str(tmp_path / "nope"), "--prev"])
    assert result.returncode == 0
    assert "no previous executive summary" in result.stdout


def test_session_start_banner_surfaces_friction(tmp_path, monkeypatch):
    """When memdoctor reports signals for the current cwd, the banner appends
    a one-liner — the whole point is that users see it without running doctor."""
    import importlib.util

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Fake a signals report for the current cwd.
    monkeypatch.setattr(mod.memdoctor, "signals_banner_line", lambda cwd, top_n=2: "friction: error-loop(3x) (run: engram doctor)")

    # Drive _on_session_start with a payload that has a cwd.
    import io as _io

    payload = _json.dumps({"cwd": str(tmp_path), "session_id": "t"})
    monkeypatch.setattr("sys.stdin", _io.StringIO(payload))

    buf = _io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    assert mod._on_session_start(None) == 0
    out = _json.loads(buf.getvalue())
    assert "friction:" in out.get("systemMessage", "")
    assert "engram doctor" in out["systemMessage"]


def test_session_start_banner_omits_friction_when_none(tmp_path, monkeypatch):
    """No signals → banner must not contain the friction string. Avoids
    crying wolf on clean sessions."""
    import importlib.util

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    spec = importlib.util.spec_from_file_location("engram_mod", ENGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod.memdoctor, "signals_banner_line", lambda cwd, top_n=2: "")

    import io as _io

    payload = _json.dumps({"cwd": str(tmp_path), "session_id": "t"})
    monkeypatch.setattr("sys.stdin", _io.StringIO(payload))
    buf = _io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    assert mod._on_session_start(None) == 0
    out = _json.loads(buf.getvalue())
    assert "friction:" not in out.get("systemMessage", "")


def test_hooks_json_uses_engram_inline():
    """After Task 8, hooks.json references engram.py, not .sh."""
    config = _json.loads((REPO / "hooks" / "hooks.json").read_text())
    for event in ("PreCompact", "SessionStart", "UserPromptSubmit"):
        for entry in config.get("hooks", {}).get(event, []):
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                assert "engram.py" in cmd, f"hook should reference engram.py: {cmd}"
                assert ".sh" not in cmd, f"hook should not reference a shell script: {cmd}"
