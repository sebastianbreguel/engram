#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""engram — unified CLI for claude-engram.

User subcommands dispatch to the `run(args)` functions exposed by each tool
module. No sys.argv mutation, no SystemExit catching.

Hook subcommands (`on-precompact`, `on-session-start`) orchestrate the work
previously done by shell scripts. LLM calls are fire-and-forget subprocesses
that never block the caller.
"""

from __future__ import annotations

import argparse
import io
import json as _json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))

import memcapture  # noqa: E402
import memdoctor  # noqa: E402
import mempatterns  # noqa: E402


def _memcap_ns(**overrides) -> argparse.Namespace:
    """Base Namespace for memcapture.run() with all flags defaulted off."""
    defaults = dict(
        transcript=None,
        all=False,
        recent=None,
        query=None,
        stats=False,
        memories=None,
        forget=None,
        inject=False,
        inject_project=None,
        banner=False,
        banner_project=None,
        banner_name=None,
        ingest_digest=False,
        ingest_snapshot=False,
        session_id=None,
        project=None,
        ephemeral=False,
        extract_facts=False,
        compactions=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patterns_ns(update: bool = False, status: bool = False, report: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        update=update,
        rebuild=False,
        status=status,
        report=report,
        suggest=False,
        forget=None,
        db_path=Path.home() / ".claude" / "memory.db",
        wiki_dir=Path.home() / ".claude" / "patterns",
    )


def _log_warning(msg: str) -> None:
    """Append a timestamped line to ~/.claude/engram.log. Never raises."""
    try:
        from datetime import datetime, timezone

        log = Path.home() / ".claude" / "engram.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _find_transcript(session_id: str) -> tuple[Path, str] | None:
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    for project_dir in projects_dir.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate, project_dir.name
    return None


_SALIENCE_RECENCY_KEEP = 20
_CORRECTION_MARKERS = ("no,", "no.", "stop", "wait", "actually", "en realidad", "no es", "no quiero", "don't", "undo")
_ACK_TOKENS = {"ok", "dale", "si", "sí", "yes", "no", "gracias", "thanks", "yap", "ya", "k"}
_ERROR_MARKERS = ("error", "failed", "traceback", "exception", "errno")
_DECISION_MARKERS = ("let's ", "we'll ", "hagamos", "vamos a ", "prefiero", "quiero que", "decide")


def _score_turn(role: str, text: str) -> float:
    """Assign 0.0–1.0 salience to a single turn. Higher = more likely to survive compression."""
    stripped = text.strip()
    low = stripped.lower()
    score = 0.5

    if role == "user":
        if any(m in low[:60] for m in _CORRECTION_MARKERS):
            score += 0.4
        if any(m in low for m in _DECISION_MARKERS):
            score += 0.15
        if low in _ACK_TOKENS or (len(stripped) < 15 and low.rstrip(".!?") in _ACK_TOKENS):
            score -= 0.45

    if any(m in low for m in _ERROR_MARKERS):
        score += 0.2

    if role == "assistant" and len(stripped) < 50:
        score -= 0.15

    return max(0.0, min(1.0, score))


def _extract_chunk(transcript: Path, tail_lines: int = 800, max_chars: int = 6000) -> str:
    """Extract tail of transcript, compressed by salience when over budget.

    Compression policy:
      1. Always preserve the last _SALIENCE_RECENCY_KEEP turns (recency matters for handoff).
      2. From remaining older turns, pack highest-salience first until max_chars.
      3. Re-emit in chronological order with '...' between non-contiguous kept turns.
    """
    from collections import deque

    tail: deque[str] = deque(maxlen=tail_lines)
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            tail.append(raw)

    turns: list[tuple[str, str, float]] = []
    for raw in tail:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        t = obj.get("type", "")
        if t == "user":
            content = obj.get("message", {}).get("content", "")
            if isinstance(content, str) and len(content) > 5:
                text = content[:500]
                turns.append(("USER", text, _score_turn("user", text)))
        elif t == "assistant":
            blocks = obj.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text = b.get("text", "")[:500]
                        if text:
                            turns.append(("ASSISTANT", text, _score_turn("assistant", text)))
                        break

    if not turns:
        return ""

    def _render(idxs: list[int]) -> str:
        out: list[str] = []
        prev = -2
        for i in idxs:
            if prev >= 0 and i - prev > 1:
                out.append("...")
            role, text, _ = turns[i]
            out.append(f"{role}: {text}")
            prev = i
        return "\n".join(out)

    all_idxs = list(range(len(turns)))
    full = _render(all_idxs)
    if len(full) <= max_chars:
        return full

    total = len(turns)

    def _adjusted_score(i: int) -> float:
        pos_from_end = total - 1 - i
        base = turns[i][2]
        if pos_from_end < _SALIENCE_RECENCY_KEEP:
            # Recent turns get +1.0 to +0.7 (newest = biggest bonus), making them dominate.
            bonus = 1.0 - 0.3 * (pos_from_end / max(1, _SALIENCE_RECENCY_KEEP))
            return base + bonus
        return base

    def _line_cost(i: int) -> int:
        role, text, _ = turns[i]
        return len(role) + len(text) + 3

    GAP_COST = 4  # "...\n"
    ranked = sorted(range(total), key=lambda i: (_adjusted_score(i), -i), reverse=True)

    keep: set[int] = set()
    budget = 0
    for i in ranked:
        c = _line_cost(i)
        if budget + c + GAP_COST > max_chars:
            continue
        keep.add(i)
        budget += c

    if not keep:
        keep.add(total - 1)

    rendered = _render(sorted(keep))
    if len(rendered) <= max_chars:
        return rendered

    # Gaps underestimated — drop lowest-scoring kept turns until the render fits.
    drop_order = sorted(keep, key=_adjusted_score)
    for i in drop_order:
        if len(keep) <= 1:
            break
        keep.discard(i)
        rendered = _render(sorted(keep))
        if len(rendered) <= max_chars:
            return rendered
    return rendered


DIGEST_PROMPT = """Analyze this coding session transcript. Extract concrete, reusable facts as atomic memories.

Each memory has:
- topic: a stable snake_case identifier (e.g., "package_manager", "test_style", "current_refactor")
- durability: "durable" for preferences/lessons/practices that persist, "ephemeral" for current project state and pending work
- content: one specific sentence

Rules:
- One fact per line, format: topic | durability | content
- Be specific, not generic. "prefers uv over pip" not "has package manager preferences"
- Reuse existing topics when the concept matches — e.g., if "package_manager" already covers uv preferences, update that topic instead of creating "dependency_management" or "uv_preference". Same concept = same topic.
- Skip routine actions (file reads, git commits, navigation)
- Max 10 facts per session
- Output ONLY the facts, no commentary

After the facts, add ONE blank line, then a single handoff paragraph addressed to the NEXT Claude session working in this project. Start with "HANDOFF: " and write 2-4 sentences in natural prose: what were we doing, where did we leave off, what should the next session pick up. Be concrete, not meta."""

EXEC_PROMPT = """Mergeá recap + context en un punteo ejecutivo de 3 líneas para abrir la próxima sesión.

RECAP: {recap}
CONTEXT: {context}

Formato EXACTO (3 bullets, en este orden, cada uno ≤90 chars):
- status: <proyecto + estado actual>
- last change: <lo último que se hizo o decidió>
- next: <próxima acción; uní pasos secuenciales con " → ", máx 3>

Sin paths absolutos, sin "Prioridad", sin comillas de code, sin preámbulo ni cierre.
Si falta info para un campo, escribí "- <key>: —".

Ejemplo:
- status: claude-engram post-refactor, 46/46 tests
- last change: fix argparse --flag=value en fire-and-forget
- next: dispatcher DISPATCH dict → integrar latest_recap() → docs

Devolvé SOLO las 3 líneas.
"""


EXECUTIVE_DIR = Path.home() / ".claude" / "engram" / "executive"


def _cwd_from_transcript(transcript: Path) -> str:
    """First cwd found in a transcript. Empty string if none."""
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except Exception:
        pass
    return ""


def _latest_recap(cwd: str, max_files: int = 20) -> str | None:
    """Find the most recent Claude Code away_summary entry for this cwd.

    Scans up to `max_files` most-recent JSONL transcripts under
    ~/.claude/projects/. Returns the content (without the "(disable recaps…)"
    hint), or None if nothing matches.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not cwd or not projects_dir.exists():
        return None
    try:
        candidates = sorted(
            projects_dir.glob("*/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:max_files]
    except Exception:
        return None
    for path in candidates:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        obj = _json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") == "system" and obj.get("subtype") == "away_summary" and obj.get("cwd") == cwd:
                        content = obj.get("content", "")
                        if isinstance(content, str) and content.strip():
                            return content.removesuffix(" (disable recaps in /config)").strip()
        except Exception:
            continue
    return None


def _executive_cache_path(cwd: str) -> Path:
    slug = cwd.replace("/", "-").strip("-") or "default"
    return EXECUTIVE_DIR / f"{slug}.md"


SNAPSHOT_PROMPT = """Analyze this coding session transcript. Extract the current work state as JSON:

{
  "task": "what the user is currently working on (one sentence)",
  "files": ["files actively being edited"],
  "last_error": "last error encountered, or null",
  "summary": "2-3 sentence context: what just happened, decisions made, what is next"
}

Rules:
- Be specific and concise
- Files: only those actively being worked on, not just read
- Summary: focus on decisions and next steps, not history
- Output ONLY valid JSON, no commentary"""


def _run_haiku(prompt: str, chunk: str, timeout: int = 120) -> str:
    if os.environ.get("ENGRAM_SKIP_LLM") == "1":
        return ""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        _log_warning("claude CLI not found in PATH; skipping Haiku call")
        return ""
    try:
        result = subprocess.run(
            [claude_bin, "--print", "--model", "claude-sonnet-4-6", "-p", prompt],
            input=chunk,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            _log_warning(f"claude exit {result.returncode}: {result.stderr[:200]}")
            return ""
        return result.stdout
    except subprocess.TimeoutExpired:
        _log_warning(f"claude timed out after {timeout}s")
        return ""
    except Exception as e:
        _log_warning(f"claude subprocess error: {e}")
        return ""


def _fire_and_forget(cmd: list[str]) -> None:
    """Spawn a detached subprocess. stderr goes to engram.log so failures are debuggable."""
    log = Path.home() / ".claude" / "engram.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        err = log.open("a", encoding="utf-8")
    except Exception:
        err = subprocess.DEVNULL
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
    finally:
        if err is not subprocess.DEVNULL:
            try:
                err.close()
            except Exception:
                pass


_COUNTER_FILE = Path.home() / ".claude" / ".engram-prompt-count"
_DIGEST_EVERY = int(os.environ.get("ENGRAM_DIGEST_EVERY", "25"))


def _read_counter() -> tuple[str, int]:
    """Read (session_id, count) from counter file. Returns ('', 0) if missing/corrupt."""
    try:
        text = _COUNTER_FILE.read_text().strip()
        sid, n = text.rsplit(":", 1)
        return sid, int(n)
    except Exception:
        return "", 0


def _write_counter(session_id: str, count: int) -> None:
    try:
        _COUNTER_FILE.write_text(f"{session_id}:{count}")
    except Exception:
        pass


def _reset_counter() -> None:
    try:
        _COUNTER_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _on_precompact(_args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = _json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    session_id = payload.get("session_id") or ""
    if not session_id:
        return 0

    found = _find_transcript(session_id)
    if found is None:
        return 0
    transcript, project = found

    import memcapture

    try:
        memcapture.run(_memcap_ns(transcript=str(transcript)))
    except Exception as e:
        print(f"capture error: {e}", file=sys.stderr)

    for mode in ("digest", "snapshot"):
        _fire_and_forget(
            [
                sys.executable,
                str(Path(__file__)),
                "_run-llm",
                "--mode",
                mode,
                "--transcript",
                str(transcript),
                "--session-id",
                session_id,
                f"--project={project}",
            ]
        )

    import mempatterns

    try:
        from pathlib import Path as _P

        mempatterns.run(
            argparse.Namespace(
                update=True,
                rebuild=False,
                status=False,
                report=False,
                suggest=False,
                forget=None,
                db_path=_P.home() / ".claude" / "memory.db",
                wiki_dir=_P.home() / ".claude" / "patterns",
            )
        )
    except Exception as e:
        print(f"patterns error: {e}", file=sys.stderr)

    cwd = payload.get("cwd") or _cwd_from_transcript(transcript)
    if cwd:
        _fire_and_forget(
            [
                sys.executable,
                str(Path(__file__)),
                "_executive",
                f"--cwd={cwd}",
                f"--project-key={cwd.replace('/', '-')}",
            ]
        )

    _reset_counter()
    return 0


def _on_user_prompt(_args: argparse.Namespace) -> int:
    """UserPromptSubmit hook: count turns, trigger mid-session digest every N prompts."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = _json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    session_id = payload.get("session_id") or ""
    if not session_id:
        print(_json.dumps({"continue": True}))
        return 0

    prev_sid, count = _read_counter()
    if prev_sid != session_id:
        count = 0
    count += 1
    _write_counter(session_id, count)

    if count >= _DIGEST_EVERY:
        found = _find_transcript(session_id)
        if found is not None:
            transcript, project = found
            import memcapture

            try:
                memcapture.run(_memcap_ns(transcript=str(transcript)))
            except Exception:
                pass
            _fire_and_forget(
                [
                    sys.executable,
                    str(Path(__file__)),
                    "_run-llm",
                    "--mode",
                    "digest",
                    "--transcript",
                    str(transcript),
                    "--session-id",
                    session_id,
                    f"--project={project}",
                ]
            )
            cwd = payload.get("cwd") or _cwd_from_transcript(transcript)
            if cwd:
                _fire_and_forget(
                    [
                        sys.executable,
                        str(Path(__file__)),
                        "_executive",
                        "--cwd",
                        cwd,
                        "--project-key",
                        cwd.replace("/", "-"),
                    ]
                )
        _write_counter(session_id, 0)

    print(_json.dumps({"continue": True}))
    return 0


_LLM_MODES = {
    "digest": {"tail_lines": 800, "max_chars": 6000, "prompt": DIGEST_PROMPT, "ingest": "ingest_digest"},
    "snapshot": {"tail_lines": 1500, "max_chars": 12000, "prompt": SNAPSHOT_PROMPT, "ingest": "ingest_snapshot"},
}


def _run_llm(args: argparse.Namespace) -> int:
    cfg = _LLM_MODES.get(args.mode)
    if cfg is None:
        return 1
    chunk = _extract_chunk(Path(args.transcript), tail_lines=cfg["tail_lines"], max_chars=cfg["max_chars"])
    if len(chunk) < 50:
        return 0
    output = _run_haiku(cfg["prompt"], chunk)
    if not output:
        return 0
    import memcapture

    return memcapture.run(
        _memcap_ns(**{cfg["ingest"]: True, "session_id": args.session_id, "project": args.project}),
        input_text=output,
    )


def _on_executive(args: argparse.Namespace) -> int:
    """Internal: merge latest recap + engram inject_context into a bullet-list
    executive summary, cache to disk for next SessionStart.

    Fire-and-forget from PreCompact / UserPromptSubmit; the cache is read by
    _on_session_start. On any failure, silently no-ops (SessionStart falls
    back to the live inject path).
    """
    cwd = args.cwd or ""
    project_key = args.project_key or ""
    if not cwd:
        return 0

    recap = _latest_recap(cwd) or ""

    buf = io.StringIO()
    try:
        memcapture.run(_memcap_ns(inject=True, inject_project=project_key or None), out=buf)
    except Exception as e:
        _log_warning(f"executive: inject_context failed: {e}")
    context = buf.getvalue().strip()

    if not recap and not context:
        return 0

    prompt = EXEC_PROMPT.replace("{recap}", recap or "(sin recap disponible)").replace("{context}", context or "(sin contexto de engram)")
    output = _run_haiku(prompt, chunk="").strip()
    if not output:
        return 0

    cache = _executive_cache_path(cwd)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(output + "\n", encoding="utf-8")
    except Exception as e:
        _log_warning(f"executive: cache write failed: {e}")
    return 0


def _preview(args: argparse.Namespace) -> int:
    """Print the cached executive summary for a cwd. Builds it synchronously
    if no cache exists yet. Useful for debugging and demoing without opening
    a new session.
    """
    cwd = args.cwd or os.getcwd()
    cache = _executive_cache_path(cwd)
    if not cache.exists():
        ns = argparse.Namespace(cwd=cwd, project_key=cwd.replace("/", "-"))
        _on_executive(ns)
    if cache.exists():
        sys.stdout.write(cache.read_text(encoding="utf-8"))
        return 0
    sys.stdout.write("(no executive summary available — no recap, no context, or LLM unavailable)\n")
    return 0


def _on_session_start(_args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = _json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    cwd = payload.get("cwd", "")
    project_key = cwd.replace("/", "-") if cwd else ""

    import memcapture

    executive = ""
    if cwd:
        cache = _executive_cache_path(cwd)
        if cache.exists():
            try:
                executive = cache.read_text(encoding="utf-8").strip()
            except Exception:
                executive = ""

    if executive:
        context = executive
    else:
        buf = io.StringIO()
        memcapture.run(_memcap_ns(inject=True, inject_project=project_key or None), out=buf)
        context = buf.getvalue()

    banner = ""
    if os.environ.get("ENGRAM_SHOW_BANNER", "1") == "1":
        buf2 = io.StringIO()
        display_name = Path(cwd).name if cwd else ""
        memcapture.run(
            _memcap_ns(
                banner=True,
                banner_project=project_key or None,
                banner_name=display_name or None,
            ),
            out=buf2,
        )
        banner = buf2.getvalue().strip()
        if executive:
            header = banner.split("\n", 1)[0] if banner else ""
            use_color = os.environ.get("NO_COLOR", "") == "" and os.environ.get("TERM", "") != "dumb"
            if use_color:
                exec_text = "\n".join(f"\033[97m{ln}\033[0m" for ln in executive.splitlines())
            else:
                exec_text = executive
            banner = f"{header}\n{exec_text}" if header else exec_text

    out: dict = {
        "continue": True,
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    }
    if banner:
        out["systemMessage"] = banner
    print(_json.dumps(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engram", description="claude-engram unified CLI")
    sub = p.add_subparsers(dest="cmd", required=False)

    c = sub.add_parser("capture", help="capture a session")
    c.add_argument("--transcript", default=None)
    c.add_argument("--all", action="store_true")
    c.set_defaults(func=lambda a: memcapture.run(_memcap_ns(transcript=a.transcript, all=a.all)))

    i = sub.add_parser("inject", help="produce SessionStart context")
    i.add_argument("--project", default=None)
    i.set_defaults(func=lambda a: memcapture.run(_memcap_ns(inject=True, inject_project=a.project)))

    d = sub.add_parser("digest", help="ingest LLM digest from stdin")
    d.add_argument("--session-id", dest="session_id", default=None)
    d.add_argument("--project", default=None)
    d.set_defaults(func=lambda a: memcapture.run(_memcap_ns(ingest_digest=True, session_id=a.session_id, project=a.project)))

    s = sub.add_parser("snapshot", help="ingest work-state snapshot from stdin")
    s.add_argument("--session-id", dest="session_id", default=None)
    s.add_argument("--project", default=None)
    s.set_defaults(func=lambda a: memcapture.run(_memcap_ns(ingest_snapshot=True, session_id=a.session_id, project=a.project)))

    pt = sub.add_parser("patterns", help="pattern detection + wiki")
    pt.add_argument("--update", action="store_true")
    pt.add_argument("--status", action="store_true")
    pt.add_argument("--report", action="store_true")
    pt.set_defaults(func=lambda a: mempatterns.run(_patterns_ns(update=a.update, status=a.status, report=a.report)))

    st = sub.add_parser("stats", help="capture statistics")
    st.set_defaults(func=lambda _a: memcapture.run(_memcap_ns(stats=True)))

    mm = sub.add_parser("memories", help="list learned memories")
    mm.set_defaults(func=lambda _a: memcapture.run(_memcap_ns(memories="*")))

    fg = sub.add_parser("forget", help="delete a memory by topic")
    fg.add_argument("topic")
    fg.set_defaults(func=lambda a: memcapture.run(_memcap_ns(forget=a.topic)))

    dr = sub.add_parser("doctor", help="detect friction signals across sessions")
    dr.add_argument("--project", type=str, default=None, help="filter by project path substring")
    dr.add_argument("--rules", action="store_true", help="print CLAUDE.md rule suggestions")
    dr.set_defaults(func=lambda a: memdoctor.run(argparse.Namespace(project=a.project, rules=a.rules)))

    op = sub.add_parser("on-precompact", help="hook: orchestrate PreCompact work")
    op.set_defaults(func=_on_precompact)

    ss = sub.add_parser("on-session-start", help="hook: orchestrate SessionStart injection + banner")
    ss.set_defaults(func=_on_session_start)

    up = sub.add_parser("on-user-prompt", help="hook: mid-session digest every N prompts")
    up.set_defaults(func=_on_user_prompt)

    rl = sub.add_parser("_run-llm", help="(internal) Haiku digest or snapshot")
    rl.add_argument("--mode", choices=sorted(_LLM_MODES.keys()), required=True)
    rl.add_argument("--transcript", required=True)
    rl.add_argument("--session-id", dest="session_id", required=True)
    rl.add_argument("--project", required=True)
    rl.set_defaults(func=_run_llm)

    ex = sub.add_parser("_executive", help="(internal) build executive summary cache for a cwd")
    ex.add_argument("--cwd", required=True)
    ex.add_argument("--project-key", dest="project_key", default="")
    ex.set_defaults(func=_on_executive)

    pv = sub.add_parser("preview", help="preview SessionStart executive summary (for debug/demo)")
    pv.add_argument("--cwd", default=None)
    pv.set_defaults(func=_preview)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
