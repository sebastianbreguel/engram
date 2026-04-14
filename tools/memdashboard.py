#!/usr/bin/env python3
"""memdashboard — Generate a static HTML dashboard from engram's SQLite data.

Usage:
    uv run ~/.claude/tools/memdashboard.py              # generate and open dashboard
    uv run ~/.claude/tools/memdashboard.py --output /tmp/dash.html  # custom output path
"""
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import json
import sqlite3
import webbrowser
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "memory.db"
PATTERNS_DIR = Path.home() / ".claude" / "patterns"
IGNORE_PATH = PATTERNS_DIR / ".ignore"


def load_ignore_list() -> list[str]:
    if not IGNORE_PATH.exists():
        return []
    return [
        line.strip()
        for line in IGNORE_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def is_ignored(project: str, ignore_list: list[str]) -> bool:
    return any(sub in project for sub in ignore_list)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ignore_where(
    ignore_list: list[str], project_col: str = "project", prefix: str = "WHERE"
) -> tuple[str, list[str]]:
    """Build SQL WHERE clause to exclude ignored projects."""
    if not ignore_list:
        return "", []
    clauses = [f"{project_col} NOT LIKE ?" for _ in ignore_list]
    params = [f"%{sub}%" for sub in ignore_list]
    return f" {prefix} " + " AND ".join(clauses), params


def _ignore_where_and(
    ignore_list: list[str], project_col: str = "project"
) -> tuple[str, list[str]]:
    """Build SQL AND clause to exclude ignored projects (for appending to existing WHERE)."""
    if not ignore_list:
        return "", []
    clauses = [f"{project_col} NOT LIKE ?" for _ in ignore_list]
    params = [f"%{sub}%" for sub in ignore_list]
    return " AND " + " AND ".join(clauses), params


def query_activity_data(conn: sqlite3.Connection, ignore_list: list[str]) -> list[dict]:
    """Sessions per day for activity heatmap.

    Reads the first line of each JSONL transcript to get the real session
    timestamp, falling back to captured_at for missing/unreadable files.
    Also tracks top 3 projects per day for richer tooltips.
    """
    where, params = _ignore_where(ignore_list)
    rows = conn.execute(
        f"SELECT project, transcript_path, captured_at FROM sessions{where}",
        params,
    ).fetchall()

    day_counts: Counter[str] = Counter()
    day_projects: dict[str, Counter[str]] = {}
    for r in rows:
        day = _session_date(r["transcript_path"], r["captured_at"])
        day_counts[day] += 1
        raw = r["project"] or "?"
        short = raw.rstrip("-").split("-")[-1][:30] or raw[:30]
        day_projects.setdefault(day, Counter())[short] += 1

    result = []
    for d, c in sorted(day_counts.items()):
        top = day_projects[d].most_common(3)
        result.append(
            {"day": d, "count": c, "top": [{"name": n, "count": k} for n, k in top]}
        )
    return result


def _session_date(transcript_path: str | None, fallback_ts: str) -> str:
    """Extract the real session date from a JSONL transcript's first line."""
    if transcript_path:
        try:
            with open(transcript_path) as f:
                import json as _json

                entry = _json.loads(f.readline())
                ts = entry.get("timestamp", "")
                if ts and len(ts) >= 10:
                    return ts[:10]
        except (OSError, ValueError, KeyError):
            pass
    return fallback_ts[:10]


def query_projects(conn: sqlite3.Connection, ignore_list: list[str]) -> list[dict]:
    """Top projects by session count."""
    where, params = _ignore_where(ignore_list)
    rows = conn.execute(
        f"SELECT project, COUNT(*) as count FROM sessions{where} GROUP BY project ORDER BY count DESC LIMIT 15",
        params,
    ).fetchall()
    results = []
    for r in rows:
        raw = r["project"] or "?"
        short = raw.rstrip("-").split("-")[-1][:30] or raw[:30]
        results.append({"project": short, "full": raw, "count": r["count"]})
    return results


def query_tools(conn: sqlite3.Connection, ignore_list: list[str]) -> list[dict]:
    """Tool usage distribution (excluding ignored projects)."""
    where, params = _ignore_where(ignore_list, "s.project")
    rows = conn.execute(
        f"SELECT t.tool_name, SUM(t.count) as total FROM tool_usage t JOIN sessions s ON t.session_id = s.session_id{where} GROUP BY t.tool_name ORDER BY total DESC LIMIT 20",
        params,
    ).fetchall()
    return [{"tool": r["tool_name"], "count": r["total"]} for r in rows]


def query_memories(conn: sqlite3.Connection) -> list[dict]:
    """All memories with metadata."""
    rows = conn.execute(
        "SELECT topic, content, durability, created_at, last_accessed FROM memories ORDER BY durability, last_accessed DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def query_facts_summary(conn: sqlite3.Connection, ignore_list: list[str]) -> dict:
    """Facts grouped by type."""
    where, params = _ignore_where(ignore_list, "s.project")
    rows = conn.execute(
        f"SELECT f.type, COUNT(*) as count FROM facts f JOIN sessions s ON f.session_id = s.session_id{where} GROUP BY f.type ORDER BY count DESC",
        params,
    ).fetchall()
    return {r["type"]: r["count"] for r in rows}


def query_top_files(conn: sqlite3.Connection, ignore_list: list[str]) -> list[dict]:
    """Top files by total touch count."""
    where, params = _ignore_where(ignore_list, "s.project")
    rows = conn.execute(
        f"SELECT ft.path, SUM(ft.count) as total FROM files_touched ft JOIN sessions s ON ft.session_id = s.session_id{where} GROUP BY ft.path ORDER BY total DESC LIMIT 20",
        params,
    ).fetchall()
    results = []
    for r in rows:
        full = r["path"]
        short = full.split("/")[-1] if "/" in full else full
        parent = "/".join(full.split("/")[-3:-1]) if full.count("/") >= 3 else ""
        results.append(
            {"file": short, "parent": parent, "full": full, "count": r["total"]}
        )
    return results


def query_file_coedits(conn: sqlite3.Connection, ignore_list: list[str]) -> list[dict]:
    """File pairs frequently edited in the same session."""
    and_clause, params = _ignore_where_and(ignore_list, "s.project")
    rows = conn.execute(
        f"""
        SELECT a.path as file_a, b.path as file_b, COUNT(DISTINCT a.session_id) as sessions
        FROM files_touched a
        JOIN files_touched b ON a.session_id = b.session_id AND a.path < b.path
        JOIN sessions s ON a.session_id = s.session_id
        WHERE a.action IN ('edit', 'write') AND b.action IN ('edit', 'write'){and_clause}
        GROUP BY a.path, b.path
        HAVING sessions >= 3
        ORDER BY sessions DESC
        LIMIT 20
    """,
        params,
    ).fetchall()
    results = []
    for r in rows:
        a_short = r["file_a"].split("/")[-1]
        b_short = r["file_b"].split("/")[-1]
        results.append(
            {
                "file_a": a_short,
                "file_b": b_short,
                "sessions": r["sessions"],
                "full_a": r["file_a"],
                "full_b": r["file_b"],
            }
        )
    return results


def query_errors(conn: sqlite3.Connection, ignore_list: list[str]) -> list[dict]:
    """Recent errors."""
    where, params = _ignore_where(ignore_list, "s.project")
    rows = conn.execute(
        f"SELECT f.content, f.created_at FROM facts f JOIN sessions s ON f.session_id = s.session_id{where} AND f.type = 'error' ORDER BY f.created_at DESC LIMIT 15",
        params,
    ).fetchall()
    return [{"content": r["content"][:120], "date": r["created_at"][:10]} for r in rows]


def query_patterns(conn: sqlite3.Connection) -> list[dict]:
    """Load patterns from wiki markdown files if they exist."""
    patterns_dir = PATTERNS_DIR / "patterns"
    if not patterns_dir.exists():
        return []
    results = []
    for md in sorted(patterns_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        lines = text.strip().splitlines()
        name = md.stem.replace("-", " ").replace("_", " ").title()
        # Extract frontmatter type if present
        ptype = "unknown"
        strength = ""
        for line in lines:
            if line.startswith("type:"):
                ptype = line.split(":", 1)[1].strip()
            if line.startswith("strength:"):
                strength = line.split(":", 1)[1].strip()
        results.append(
            {"name": name, "type": ptype, "strength": strength, "file": md.name}
        )
    return results


def query_project_profiles(
    conn: sqlite3.Connection, ignore_list: list[str]
) -> list[dict]:
    """Build a narrative profile for each active project."""
    where, params = _ignore_where(ignore_list)
    projects = conn.execute(
        f"SELECT project, COUNT(*) as sessions FROM sessions{where} GROUP BY project ORDER BY sessions DESC LIMIT 12",
        params,
    ).fetchall()

    profiles = []
    for p in projects:
        slug = p["project"]
        short = slug.rstrip("-").split("-")[-1][:30] or slug[:30]

        # Recent session topics
        topics = conn.execute(
            "SELECT topic, branch, captured_at FROM sessions WHERE project = ? AND topic IS NOT NULL ORDER BY captured_at DESC LIMIT 5",
            (slug,),
        ).fetchall()

        # Handoff memory
        handoff_topic = "handoff_" + slug.lower().replace("-", "_").strip("_")
        handoff = conn.execute(
            "SELECT content FROM memories WHERE topic = ?", (handoff_topic,)
        ).fetchone()

        # Related memories (ephemeral from this project)
        related_mems = conn.execute(
            "SELECT m.topic, m.content, m.durability FROM memories m LEFT JOIN sessions s ON m.source_session = s.session_id WHERE s.project = ? AND m.topic != ? ORDER BY m.last_accessed DESC LIMIT 5",
            (slug, handoff_topic),
        ).fetchall()

        # Top files in this project
        top_files = conn.execute(
            "SELECT ft.path, SUM(ft.count) as total FROM files_touched ft WHERE ft.session_id IN (SELECT session_id FROM sessions WHERE project = ?) GROUP BY ft.path ORDER BY total DESC LIMIT 8",
            (slug,),
        ).fetchall()

        # Tech stack from file extensions
        extensions = conn.execute(
            "SELECT ft.path FROM files_touched ft WHERE ft.session_id IN (SELECT session_id FROM sessions WHERE project = ?)",
            (slug,),
        ).fetchall()
        ext_counter: Counter = Counter()
        for row in extensions:
            path = row["path"]
            if "." in path.split("/")[-1]:
                ext = "." + path.split("/")[-1].rsplit(".", 1)[-1]
                ext_counter[ext] += 1
        tech_stack = [ext for ext, _ in ext_counter.most_common(6)]

        # Recent errors
        errs = conn.execute(
            "SELECT f.content FROM facts f WHERE f.session_id IN (SELECT session_id FROM sessions WHERE project = ?) AND f.type = 'error' ORDER BY f.created_at DESC LIMIT 3",
            (slug,),
        ).fetchall()

        profiles.append(
            {
                "slug": slug,
                "name": short,
                "sessions": p["sessions"],
                "handoff": handoff["content"] if handoff else None,
                "topics": [
                    {
                        "topic": t["topic"],
                        "branch": t["branch"],
                        "date": t["captured_at"][:10],
                    }
                    for t in topics
                ],
                "memories": [
                    {
                        "topic": m["topic"],
                        "content": m["content"][:100],
                        "durability": m["durability"],
                    }
                    for m in related_mems
                ],
                "top_files": [
                    {
                        "file": f["path"].split("/")[-1],
                        "full": f["path"],
                        "count": f["total"],
                    }
                    for f in top_files
                ],
                "tech_stack": tech_stack,
                "errors": [e["content"][:100] for e in errs],
            }
        )

    return profiles


def query_stats(conn: sqlite3.Connection, ignore_list: list[str]) -> dict:
    """General stats."""
    where, params = _ignore_where(ignore_list)
    sessions = conn.execute(
        f"SELECT COUNT(*) as c FROM sessions{where}", params
    ).fetchone()["c"]
    projects = conn.execute(
        f"SELECT COUNT(DISTINCT project) as c FROM sessions{where}", params
    ).fetchone()["c"]
    where_ft, params_ft = _ignore_where(ignore_list, "s.project")
    files = conn.execute(
        f"SELECT COUNT(DISTINCT ft.path) as c FROM files_touched ft JOIN sessions s ON ft.session_id = s.session_id{where_ft}",
        params_ft,
    ).fetchone()["c"]
    facts = conn.execute(
        f"SELECT COUNT(*) as c FROM facts f JOIN sessions s ON f.session_id = s.session_id{where_ft}",
        params_ft,
    ).fetchone()["c"]
    memories = conn.execute("SELECT COUNT(*) as c FROM memories").fetchone()["c"]
    return {
        "sessions": sessions,
        "projects": projects,
        "files": files,
        "facts": facts,
        "memories": memories,
    }


PROJECT_COLORS = [
    "#2563eb",
    "#7c3aed",
    "#0891b2",
    "#16a34a",
    "#ca8a04",
    "#dc2626",
    "#4f46e5",
    "#0d9488",
    "#c026d3",
    "#ea580c",
    "#059669",
    "#6366f1",
]


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_memories_html(memories: list[dict]) -> str:
    if not memories:
        return '<div class="empty-state">No memories yet — they appear after your first compaction</div>'
    durable = [m for m in memories if m["durability"] == "durable"]
    ephemeral = [m for m in memories if m["durability"] == "ephemeral"]
    html = ""
    if durable:
        html += '<div class="mem-section-label">Preferences &amp; Practices</div>'
        for m in durable:
            html += f'<div class="mem-row"><span class="pill pill-durable">durable</span><span class="mem-topic">{_html_escape(m["topic"])}</span><span class="mem-content">{_html_escape(m["content"][:90])}</span></div>'
    if ephemeral:
        html += '<div class="mem-section-label" style="margin-top:16px">Active Context</div>'
        for m in ephemeral:
            html += f'<div class="mem-row"><span class="pill pill-ephemeral">ephemeral</span><span class="mem-topic">{_html_escape(m["topic"])}</span><span class="mem-content">{_html_escape(m["content"][:90])}</span></div>'
    return html


def _build_profiles_html(profiles: list[dict]) -> str:
    if not profiles:
        return ""
    html = ""
    for i, p in enumerate(profiles):
        color = PROJECT_COLORS[i % len(PROJECT_COLORS)]
        tech = (
            " ".join(f'<span class="tech-pill">{ext}</span>' for ext in p["tech_stack"])
            if p["tech_stack"]
            else ""
        )

        handoff_html = ""
        if p["handoff"]:
            handoff_html = f'<div class="handoff-block"><div class="handoff-label">HANDOFF</div><div class="handoff-text">{_html_escape(p["handoff"][:300])}</div></div>'

        topics_html = ""
        if p["topics"]:
            topics_html = '<div class="profile-section"><div class="section-label">Recent Sessions</div>'
            for t in p["topics"]:
                branch = (
                    f'<span class="branch-tag">{_html_escape(t["branch"])}</span>'
                    if t["branch"]
                    else ""
                )
                topics_html += f'<div class="topic-row"><span class="topic-date">{t["date"]}</span>{branch}<span class="topic-text">{_html_escape((t["topic"] or "")[:75])}</span></div>'
            topics_html += "</div>"

        files_html = ""
        if p["top_files"]:
            files_html = '<div class="profile-section"><div class="section-label">Most Touched</div>'
            for f in p["top_files"][:6]:
                pct = min(
                    100, int(f["count"] / max(1, p["top_files"][0]["count"]) * 100)
                )
                files_html += f'<div class="file-bar-row"><span class="file-name">{_html_escape(f["file"])}</span><div class="file-bar-track"><div class="file-bar-fill" style="width:{pct}%;background:{color}40;border-left:2px solid {color}"></div></div><span class="file-count">{f["count"]}</span></div>'
            files_html += "</div>"

        errors_html = ""
        if p["errors"]:
            errors_html = '<div class="profile-section"><div class="section-label err-label">Errors</div>'
            for e in p["errors"]:
                errors_html += f'<div class="err-line">{_html_escape(e)}</div>'
            errors_html += "</div>"

        html += f"""<div class="profile-card">
<div class="profile-header"><div class="profile-dot" style="background:{color}"></div><h3 class="profile-name">{_html_escape(p["name"])}</h3><span class="profile-count">{p["sessions"]}</span></div>
<div class="tech-row">{tech}</div>
{handoff_html}{topics_html}{files_html}{errors_html}
</div>"""
    return html


def _build_html(
    stats,
    projects,
    tools,
    memories,
    facts_summary,
    top_files,
    coedits,
    errors,
    patterns,
    profiles,
    activity,
) -> str:
    durable_count = sum(1 for m in memories if m["durability"] == "durable")
    ephemeral_count = sum(1 for m in memories if m["durability"] == "ephemeral")
    pattern_count = len(patterns)

    # ── Activity heatmap (GitHub-style, last 52 weeks) ──
    activity_map = {a["day"]: a["count"] for a in activity}
    today = date.today()
    # Start from the Sunday 52 weeks ago
    start = today - timedelta(
        days=today.weekday() + 1 + 52 * 7
    )  # previous Sunday, 52 weeks back
    if start.weekday() != 6:
        start -= timedelta(days=(start.weekday() + 1) % 7)

    # Color levels for the heatmap
    heatmap_colors = ["var(--surface2)", "#dbeafe", "#93c5fd", "#3b82f6", "#1d4ed8"]
    max_count = max((a["count"] for a in activity), default=1) or 1

    def _heat_color(count):
        if count == 0:
            return heatmap_colors[0]
        ratio = count / max_count
        if ratio <= 0.25:
            return heatmap_colors[1]
        elif ratio <= 0.5:
            return heatmap_colors[2]
        elif ratio <= 0.75:
            return heatmap_colors[3]
        return heatmap_colors[4]

    # Build weeks grid
    day_names = ["", "Mon", "", "Wed", "", "Fri", ""]
    heatmap_labels_html = "".join(
        f'<div class="heatmap-label">{d}</div>' for d in day_names
    )

    weeks_html = ""
    month_markers = []
    current = start
    week_idx = 0
    while current <= today:
        week_html = ""
        for dow in range(7):
            d = current + timedelta(days=dow)
            if d > today:
                week_html += '<div class="heatmap-day" style="visibility:hidden"></div>'
            else:
                ds = d.isoformat()
                c = activity_map.get(ds, 0)
                bg = _heat_color(c)
                week_html += f'<div class="heatmap-day" style="background:{bg}" data-count="{c}" data-date="{ds}"></div>'
            # Track first day of each month for labels
            if dow == 0 and d.day <= 7 and d <= today:
                month_markers.append((week_idx, d.strftime("%b")))
        weeks_html += f'<div class="heatmap-week">{week_html}</div>'
        current += timedelta(days=7)
        week_idx += 1

    # Build month labels positioned by week index
    months_html = ""
    prev_end = 0
    for widx, mname in month_markers:
        offset_px = widx * 16  # 13px box + 3px gap
        if offset_px > prev_end + 20:
            months_html += f'<div class="heatmap-month" style="position:absolute;left:{offset_px}px">{mname}</div>'
            prev_end = offset_px + 24

    total_sessions_year = sum(
        a["count"] for a in activity if a["day"] >= start.isoformat()
    )
    heatmap_html = f"""<div class="heatmap-wrap">
<div style="position:relative;height:18px;margin-left:28px;margin-bottom:6px">{months_html}</div>
<div style="display:flex">
<div class="heatmap-labels">{heatmap_labels_html}</div>
<div class="heatmap-grid">{weeks_html}</div>
</div>
<div class="heatmap-legend">
<span>{total_sessions_year} sessions in the last year</span>
<span style="margin-left:12px">Less</span>
<div class="heatmap-legend-box" style="background:{heatmap_colors[0]}"></div>
<div class="heatmap-legend-box" style="background:{heatmap_colors[1]}"></div>
<div class="heatmap-legend-box" style="background:{heatmap_colors[2]}"></div>
<div class="heatmap-legend-box" style="background:{heatmap_colors[3]}"></div>
<div class="heatmap-legend-box" style="background:{heatmap_colors[4]}"></div>
<span>More</span>
</div>
</div>"""

    facts_pills = ""
    fact_colors = {
        "error": "#dc2626",
        "decision": "#2563eb",
        "correction": "#ca8a04",
        "topic": "#7c3aed",
    }
    for ftype, fcount in facts_summary.items():
        fc = fact_colors.get(ftype, "#8b949e")
        facts_pills += f'<div class="fact-pill" style="--fc:{fc}"><span class="fact-dot" style="background:{fc}"></span><span class="fact-name">{ftype}</span><span class="fact-count">{fcount}</span></div>'

    coedits_html = ""
    if coedits:
        for c in coedits[:12]:
            coedits_html += f'<div class="coedit-row"><span class="coedit-file">{_html_escape(c["file_a"])}</span><span class="coedit-arrow">&harr;</span><span class="coedit-file">{_html_escape(c["file_b"])}</span><span class="coedit-count">{c["sessions"]}x</span></div>'
    else:
        coedits_html = (
            '<div class="empty-state">Detected after 3+ shared edit sessions</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>engram</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
:root {{
    --bg: #ffffff;
    --surface: #f8fafc;
    --surface2: #f1f5f9;
    --border: #e2e8f0;
    --text: #0f172a;
    --text2: #334155;
    --text-muted: #94a3b8;
    --accent: #2563eb;
    --accent2: #3b82f6;
    --blue: #2563eb;
    --green: #16a34a;
    --red: #dc2626;
    --purple: #7c3aed;
    --radius: 12px;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }}

/* ── Hero ── */
.hero {{
    position:relative;
    overflow:hidden;
    padding:80px 48px 60px;
    text-align:center;
    background:linear-gradient(180deg, #eff6ff 0%, var(--bg) 100%);
}}
.hero::before {{
    content:'';
    position:absolute;
    top:-50%;left:-50%;width:200%;height:200%;
    background:radial-gradient(circle at 50% 0%, rgba(37,99,235,0.06) 0%, transparent 50%);
    pointer-events:none;
}}
.hero-title {{
    font-size:56px;
    font-weight:800;
    letter-spacing:-2px;
    margin-bottom:8px;
    background:linear-gradient(135deg, #2563eb, #7c3aed);
    -webkit-background-clip:text;
    -webkit-text-fill-color:transparent;
}}
.hero-sub {{
    font-size:20px;
    color:var(--text-muted);
    font-weight:400;
    max-width:500px;
    margin:0 auto 40px;
}}

/* ── Stats ── */
.stats-grid {{
    display:flex;
    justify-content:center;
    gap:32px;
    flex-wrap:wrap;
}}
.stat {{
    text-align:center;
    min-width:100px;
}}
.stat-num {{
    font-size:36px;
    font-weight:800;
    color:var(--text);
    line-height:1;
}}
.stat-label {{
    font-size:11px;
    color:var(--text-muted);
    text-transform:uppercase;
    letter-spacing:1.5px;
    margin-top:6px;
    font-weight:500;
}}

/* ── Container ── */
.container {{ max-width:1200px; margin:0 auto; padding:0 32px; }}

/* ── Section headings ── */
.section-heading {{
    font-size:36px;
    font-weight:800;
    letter-spacing:-1px;
    margin:64px 0 12px;
}}
.section-desc {{
    font-size:15px;
    color:var(--text-muted);
    margin-bottom:32px;
    max-width:600px;
}}

/* ── Cards grid ── */
.cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(420px, 1fr)); gap:20px; margin-bottom:48px; }}
.card {{
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:var(--radius);
    padding:28px;
    transition:border-color 0.2s;
}}
.card:hover {{ border-color:#cbd5e1; }}
.card-title {{
    font-size:13px;
    font-weight:600;
    text-transform:uppercase;
    letter-spacing:1px;
    color:var(--text-muted);
    margin-bottom:20px;
}}
.chart-box {{ position:relative; height:280px; }}

/* ── Facts pills (claude-mem style) ── */
.facts-grid {{ display:flex; flex-wrap:wrap; gap:12px; }}
.fact-pill {{
    display:flex; align-items:center; gap:10px;
    background:var(--surface2);
    border:1px solid var(--border);
    border-radius:999px;
    padding:10px 20px;
    transition:border-color 0.2s;
}}
.fact-pill:hover {{ border-color:var(--fc); }}
.fact-dot {{ width:10px; height:10px; border-radius:50%; flex-shrink:0; }}
.fact-name {{ font-size:14px; font-weight:500; color:var(--text2); }}
.fact-count {{ font-size:14px; font-weight:700; color:var(--text); margin-left:4px; }}

/* ── Memories ── */
.mem-section-label {{ font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:1px; color:var(--text-muted); margin-bottom:10px; }}
.mem-row {{ display:flex; align-items:baseline; gap:10px; padding:8px 0; border-bottom:1px solid var(--border); font-size:13px; }}
.mem-row:last-child {{ border-bottom:none; }}
.pill {{
    display:inline-flex; padding:2px 10px; border-radius:999px;
    font-size:11px; font-weight:600; flex-shrink:0;
}}
.pill-durable {{ background:rgba(22,163,74,0.1); color:var(--green); }}
.pill-ephemeral {{ background:rgba(37,99,235,0.1); color:var(--accent); }}
.mem-topic {{ font-family:'JetBrains Mono',monospace; color:var(--blue); font-size:12px; flex-shrink:0; }}
.mem-content {{ color:var(--text-muted); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

/* ── Co-edits ── */
.coedit-row {{ display:flex; align-items:center; gap:10px; padding:7px 0; border-bottom:1px solid var(--border); font-size:13px; }}
.coedit-row:last-child {{ border-bottom:none; }}
.coedit-file {{ font-family:'JetBrains Mono',monospace; color:var(--blue); font-size:12px; }}
.coedit-arrow {{ color:var(--text-muted); font-size:11px; }}
.coedit-count {{ margin-left:auto; color:var(--text-muted); font-size:12px; font-weight:600; }}

/* ── Profile cards ── */
.profiles-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(380px, 1fr)); gap:20px; margin-bottom:48px; }}
.profile-card {{
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:var(--radius);
    padding:28px;
    transition:border-color 0.2s;
}}
.profile-card:hover {{ border-color:#cbd5e1; }}
.profile-header {{ display:flex; align-items:center; gap:12px; margin-bottom:14px; }}
.profile-dot {{ width:12px; height:12px; border-radius:50%; flex-shrink:0; }}
.profile-name {{ font-size:20px; font-weight:700; letter-spacing:-0.5px; flex:1; }}
.profile-count {{ font-size:24px; font-weight:800; color:var(--text-muted); }}
.tech-row {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; }}
.tech-pill {{
    padding:3px 10px; border-radius:999px; font-size:11px; font-weight:500;
    background:var(--surface2); border:1px solid var(--border); color:var(--text2);
}}
.handoff-block {{
    background:linear-gradient(135deg, rgba(22,163,74,0.06), rgba(22,163,74,0.02));
    border-left:3px solid var(--green);
    border-radius:0 8px 8px 0;
    padding:14px 16px;
    margin-bottom:16px;
}}
.handoff-label {{ font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:1.5px; color:var(--green); margin-bottom:6px; }}
.handoff-text {{ font-size:13px; color:var(--text2); line-height:1.5; }}
.profile-section {{ margin-bottom:14px; }}
.section-label {{ font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:1px; color:var(--text-muted); margin-bottom:8px; }}
.err-label {{ color:var(--red); }}
.topic-row {{ display:flex; align-items:baseline; gap:8px; padding:4px 0; font-size:12px; }}
.topic-date {{ color:var(--text-muted); font-family:'JetBrains Mono',monospace; font-size:11px; flex-shrink:0; }}
.topic-text {{ color:var(--text2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }}
.branch-tag {{ padding:1px 6px; border-radius:4px; font-size:10px; background:rgba(37,99,235,0.08); color:var(--blue); font-family:'JetBrains Mono',monospace; flex-shrink:0; }}
.file-bar-row {{ display:flex; align-items:center; gap:8px; padding:3px 0; font-size:12px; }}
.file-name {{ font-family:'JetBrains Mono',monospace; color:var(--text2); font-size:11px; min-width:120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.file-bar-track {{ flex:1; height:6px; background:var(--surface2); border-radius:3px; overflow:hidden; }}
.file-bar-fill {{ height:100%; border-radius:3px; }}
.file-count {{ font-family:'JetBrains Mono',monospace; color:var(--text-muted); font-size:11px; min-width:28px; text-align:right; }}
.err-line {{ font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--red); padding:3px 0; word-break:break-all; opacity:0.8; }}

.empty-state {{ color:var(--text-muted); font-size:13px; font-style:italic; padding:20px 0; }}

/* ── Activity Heatmap ── */
.heatmap-wrap {{ margin-bottom:48px; overflow-x:auto; }}
.heatmap-grid {{ display:flex; gap:3px; }}
.heatmap-week {{ display:flex; flex-direction:column; gap:3px; }}
.heatmap-day {{
    width:13px; height:13px; border-radius:2px;
    background:var(--surface2);
    transition: outline 0.15s;
}}
.heatmap-day:hover {{ outline:2px solid var(--accent); outline-offset:1px; }}
.heatmap-day[data-count="0"] {{ background:var(--surface2); }}
.heatmap-labels {{ display:flex; gap:3px; flex-direction:column; margin-right:8px; padding-top:0; }}
.heatmap-label {{ height:13px; font-size:9px; color:var(--text-muted); line-height:13px; font-weight:500; }}
.heatmap-months {{ display:flex; gap:3px; margin-left:28px; margin-bottom:6px; }}
.heatmap-month {{ font-size:10px; color:var(--text-muted); font-weight:500; }}
.heatmap-legend {{ display:flex; align-items:center; gap:6px; margin-top:12px; justify-content:flex-end; font-size:11px; color:var(--text-muted); }}
.heatmap-legend-box {{ width:13px; height:13px; border-radius:2px; }}

/* Heatmap Tooltip */
#heatmap-tooltip {{
    position:fixed;
    background:#0f172a;
    color:#fff;
    padding:10px 14px;
    border-radius:8px;
    font-size:12px;
    font-family:'Inter',system-ui,sans-serif;
    box-shadow:0 8px 20px rgba(15,23,42,0.25);
    pointer-events:none;
    opacity:0;
    transform:translate(-50%, calc(-100% - 10px));
    transition:opacity 0.1s;
    z-index:1000;
    min-width:180px;
    max-width:280px;
}}
#heatmap-tooltip.visible {{ opacity:1; }}
#heatmap-tooltip::after {{
    content:'';
    position:absolute;
    bottom:-6px; left:50%;
    transform:translateX(-50%);
    border:6px solid transparent;
    border-top-color:#0f172a;
    border-bottom:0;
}}
.tt-date {{ font-weight:600; font-size:13px; margin-bottom:4px; }}
.tt-count {{ color:#93c5fd; font-size:11px; margin-bottom:8px; }}
.tt-projects {{ border-top:1px solid rgba(255,255,255,0.1); padding-top:8px; }}
.tt-proj-row {{ display:flex; justify-content:space-between; gap:12px; font-size:11px; padding:2px 0; }}
.tt-proj-name {{ color:#cbd5e1; font-family:'JetBrains Mono',monospace; }}
.tt-proj-count {{ color:#94a3b8; font-weight:600; }}
.tt-empty {{ color:#64748b; font-style:italic; font-size:11px; }}

/* ── Footer ── */
.footer {{
    text-align:center;
    color:var(--text-muted);
    font-size:12px;
    padding:48px 0 32px;
    border-top:1px solid var(--border);
    margin-top:32px;
}}
.footer a {{ color:var(--accent); text-decoration:none; }}
</style>
</head>
<body>

<!-- Hero -->
<div class="hero">
    <div class="hero-title">engram</div>
    <div class="hero-sub">what I've learned about you</div>
    <div class="stats-grid">
        <div class="stat"><div class="stat-num">{stats["sessions"]}</div><div class="stat-label">Sessions</div></div>
        <div class="stat"><div class="stat-num">{stats["projects"]}</div><div class="stat-label">Projects</div></div>
        <div class="stat"><div class="stat-num">{stats["files"]}</div><div class="stat-label">Files</div></div>
        <div class="stat"><div class="stat-num">{durable_count}</div><div class="stat-label">Preferences</div></div>
        <div class="stat"><div class="stat-num">{ephemeral_count}</div><div class="stat-label">Context Notes</div></div>
        <div class="stat"><div class="stat-num">{pattern_count}</div><div class="stat-label">Patterns</div></div>
    </div>
</div>

<div class="container">

<!-- Activity Heatmap -->
<div class="section-heading">Activity</div>
<div class="section-desc">Your coding sessions over the past year.</div>
{heatmap_html}
<div id="heatmap-tooltip"></div>

<!-- How you'd remember it -->
<div class="section-heading">How you'd remember it&mdash;<br>for your AI.</div>
<div class="section-desc">Every fact is auto-categorized. Filter by errors, decisions, or corrections.</div>
<div class="facts-grid" style="margin-bottom:48px">{facts_pills}</div>

<!-- Charts -->
<div class="cards">
<div class="card">
    <div class="card-title">Top Projects</div>
    <div class="chart-box"><canvas id="projectsChart"></canvas></div>
</div>
<div class="card">
    <div class="card-title">Tool Usage</div>
    <div class="chart-box"><canvas id="toolsChart"></canvas></div>
</div>
</div>

<!-- Memories -->
<div class="section-heading">Your AI's memory.</div>
<div class="section-desc">Preferences persist forever. Context notes expire in 7 days.</div>
<div class="card" style="margin-bottom:48px">
    {_build_memories_html(memories)}
</div>

<!-- Co-edits & Files -->
<div class="cards">
<div class="card">
    <div class="card-title">File Co-edits &mdash; always edited together</div>
    {coedits_html}
</div>
<div class="card">
    <div class="card-title">Most Touched Files</div>
    <div class="chart-box"><canvas id="filesChart"></canvas></div>
</div>
</div>

<!-- Project Profiles -->
<div class="section-heading">Project Profiles</div>
<div class="section-desc">What Claude sees when opening each project &mdash; handoff, context, files, errors.</div>
<div class="profiles-grid">
{_build_profiles_html(profiles)}
</div>

</div>

<div class="footer">
    <strong>engram</strong> &mdash; persistent memory for Claude Code &middot; all data from <code>~/.claude/memory.db</code><br>
    <a href="https://github.com/sebastianbreguel/engram">github.com/sebastianbreguel/engram</a>
</div>

<script>
Chart.defaults.color = '#64748b';
Chart.defaults.borderColor = '#e2e8f000';
Chart.defaults.font.family = "'Inter', system-ui, sans-serif";

const PC = {json.dumps(PROJECT_COLORS)};

const projectsData = {json.dumps(projects)};
new Chart(document.getElementById('projectsChart'), {{
    type: 'bar',
    data: {{
        labels: projectsData.map(p => p.project),
        datasets: [{{
            data: projectsData.map(p => p.count),
            backgroundColor: projectsData.map((_,i) => PC[i % PC.length] + '33'),
            borderColor: projectsData.map((_,i) => PC[i % PC.length]),
            borderWidth: 1,
            borderRadius: 6,
            borderSkipped: false,
        }}]
    }},
    options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            x: {{ grid: {{ color: '#e2e8f066' }}, ticks: {{ font: {{ size: 11 }} }} }},
            y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 12, weight: 500 }} }} }}
        }}
    }}
}});

const toolsData = {json.dumps(tools[:12])};
new Chart(document.getElementById('toolsChart'), {{
    type: 'bar',
    data: {{
        labels: toolsData.map(t => t.tool),
        datasets: [{{
            data: toolsData.map(t => t.count),
            backgroundColor: toolsData.map((_,i) => PC[(i+3) % PC.length] + '33'),
            borderColor: toolsData.map((_,i) => PC[(i+3) % PC.length]),
            borderWidth: 1,
            borderRadius: 6,
            borderSkipped: false,
        }}]
    }},
    options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            x: {{ grid: {{ color: '#e2e8f066' }}, ticks: {{ font: {{ size: 11 }} }} }},
            y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11, family: "'JetBrains Mono', monospace" }} }} }}
        }}
    }}
}});

const filesData = {json.dumps(top_files[:12])};
new Chart(document.getElementById('filesChart'), {{
    type: 'bar',
    data: {{
        labels: filesData.map(f => f.parent ? f.parent + '/' + f.file : f.file),
        datasets: [{{
            data: filesData.map(f => f.count),
            backgroundColor: '#7c3aed22',
            borderColor: '#7c3aed',
            borderWidth: 1,
            borderRadius: 6,
            borderSkipped: false,
        }}]
    }},
    options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            x: {{ grid: {{ color: '#e2e8f066' }}, ticks: {{ font: {{ size: 11 }} }} }},
            y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10, family: "'JetBrains Mono', monospace" }} }} }}
        }}
    }}
}});

// ── Heatmap tooltip ──
(function() {{
    const activityByDay = {json.dumps({a["day"]: a for a in activity})};
    const tooltip = document.getElementById('heatmap-tooltip');

    function formatDate(dateStr) {{
        const [y, m, d] = dateStr.split('-').map(Number);
        const date = new Date(y, m - 1, d);
        return date.toLocaleDateString('en-US', {{ weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }});
    }}

    document.querySelectorAll('.heatmap-day[data-date]').forEach(cell => {{
        cell.addEventListener('mouseenter', (e) => {{
            const date = cell.dataset.date;
            const count = parseInt(cell.dataset.count, 10);
            const info = activityByDay[date];

            let html = `<div class="tt-date">${{formatDate(date)}}</div>`;
            html += `<div class="tt-count">${{count}} session${{count === 1 ? '' : 's'}}</div>`;

            if (info && info.top && info.top.length > 0) {{
                html += '<div class="tt-projects">';
                info.top.forEach(p => {{
                    html += `<div class="tt-proj-row"><span class="tt-proj-name">${{p.name}}</span><span class="tt-proj-count">${{p.count}}</span></div>`;
                }});
                html += '</div>';
            }} else if (count === 0) {{
                html += '<div class="tt-empty">No activity</div>';
            }}

            tooltip.innerHTML = html;
            tooltip.classList.add('visible');
            positionTooltip(cell);
        }});

        cell.addEventListener('mouseleave', () => {{
            tooltip.classList.remove('visible');
        }});
    }});

    function positionTooltip(cell) {{
        const rect = cell.getBoundingClientRect();
        tooltip.style.left = (rect.left + rect.width / 2) + 'px';
        tooltip.style.top = rect.top + 'px';
    }}
}})();
</script>
</body>
</html>"""


def generate_html(output_path: Path) -> None:
    conn = get_connection()
    ignore_list = load_ignore_list()

    stats = query_stats(conn, ignore_list)
    activity = query_activity_data(conn, ignore_list)
    projects = query_projects(conn, ignore_list)
    tools = query_tools(conn, ignore_list)
    memories = query_memories(conn)
    facts_summary = query_facts_summary(conn, ignore_list)
    top_files = query_top_files(conn, ignore_list)
    coedits = query_file_coedits(conn, ignore_list)
    errors = query_errors(conn, ignore_list)
    patterns = query_patterns(conn)
    profiles = query_project_profiles(conn, ignore_list)

    conn.close()

    html = _build_html(
        stats,
        projects,
        tools,
        memories,
        facts_summary,
        top_files,
        coedits,
        errors,
        patterns,
        profiles,
        activity,
    )
    output_path.write_text(html, encoding="utf-8")
    print(f"Dashboard generated: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="engram dashboard — visualize your session data"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=str(Path.home() / ".claude" / "engram-dashboard.html"),
        help="Output HTML file path (default: ~/.claude/engram-dashboard.html)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Generate without opening in browser",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}")
        print("Run a few Claude Code sessions first, then try again.")
        raise SystemExit(1)

    output = Path(args.output)
    generate_html(output)

    if not args.no_open:
        webbrowser.open(f"file://{output}")


if __name__ == "__main__":
    main()
