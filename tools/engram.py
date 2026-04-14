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


def _memcap_ns(**overrides) -> argparse.Namespace:
    """Base Namespace for memcapture.run() with all flags defaulted off."""
    defaults = dict(
        transcript=None,
        all=False,
        recent=None,
        query=None,
        dashboard=False,
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


def _dispatch_capture(args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(transcript=args.transcript, all=args.all))


def _dispatch_inject(args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(inject=True, inject_project=args.project))


def _dispatch_digest(args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(ingest_digest=True, session_id=args.session_id, project=args.project))


def _dispatch_snapshot(args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(ingest_snapshot=True, session_id=args.session_id, project=args.project))


def _dispatch_stats(_args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(stats=True))


def _dispatch_memories(_args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(memories="*"))


def _dispatch_forget(args: argparse.Namespace) -> int:
    import memcapture

    return memcapture.run(_memcap_ns(forget=args.topic))


def _dispatch_patterns(args: argparse.Namespace) -> int:
    import mempatterns

    from pathlib import Path as _P

    return mempatterns.run(
        argparse.Namespace(
            update=args.update,
            rebuild=False,
            status=args.status,
            report=args.report,
            suggest=False,
            forget=None,
            db_path=_P.home() / ".claude" / "memory.db",
            wiki_dir=_P.home() / ".claude" / "patterns",
        )
    )


def _dispatch_dashboard(args: argparse.Namespace) -> int:
    import memdashboard

    output = args.output or str(Path.home() / ".claude" / "engram-dashboard.html")
    return memdashboard.run(argparse.Namespace(output=output, no_open=args.no_open))


def _print_compile_deprecation() -> None:
    print(
        "NOTE: `engram compile` / `compiled-knowledge/` is planned for deprecation in engram v2.\n"
        "      v2 replaces the markdown artifact with an automatic cross-project `concepts`\n"
        "      table in memory.db. Use `engram export-concepts` for the markdown bridge.",
        file=sys.stderr,
    )


def _dispatch_compile(args: argparse.Namespace) -> int:
    _print_compile_deprecation()
    import memcompile

    return memcompile.run(argparse.Namespace(lint_only=args.lint_only, dry_run=args.dry_run))


def _dispatch_export_concepts(args: argparse.Namespace) -> int:
    _print_compile_deprecation()
    import memcompile

    rc = memcompile.run(argparse.Namespace(lint_only=False, dry_run=False))
    out = Path(args.output) if args.output else Path.home() / ".claude" / "compiled-knowledge" / "concepts.md"
    if out.exists():
        print(f"Exported: {out}")
        return rc
    print(f"No concepts file produced at {out}", file=sys.stderr)
    return 1


def _find_transcript(session_id: str) -> tuple[Path, str] | None:
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    for project_dir in projects_dir.iterdir():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate, project_dir.name
    return None


def _extract_chunk(transcript: Path, pct: float = 0.2, max_chars: int = 6000) -> str:
    lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, int(len(lines) * (1 - pct)))
    msgs: list[str] = []
    for raw in lines[start:]:
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
                msgs.append("USER: " + content[:500])
        elif t == "assistant":
            blocks = obj.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "text":
                        msgs.append("ASSISTANT: " + b.get("text", "")[:500])
                        break
    out = "\n".join(msgs)
    if len(out) > max_chars:
        half = max_chars // 2
        out = out[:half] + "\n...\n" + out[-half:]
    return out


DIGEST_PROMPT = """Analyze this coding session transcript. Extract concrete, reusable facts as atomic memories.

Each memory has:
- topic: a stable snake_case identifier (e.g., "package_manager", "test_style", "current_refactor")
- durability: "durable" for preferences/lessons/practices that persist, "ephemeral" for current project state and pending work
- content: one specific sentence

Rules:
- One fact per line, format: topic | durability | content
- Be specific, not generic. "prefers uv over pip" not "has package manager preferences"
- Skip routine actions (file reads, git commits, navigation)
- Max 10 facts per session
- Output ONLY the facts, no commentary

After the facts, add ONE blank line, then a single handoff paragraph addressed to the NEXT Claude session working in this project. Start with "HANDOFF: " and write 2-4 sentences in natural prose: what were we doing, where did we leave off, what should the next session pick up. Be concrete, not meta."""

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
        return ""
    try:
        result = subprocess.run(
            [claude_bin, "--print", "--model", "claude-haiku-4-5", "-p", prompt],
            input=chunk,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _fire_and_forget(cmd: list[str]) -> None:
    """Spawn a detached subprocess that survives the parent's exit."""
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _dispatch_on_precompact(_args: argparse.Namespace) -> int:
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

    _fire_and_forget(
        [
            sys.executable,
            str(Path(__file__)),
            "_run-digest",
            "--transcript",
            str(transcript),
            "--session-id",
            session_id,
            "--project",
            project,
        ]
    )
    _fire_and_forget(
        [
            sys.executable,
            str(Path(__file__)),
            "_run-snapshot",
            "--transcript",
            str(transcript),
            "--session-id",
            session_id,
            "--project",
            project,
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

    return 0


def _dispatch_run_digest(args: argparse.Namespace) -> int:
    chunk = _extract_chunk(Path(args.transcript), pct=0.2, max_chars=6000)
    if len(chunk) < 50:
        return 0
    output = _run_haiku(DIGEST_PROMPT, chunk)
    if not output:
        return 0
    import memcapture

    sys.stdin = io.StringIO(output)
    return memcapture.run(_memcap_ns(ingest_digest=True, session_id=args.session_id, project=args.project))


def _dispatch_run_snapshot(args: argparse.Namespace) -> int:
    chunk = _extract_chunk(Path(args.transcript), pct=0.15, max_chars=12000)
    if len(chunk) < 50:
        return 0
    output = _run_haiku(SNAPSHOT_PROMPT, chunk)
    if not output:
        return 0
    import memcapture

    sys.stdin = io.StringIO(output)
    return memcapture.run(_memcap_ns(ingest_snapshot=True, session_id=args.session_id, project=args.project))


def _dispatch_on_session_start(_args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = _json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    cwd = payload.get("cwd", "")
    project_key = cwd.replace("/", "-") if cwd else ""

    import memcapture

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

    i = sub.add_parser("inject", help="produce SessionStart context")
    i.add_argument("--project", default=None)

    d = sub.add_parser("digest", help="ingest LLM digest from stdin")
    d.add_argument("--session-id", dest="session_id", default=None)
    d.add_argument("--project", default=None)

    s = sub.add_parser("snapshot", help="ingest work-state snapshot from stdin")
    s.add_argument("--session-id", dest="session_id", default=None)
    s.add_argument("--project", default=None)

    pt = sub.add_parser("patterns", help="pattern detection + wiki")
    pt.add_argument("--update", action="store_true")
    pt.add_argument("--status", action="store_true")
    pt.add_argument("--report", action="store_true")

    dash = sub.add_parser("dashboard", help="visual dashboard")
    dash.add_argument("--output", default=None)
    dash.add_argument("--no-open", dest="no_open", action="store_true")

    cm = sub.add_parser("compile", help="cross-project memory compile (manual, deprecated)")
    cm.add_argument("--lint-only", dest="lint_only", action="store_true")
    cm.add_argument("--dry-run", dest="dry_run", action="store_true")

    ec = sub.add_parser("export-concepts", help="regenerate compiled-knowledge/concepts.md (migration bridge)")
    ec.add_argument("--output", default=None)

    sub.add_parser("stats", help="capture statistics")
    sub.add_parser("memories", help="list learned memories")

    fg = sub.add_parser("forget", help="delete a memory by topic")
    fg.add_argument("topic")

    sub.add_parser("on-precompact", help="hook: orchestrate PreCompact work")
    sub.add_parser("on-session-start", help="hook: orchestrate SessionStart injection + banner")

    rd = sub.add_parser("_run-digest", help="(internal) Haiku digest")
    rd.add_argument("--transcript", required=True)
    rd.add_argument("--session-id", dest="session_id", required=True)
    rd.add_argument("--project", required=True)

    rs = sub.add_parser("_run-snapshot", help="(internal) Haiku snapshot")
    rs.add_argument("--transcript", required=True)
    rs.add_argument("--session-id", dest="session_id", required=True)
    rs.add_argument("--project", required=True)

    return p


DISPATCH = {
    "capture": _dispatch_capture,
    "inject": _dispatch_inject,
    "digest": _dispatch_digest,
    "snapshot": _dispatch_snapshot,
    "patterns": _dispatch_patterns,
    "dashboard": _dispatch_dashboard,
    "compile": _dispatch_compile,
    "export-concepts": _dispatch_export_concepts,
    "stats": _dispatch_stats,
    "memories": _dispatch_memories,
    "forget": _dispatch_forget,
    "on-precompact": _dispatch_on_precompact,
    "on-session-start": _dispatch_on_session_start,
    "_run-digest": _dispatch_run_digest,
    "_run-snapshot": _dispatch_run_snapshot,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    return DISPATCH[args.cmd](args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
