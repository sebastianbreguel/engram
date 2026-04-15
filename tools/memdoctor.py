#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""memdoctor — detect conversational friction signals across Claude Code sessions.

Signal detection ported from millionco/claude-doctor (MIT).
https://github.com/millionco/claude-doctor
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

CORRECTION_PATTERNS = [
    re.compile(r"^no[,.\s!]", re.IGNORECASE),
    re.compile(r"^nope", re.IGNORECASE),
    re.compile(r"^wrong", re.IGNORECASE),
    re.compile(r"^that'?s not", re.IGNORECASE),
    re.compile(r"^not what i", re.IGNORECASE),
    re.compile(r"^i (said|meant|asked|wanted)", re.IGNORECASE),
    re.compile(r"^actually[,\s]", re.IGNORECASE),
    re.compile(r"^wait[,\s]", re.IGNORECASE),
    re.compile(r"^stop", re.IGNORECASE),
    re.compile(r"^instead[,\s]", re.IGNORECASE),
    re.compile(r"^don'?t do that", re.IGNORECASE),
    re.compile(r"^why did you", re.IGNORECASE),
]

KEEP_GOING_PATTERNS = [
    re.compile(r"^keep going", re.IGNORECASE),
    re.compile(r"^continue", re.IGNORECASE),
    re.compile(r"^keep at it", re.IGNORECASE),
    re.compile(r"^more$", re.IGNORECASE),
    re.compile(r"^finish", re.IGNORECASE),
    re.compile(r"^go on", re.IGNORECASE),
    re.compile(r"^don'?t stop", re.IGNORECASE),
    re.compile(r"^you'?re not done", re.IGNORECASE),
    re.compile(r"^not done", re.IGNORECASE),
    re.compile(r"^keep iterating", re.IGNORECASE),
]

META_MESSAGE_PATTERNS = [
    re.compile(r"^<local-command"),
    re.compile(r"^<command-name>"),
    re.compile(r"^<environment>"),
    re.compile(r"^<local-command-stdout>"),
    re.compile(r"^<task-notification"),
    re.compile(r"^<skill"),
    re.compile(r"^<system-reminder>"),
    re.compile(r"^Caveat:"),
]

CORRECTION_RATE_THRESHOLD = 0.2
MIN_CORRECTIONS_TO_FLAG = 2
KEEP_GOING_MIN_TO_FLAG = 2
ERROR_LOOP_THRESHOLD = 3
MAX_USER_MESSAGE_LENGTH = 2000

RULES_MAP = {
    "correction-heavy": (
        "When the user corrects you, stop and re-read their message. Quote back what they asked for and confirm before proceeding."
    ),
    "error-loop": (
        "After 2 consecutive tool failures, stop and change your approach entirely. Explain what failed and try a different strategy."
    ),
    "keep-going-loop": (
        "Complete the FULL task before stopping. Don't stop early — if the user asked for N items, deliver all N before handing back."
    ),
}


def parse_jsonl(path: Path) -> list[dict]:
    """Read a session jsonl file line-by-line. Skip malformed lines."""
    if not Path(path).exists():
        return []
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _is_meta_message(content: str) -> bool:
    return any(p.match(content) for p in META_MESSAGE_PATTERNS)


def _extract_user_texts(events: list[dict]) -> list[str]:
    """User messages with string content, excluding meta/tool-result/interrupt."""
    texts = []
    for ev in events:
        if ev.get("type") != "user":
            continue
        if ev.get("isMeta"):
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, str) or not content:
            continue
        if len(content) > MAX_USER_MESSAGE_LENGTH:
            continue
        if _is_meta_message(content):
            continue
        if content.startswith("[Request interrupted"):
            continue
        texts.append(content)
    return texts


def _extract_tool_error_sequence(events: list[dict]) -> list[bool]:
    """Ordered list of is_error flags from tool_result blocks."""
    seq = []
    for ev in events:
        if ev.get("type") != "user":
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                seq.append(bool(block.get("is_error")))
    return seq


def detect_correction_heavy(events: list[dict]) -> str | None:
    texts = _extract_user_texts(events)
    if not texts:
        return None
    corrections = sum(1 for t in texts if any(p.match(t) for p in CORRECTION_PATTERNS))
    if corrections < MIN_CORRECTIONS_TO_FLAG:
        return None
    rate = corrections / len(texts)
    if rate > CORRECTION_RATE_THRESHOLD:
        return "correction-heavy"
    return None


def detect_error_loop(events: list[dict]) -> str | None:
    seq = _extract_tool_error_sequence(events)
    consecutive = 0
    for is_error in seq:
        consecutive = consecutive + 1 if is_error else 0
        if consecutive >= ERROR_LOOP_THRESHOLD:
            return "error-loop"
    return None


def detect_keep_going(events: list[dict]) -> str | None:
    texts = _extract_user_texts(events)
    count = sum(1 for t in texts if any(p.match(t.strip()) for p in KEEP_GOING_PATTERNS))
    if count >= KEEP_GOING_MIN_TO_FLAG:
        return "keep-going-loop"
    return None


def detect_signals(events: list[dict]) -> list[str]:
    return [s for s in (detect_correction_heavy(events), detect_error_loop(events), detect_keep_going(events)) if s]


def format_rules(signals: set[str]) -> str:
    bullets = [f"- {RULES_MAP[s]}" for s in sorted(signals) if s in RULES_MAP]
    return "\n".join(bullets)


def _decode_project(encoded: str) -> str:
    """Reverse Claude Code's project encoding: '-Users-foo-bar' → '/Users/foo/bar'."""
    return "/" + encoded.lstrip("-").replace("-", "/")


def _iter_sessions(project_filter: str | None = None):
    """Yield (project_slug, session_id, events) for all sessions, optionally filtered.

    project_filter matches as substring against the decoded project path.
    """
    if not PROJECTS_DIR.exists():
        return
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        decoded = _decode_project(project_dir.name)
        if project_filter and project_filter not in decoded and project_filter not in project_dir.name:
            continue
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            events = parse_jsonl(jsonl)
            if events:
                yield decoded, jsonl.stem, events


def _analyze(project_filter: str | None = None) -> dict:
    """Walk sessions, return aggregated signal counts + per-project breakdown."""
    per_project: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    session_count = 0
    for project, _sid, events in _iter_sessions(project_filter):
        session_count += 1
        for signal in detect_signals(events):
            per_project[project][signal] += 1
    totals: dict[str, int] = defaultdict(int)
    for sig_counts in per_project.values():
        for signal, count in sig_counts.items():
            totals[signal] += count
    return {
        "sessions": session_count,
        "projects": dict(per_project),
        "totals": dict(totals),
    }


def _print_summary(report: dict) -> None:
    sessions = report["sessions"]
    totals = report["totals"]
    projects = report["projects"]
    print(f"engram doctor — analyzed {sessions} session(s)")
    if not totals:
        print("  No friction signals detected. Nice.")
        return
    print("\nSignal totals:")
    for signal, count in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"  {signal}: {count}")
    print("\nTop projects:")
    ranked = sorted(
        projects.items(),
        key=lambda kv: -sum(kv[1].values()),
    )[:10]
    for project, sig_counts in ranked:
        total = sum(sig_counts.values())
        sigs = ", ".join(f"{s}({c})" for s, c in sorted(sig_counts.items(), key=lambda x: -x[1]))
        print(f"  [{total}] {project} — {sigs}")


def _print_rules(report: dict) -> None:
    detected = {s for s in report["totals"] if report["totals"][s] >= MIN_CORRECTIONS_TO_FLAG}
    rules = format_rules(detected)
    if not rules:
        print("No rules to suggest.")
        return
    print("## Rules (auto-generated by engram doctor)\n")
    print(f"Based on analysis of {report['sessions']} session(s). Paste into your CLAUDE.md or AGENTS.md.\n")
    print(rules)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="engram doctor — friction signal detection")
    p.add_argument("--project", type=str, default=None, help="filter by project path substring")
    p.add_argument("--rules", action="store_true", help="print CLAUDE.md rule suggestions")
    return p


def run(args: argparse.Namespace) -> int:
    report = _analyze(project_filter=args.project)
    if args.rules:
        _print_rules(report)
    else:
        _print_summary(report)
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
