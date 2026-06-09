#!/usr/bin/env python3
"""
Jira → FS Portfolio Dashboard sync script.

Fetches current status, progress, and lastUpdated for each tracked epic
from the TVG Jira REST API, then patches index.html in place so GitHub
Pages redeploys with fresh data.

Required environment variables:
  JIRA_EMAIL      - Your TVG email (e.g. paul.mcwilliam@theverygroup.com)
  JIRA_API_TOKEN  - Jira API token from https://id.atlassian.com/manage-profile/security/api-tokens
  DRY_RUN         - Optional: 'true' to print changes without writing (default: false)
"""

import os
import re
import json
import sys
import requests
from datetime import datetime, timezone

# ── Config ───────────────────────────────────────────────────────────────────

JIRA_BASE   = "https://theverygroup.atlassian.net"
JIRA_EMAIL  = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN  = os.environ.get("JIRA_API_TOKEN", "")
DRY_RUN     = os.environ.get("DRY_RUN", "false").lower() == "true"
HTML_PATH   = "index.html"
RESULT_PATH = ".github/sync_result.md"

EPIC_IDS = ["DPD-757", "DPD-754", "DPD-905", "DPD-901", "DPD-915"]

# Map Jira status category/name → dashboard display label
STATUS_MAP = {
    # Category: new
    "To Do":                    "Not Started",
    "Backlog":                  "Not Started",
    "Selected for Development": "Not Started",
    # Category: indeterminate
    "In Progress":              "In Progress",
    "In Review":                "In Progress",
    "Review":                   "In Progress",
    "In Development":           "In Progress",
    "Testing":                  "In Progress",
    "UAT":                      "In Progress",
    "Ready for Release":        "In Progress",
    # Category: done
    "Done":                     "Complete",
    "Closed":                   "Complete",
    "Resolved":                 "Complete",
    "Cancelled":                "Complete",
    "Won't Do":                 "Complete",
    # At risk
    "Blocked":                  "At Risk",
    "On Hold":                  "At Risk",
    "Impediment":               "At Risk",
}


# ── Jira API helpers ──────────────────────────────────────────────────────────

def jira_get(path: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{JIRA_BASE}{path}",
        auth=(JIRA_EMAIL, JIRA_TOKEN),
        params=params,
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_progress_from_children(epic_id: str) -> int | None:
    """
    Count child issues (via both classic Epic Link and next-gen parent)
    and return (done / total) * 100.  Returns None if no children found.
    """
    try:
        # Try classic epic-link JQL first, then next-gen parent
        for jql in [
            f'"Epic Link" = {epic_id}',
            f'parent = {epic_id}',
        ]:
            data = jira_get(
                "/rest/api/3/search",
                params={
                    "jql": jql,
                    "fields": "status",
                    "maxResults": 500,
                },
            )
            issues = data.get("issues", [])
            if issues:
                total = len(issues)
                done  = sum(
                    1 for i in issues
                    if i["fields"]["status"]["statusCategory"]["key"] == "done"
                )
                pct = round((done / total) * 100)
                print(f"    child issues: {done}/{total} done → {pct}%")
                return pct
    except Exception as exc:
        print(f"    child-issue progress unavailable: {exc}")
    return None


def fetch_epic(epic_id: str) -> dict:
    print(f"\nFetching {epic_id} …")
    data = jira_get(
        f"/rest/api/3/issue/{epic_id}",
        params={
            "fields": (
                "summary,status,updated,assignee,progress,"
                "duedate,customfield_10015,"   # TVG target end date
                "customfield_10016,"            # story points
                "customfield_10014"             # epic name (cloud)
            )
        },
    )
    fields = data["fields"]

    # Status ─────────────────────────────────────────────────────────────────
    raw_status  = fields.get("status", {}).get("name", "In Progress")
    mapped_status = STATUS_MAP.get(raw_status, "In Progress")
    print(f"  status:      {raw_status!r} → {mapped_status!r}")

    # Last updated ────────────────────────────────────────────────────────────
    updated_raw  = fields.get("updated", "")
    last_updated = updated_raw[:10] if updated_raw else None      # YYYY-MM-DD
    print(f"  lastUpdated: {last_updated}")

    # Progress ────────────────────────────────────────────────────────────────
    # 1. Try Jira's built-in progress object (hours-based)
    progress = None
    prog_obj = fields.get("progress") or {}
    if prog_obj.get("total", 0) > 0:
        progress = round(prog_obj.get("progress", 0) / prog_obj["total"] * 100)
        print(f"  progress:    {progress}% (from Jira progress object)")

    # 2. Fall back to counting child issues
    if progress is None:
        progress = get_progress_from_children(epic_id)
        if progress is not None:
            print(f"  progress:    {progress}% (from child issue count)")

    if progress is None:
        print("  progress:    unchanged (no data available)")

    # Due / milestone date ────────────────────────────────────────────────────
    # Try duedate, then TVG customfield_10015 (target end), keep existing if both null
    next_milestone = fields.get("duedate") or fields.get("customfield_10015")
    if next_milestone:
        # Normalise to YYYY-MM-DD (some fields return datetime strings)
        next_milestone = str(next_milestone)[:10]
    print(f"  nextMilestone: {next_milestone or 'unchanged'}")

    return {
        "id":            epic_id,
        "status":        mapped_status,
        "lastUpdated":   last_updated,
        "progress":      progress,
        "nextMilestone": next_milestone,
    }


# ── HTML patch helpers ────────────────────────────────────────────────────────

def find_epic_block(html: str, epic_id: str) -> tuple[int, int] | tuple[None, None]:
    """
    Return (start, end) byte offsets for the JS object that contains
    id: '<epic_id>'.  Walks braces to find the enclosing { … }.
    """
    m = re.search(rf"id:\s*'{re.escape(epic_id)}'", html)
    if not m:
        return None, None

    # Walk back to the opening brace of this epic object
    pos = m.start()
    while pos > 0 and html[pos] != "{":
        pos -= 1
    start = pos

    # Walk forward tracking depth
    depth = 0
    pos   = start
    while pos < len(html):
        if html[pos] == "{":
            depth += 1
        elif html[pos] == "}":
            depth -= 1
            if depth == 0:
                return start, pos + 1
        pos += 1

    return None, None


def patch_str_field(block: str, field: str, new_val: str) -> str:
    """Replace  field: 'old'  →  field: 'new'  inside an epic block."""
    pattern     = rf"({re.escape(field)}:\s*')(.*?)(')"
    replacement = rf"\g<1>{new_val}\g<3>"
    new_block, n = re.subn(pattern, replacement, block, count=1, flags=re.DOTALL)
    return new_block if n else block


def patch_num_field(block: str, field: str, new_val: int) -> str:
    """Replace  field: 65  →  field: 72  inside an epic block."""
    pattern     = rf"({re.escape(field)}:\s*)(\d+)"
    replacement = rf"\g<1>{new_val}"
    new_block, n = re.subn(pattern, replacement, block, count=1)
    return new_block if n else block


def patch_sync_timestamp(html: str, ts: str) -> str:
    """Update or insert the LAST_JIRA_SYNC JS constant."""
    const_line = f"const LAST_JIRA_SYNC = '{ts}';"
    if "const LAST_JIRA_SYNC" in html:
        html = re.sub(r"const LAST_JIRA_SYNC = '[^']*';", const_line, html)
    else:
        # Insert just before the TODAY constant
        html = html.replace(
            "const TODAY = new Date();",
            f"{const_line}\nconst TODAY = new Date();"
        )
    return html


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if not JIRA_EMAIL or not JIRA_TOKEN:
        print("ERROR: JIRA_EMAIL and JIRA_API_TOKEN environment variables are required.")
        return 1

    if DRY_RUN:
        print("DRY RUN mode — no files will be written.\n")

    # Read current HTML
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    results  = []
    changes  = []
    n_errors = 0

    for epic_id in EPIC_IDS:
        try:
            data = fetch_epic(epic_id)
        except requests.HTTPError as exc:
            print(f"  HTTP error fetching {epic_id}: {exc}")
            results.append({"id": epic_id, "error": str(exc)})
            n_errors += 1
            continue
        except Exception as exc:
            print(f"  Unexpected error fetching {epic_id}: {exc}")
            results.append({"id": epic_id, "error": str(exc)})
            n_errors += 1
            continue

        results.append(data)

        # Locate the epic block in the HTML
        start, end = find_epic_block(html, epic_id)
        if start is None:
            print(f"  WARNING: could not locate {epic_id} block in index.html")
            continue

        original_block = html[start:end]
        block          = original_block

        # Patch fields (only when we have fresh data)
        if data.get("lastUpdated"):
            block = patch_str_field(block, "lastUpdated", data["lastUpdated"])

        if data.get("status"):
            block = patch_str_field(block, "status", data["status"])

        if data.get("progress") is not None:
            block = patch_num_field(block, "progress", data["progress"])

        if data.get("nextMilestone"):
            block = patch_str_field(block, "nextMilestone", data["nextMilestone"])

        if block != original_block:
            html = html[:start] + block + html[end:]
            changes.append(epic_id)
            print(f"  ✓ patched {epic_id}")
        else:
            print(f"  – no changes for {epic_id}")

    # Update sync timestamp
    sync_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    html    = patch_sync_timestamp(html, sync_ts)

    # Write HTML
    if not DRY_RUN:
        with open(HTML_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nindex.html written. Changed epics: {changes or 'none'}")
    else:
        print(f"\n[DRY RUN] Would update: {changes or 'none'}")

    # Write GitHub Actions step summary
    summary_lines = [
        f"| Epic | Status | Progress | Updated | Milestone |",
        f"|------|--------|----------|---------|-----------|",
    ]
    for r in results:
        if "error" in r:
            summary_lines.append(f"| {r['id']} | ❌ {r['error'][:60]} | — | — | — |")
        else:
            prog  = f"{r['progress']}%" if r.get("progress") is not None else "—"
            ms    = r.get("nextMilestone") or "—"
            upd   = r.get("lastUpdated")   or "—"
            summary_lines.append(
                f"| {r['id']} | {r.get('status','—')} | {prog} | {upd} | {ms} |"
            )

    summary = "\n".join([
        f"**Sync time:** {sync_ts}",
        f"**Epics updated:** {', '.join(changes) if changes else 'none (data unchanged)'}",
        f"**Errors:** {n_errors}",
        "",
        *summary_lines,
    ])

    os.makedirs(".github", exist_ok=True)
    with open(RESULT_PATH, "w") as f:
        f.write(summary)

    print("\n" + summary)
    return 0 if n_errors < len(EPIC_IDS) else 1   # partial success is still ok


if __name__ == "__main__":
    sys.exit(main())
