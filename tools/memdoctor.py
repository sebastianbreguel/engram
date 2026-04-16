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
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
MEMORY_DB = Path.home() / ".claude" / "memory.db"

ABS_PATH_RE = re.compile(r"/(?:[\w.-]+/)+[\w.-]+")

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
RAPID_CORRECTION_WINDOW_SECONDS = 60
RAPID_CORRECTION_MIN_COUNT = 2
# Restart-cluster thresholds — conservative educated guesses, no ground truth.
# ≥3 user msgs filters out ~87% of ephemeral JSONLs (hooks, subagents, rapid-fire invocations)
# observed on local data. 30min/n=3 errs toward false negatives over false positives.
MIN_USER_MSGS_PER_SESSION = 3
RESTART_CLUSTER_SIZE = 3
RESTART_WINDOW_MINUTES = 30

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
    "rapid-corrections": ("Two corrections within a minute is a frustration spike. Stop and ask what the user wants before continuing."),
    "restart-cluster": (
        "If the user restarted sessions 3+ times in this project within 30 minutes, something is confusing you. "
        "Ask what went wrong in the previous attempts before retrying."
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


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_user_texts_with_ts(events: list[dict]) -> list[tuple[str, datetime]]:
    """User messages with timestamps — same filters as _extract_user_texts, plus drops entries with unparseable ts."""
    out: list[tuple[str, datetime]] = []
    for ev in events:
        if ev.get("type") != "user" or ev.get("isMeta"):
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, str) or not content:
            continue
        if len(content) > MAX_USER_MESSAGE_LENGTH:
            continue
        if _is_meta_message(content) or content.startswith("[Request interrupted"):
            continue
        ts = _parse_ts(ev.get("timestamp"))
        if ts is None:
            continue
        out.append((content, ts))
    return out


def detect_rapid_corrections(events: list[dict]) -> str | None:
    """Flag when ≥RAPID_CORRECTION_MIN_COUNT corrections land within a rolling RAPID_CORRECTION_WINDOW_SECONDS window."""
    timestamps = [ts for text, ts in _extract_user_texts_with_ts(events) if any(p.match(text) for p in CORRECTION_PATTERNS)]
    if len(timestamps) < RAPID_CORRECTION_MIN_COUNT:
        return None
    window = timedelta(seconds=RAPID_CORRECTION_WINDOW_SECONDS)
    for i in range(len(timestamps) - RAPID_CORRECTION_MIN_COUNT + 1):
        if timestamps[i + RAPID_CORRECTION_MIN_COUNT - 1] - timestamps[i] <= window:
            return "rapid-corrections"
    return None


def _session_meta(events: list[dict]) -> tuple[datetime | None, int]:
    """Return (first_timestamp, count_of_real_user_msgs) — used for restart-cluster filtering."""
    first_ts = None
    n_user = 0
    for ev in events:
        if first_ts is None:
            ts = _parse_ts(ev.get("timestamp"))
            if ts:
                first_ts = ts
        if ev.get("type") == "user" and not ev.get("isMeta"):
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, str) and content and not _is_meta_message(content):
                n_user += 1
    return first_ts, n_user


def count_restart_clusters(starts: list[datetime]) -> int:
    """Non-overlapping count of ≥RESTART_CLUSTER_SIZE session starts within RESTART_WINDOW_MINUTES."""
    starts = sorted(starts)
    if len(starts) < RESTART_CLUSTER_SIZE:
        return 0
    window = timedelta(minutes=RESTART_WINDOW_MINUTES)
    count = 0
    i = 0
    while i <= len(starts) - RESTART_CLUSTER_SIZE:
        if starts[i + RESTART_CLUSTER_SIZE - 1] - starts[i] <= window:
            count += 1
            i += RESTART_CLUSTER_SIZE  # non-overlapping window
        else:
            i += 1
    return count


def detect_signals(events: list[dict]) -> list[str]:
    return [
        s
        for s in (
            detect_correction_heavy(events),
            detect_error_loop(events),
            detect_keep_going(events),
            detect_rapid_corrections(events),
        )
        if s
    ]


def _tool_result_text(block: dict) -> str | None:
    content = block.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def extract_error_context(events: list[dict]) -> str | None:
    """Return the text of the last tool_result with is_error=True, if any."""
    last = None
    for ev in events:
        if ev.get("type") != "user":
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                text = _tool_result_text(block)
                if text:
                    last = text
    return last


def normalize_error(text: str) -> str:
    """First meaningful line with absolute paths replaced by <path>, capped at 200 chars."""
    first = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("File "):
            first = stripped
            break
    if not first:
        first = text.strip()[:200]
    first = ABS_PATH_RE.sub("<path>", first)
    return first[:200]


def enrich_from_memory(error_text: str, db_path: Path = MEMORY_DB) -> dict | None:
    """Look up prior occurrences of an error in memory.db facts table. Returns None if DB missing or no match."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    try:
        tokens = [t for t in re.findall(r"\w{3,}", error_text) if not t.isdigit()][:4]
        rows: list = []
        if tokens:
            fts_query = " ".join(tokens)
            try:
                rows = conn.execute(
                    "SELECT project FROM facts_fts WHERE facts_fts MATCH ? AND type='error' LIMIT 50",
                    (fts_query,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            snippet = error_text[:80]
            rows = conn.execute(
                "SELECT s.project FROM facts f JOIN sessions s ON f.session_id = s.session_id "
                "WHERE f.type='error' AND f.content LIKE ? LIMIT 50",
                (f"%{snippet}%",),
            ).fetchall()
        if not rows:
            return None
        projects = sorted({r["project"] for r in rows if r["project"]})
        return {"count": len(rows), "projects": projects[:3]}
    finally:
        conn.close()


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
    """Walk sessions, return aggregated signal counts + per-project breakdown + error samples."""
    per_project: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    starts_by_project: dict[str, list[datetime]] = defaultdict(list)
    error_samples: list[tuple[str, str]] = []
    session_count = 0
    for project, _sid, events in _iter_sessions(project_filter):
        session_count += 1
        signals = detect_signals(events)
        for signal in signals:
            per_project[project][signal] += 1
        if "error-loop" in signals:
            err = extract_error_context(events)
            if err:
                error_samples.append((project, normalize_error(err)))
        first_ts, n_user = _session_meta(events)
        if first_ts and n_user >= MIN_USER_MSGS_PER_SESSION:
            starts_by_project[project].append(first_ts)
    for project, starts in starts_by_project.items():
        clusters = count_restart_clusters(starts)
        if clusters:
            per_project[project]["restart-cluster"] += clusters
    totals: dict[str, int] = defaultdict(int)
    for sig_counts in per_project.values():
        for signal, count in sig_counts.items():
            totals[signal] += count
    return {
        "sessions": session_count,
        "projects": dict(per_project),
        "totals": dict(totals),
        "error_samples": error_samples,
    }


def _print_summary(report: dict) -> None:
    sessions = report["sessions"]
    totals = report["totals"]
    projects = report["projects"]
    print(f"engram doctor — analyzed {sessions} session(s)")
    if not totals:
        print("  No friction signals detected. Nice.")
        return
    print(f"\nSignal totals (denominator: {sessions} sessions):")
    for signal, count in sorted(totals.items(), key=lambda x: -x[1]):
        pct = (count / sessions * 100) if sessions else 0
        print(f"  {signal}: {count} ({pct:.1f}%)")
    print("\nTop projects:")
    ranked = sorted(
        projects.items(),
        key=lambda kv: -sum(kv[1].values()),
    )[:10]
    for project, sig_counts in ranked:
        total = sum(sig_counts.values())
        sigs = ", ".join(f"{s}({c})" for s, c in sorted(sig_counts.items(), key=lambda x: -x[1]))
        print(f"  [{total}] {project} — {sigs}")
    _print_enriched_errors(report.get("error_samples", []))


def _print_enriched_errors(samples: list[tuple[str, str]]) -> None:
    if not samples:
        return
    counts = Counter(err for _, err in samples)
    top = counts.most_common(5)
    print("\nTop errors (error-loop):")
    for err, count in top:
        print(f"  [{count}x] {err[:100]}")
        enriched = enrich_from_memory(err)
        if enriched:
            projs = ", ".join(enriched["projects"]) or "?"
            print(f"    ↳ seen {enriched['count']}x in memory.db (projects: {projs})")


def _print_rules(report: dict) -> None:
    detected = {s for s in report["totals"] if report["totals"][s] >= MIN_CORRECTIONS_TO_FLAG}
    rules = format_rules(detected)
    if not rules:
        print("No rules to suggest.")
        return
    print("## Rules (auto-generated by engram doctor)\n")
    print(f"Based on analysis of {report['sessions']} session(s). Paste into your CLAUDE.md or AGENTS.md.\n")
    print(rules)


def _print_rules_per_project(report: dict) -> None:
    """Emit per-project rule blocks. Include a project only if it has signals ≥ MIN_CORRECTIONS_TO_FLAG."""
    ranked = sorted(
        report["projects"].items(),
        key=lambda kv: -sum(kv[1].values()),
    )
    emitted = 0
    print("## Rules per project (auto-generated by engram doctor)\n")
    for project, sig_counts in ranked:
        detected = {s for s, c in sig_counts.items() if c >= MIN_CORRECTIONS_TO_FLAG}
        rules = format_rules(detected)
        if not rules:
            continue
        sigs = ", ".join(f"{s}({c})" for s, c in sorted(sig_counts.items(), key=lambda x: -x[1]))
        print(f"### {project}")
        print(f"_{sigs}_\n")
        print(rules)
        print()
        emitted += 1
    if emitted == 0:
        print("No per-project rules to suggest.")


def signals_for_executive(project_filter: str, top_n: int = 3) -> str:
    """Return a short text block of top friction signals for a project.

    Used by engram.py `_on_executive` to inject friction context into the
    executive summary prompt. Empty string if nothing significant fires.

    Only surfaces signals whose count >= MIN_CORRECTIONS_TO_FLAG so noise
    from single-session blips doesn't bleed into `next:` lines.
    """
    if not project_filter:
        return ""
    try:
        report = _analyze(project_filter=project_filter)
    except Exception:
        return ""
    totals = report.get("totals", {})
    if not totals:
        return ""
    ranked = [(s, c) for s, c in totals.items() if c >= MIN_CORRECTIONS_TO_FLAG]
    if not ranked:
        return ""
    ranked.sort(key=lambda x: -x[1])
    lines = [f"- {s} ({c}x)" for s, c in ranked[:top_n]]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="engram doctor — friction signal detection")
    p.add_argument("--project", type=str, default=None, help="filter by project path substring")
    p.add_argument("--rules", action="store_true", help="print CLAUDE.md rule suggestions")
    p.add_argument("--per-project", action="store_true", help="with --rules, emit one rule block per project")
    return p


def run(args: argparse.Namespace) -> int:
    report = _analyze(project_filter=args.project)
    if args.rules and args.per_project:
        _print_rules_per_project(report)
    elif args.rules:
        _print_rules(report)
    else:
        _print_summary(report)
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
