#!/usr/bin/env python3
"""
Report generator — Monday digest and Friday SteerCo report.

Set REPORT_MODE=digest or REPORT_MODE=steerco before running.

Reads epic data from index.html, calls Claude API, saves report
to reports/ directory, and optionally emails it via SMTP.

Required:
  ANTHROPIC_API_KEY  — Anthropic API key
  REPORT_MODE        — 'digest' or 'steerco'

Optional (for email sending):
  EMAIL_TO           — recipient address (e.g. paul.mcwilliam@theverygroup.com)
  SMTP_SERVER        — e.g. smtp.gmail.com
  SMTP_PORT          — e.g. 587
  SMTP_USER          — sender email
  SMTP_PASS          — app password / SMTP password
"""

import os, re, sys, json, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REPORT_MODE   = os.environ.get("REPORT_MODE", "digest")
DRY_RUN       = os.environ.get("DRY_RUN", "false").lower() == "true"
EMAIL_TO      = os.environ.get("EMAIL_TO", "")
SMTP_SERVER   = os.environ.get("SMTP_SERVER", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASS     = os.environ.get("SMTP_PASS", "")

HTML_PATH     = "index.html"
REPORTS_DIR   = "reports"
RESULT_PATH   = ".github/report_result.md"

TODAY         = datetime.now(timezone.utc)
TODAY_STR     = TODAY.strftime("%Y-%m-%d")
TODAY_UK      = TODAY.strftime("%d %b %Y")
WEEK_NUM      = TODAY.strftime("%Y-W%V")


# ── Parse epic data from index.html ──────────────────────────────────────────

def parse_epics(html: str) -> list[dict]:
    """Extract EPICS array data from the dashboard HTML."""
    epics = []
    for m in re.finditer(r"id:\s*'(DPD-\d+)'", html):
        epic_id = m.group(1)
        start, end = _find_block(html, m.start())
        if start is None:
            continue
        block = html[start:end]

        def extract_str(field):
            r = re.search(rf"{re.escape(field)}:\s*'([^']*)'", block)
            return r.group(1) if r else ""

        def extract_num(field):
            r = re.search(rf"{re.escape(field)}:\s*(\d+)", block)
            return int(r.group(1)) if r else 0

        decisions = []
        for dm in re.finditer(r"\{[^}]*status:\s*'(open|blocked|resolved)'[^}]*\}", block):
            db = dm.group(0)
            txt = re.search(r"text:\s*'([^']*)'", db)
            sts = re.search(r"status:\s*'([^']*)'", db)
            dte = re.search(r"date:\s*'([^']*)'", db)
            own = re.search(r"owner:\s*'([^']*)'", db)
            if txt:
                decisions.append({
                    "text":   txt.group(1),
                    "status": sts.group(1) if sts else "",
                    "date":   dte.group(1) if dte else "",
                    "owner":  own.group(1) if own else "",
                })

        epics.append({
            "id":             epic_id,
            "name":           extract_str("name"),
            "progress":       extract_num("progress"),
            "lastUpdated":    extract_str("lastUpdated"),
            "nextMilestone":  extract_str("nextMilestone"),
            "milestoneLabel": extract_str("milestoneLabel"),
            "status":         extract_str("status"),
            "notes":          extract_str("notes"),
            "decisions":      decisions,
        })
    return epics


def _find_block(html, search_start):
    pos = search_start
    while pos > 0 and html[pos] != "{":
        pos -= 1
    start = pos
    depth, pos = 0, start
    while pos < len(html):
        if html[pos] == "{":
            depth += 1
        elif html[pos] == "}":
            depth -= 1
            if depth == 0:
                return start, pos + 1
        pos += 1
    return None, None


def calc_rag(epic: dict) -> str:
    """Reproduce the dashboard's calcRAG logic."""
    try:
        last = datetime.fromisoformat(epic["lastUpdated"])
        stale = (TODAY - last.replace(tzinfo=timezone.utc)).days
    except Exception:
        stale = 999
    try:
        nxt = datetime.fromisoformat(epic["nextMilestone"])
        due_in = (nxt.replace(tzinfo=timezone.utc) - TODAY).days
    except Exception:
        due_in = None

    if stale > 60 or (due_in is not None and due_in <= 7):
        return "RED"
    if stale > 30 or (due_in is not None and due_in <= 14):
        return "AMBER"
    return "GREEN"


def format_epics_for_prompt(epics: list) -> str:
    lines = []
    for e in epics:
        rag = calc_rag(e)
        due_in = ""
        try:
            nxt = datetime.fromisoformat(e["nextMilestone"])
            due_in = f" · due {(nxt.replace(tzinfo=timezone.utc) - TODAY).days}d"
        except Exception:
            pass
        open_dec = sum(1 for d in e["decisions"] if d["status"] == "open")
        blocked  = sum(1 for d in e["decisions"] if d["status"] == "blocked")
        lines.append(
            f"- {e['id']} | {e['name']} | {rag} | {e['progress']}% | "
            f"{e['milestoneLabel']}{due_in} | {open_dec} open decisions"
            + (f" | ⚠ {blocked} BLOCKED" if blocked else "")
        )
    return "\n".join(lines)


def format_decisions_for_prompt(epics: list) -> str:
    rows = []
    for e in epics:
        for d in e["decisions"]:
            if d["status"] in ("open", "blocked"):
                rows.append(f"  [{e['id']}] {d['text']} — {d['date']} ({d['owner']}){' *** BLOCKED ***' if d['status'] == 'blocked' else ''}")
    return "\n".join(rows) or "None"


# ── Prompts ───────────────────────────────────────────────────────────────────

def build_digest_prompt(epics: list) -> str:
    rag_counts = {r: sum(1 for e in epics if calc_rag(e) == r) for r in ("RED","AMBER","GREEN")}
    return f"""You are Paul McWilliam, Programme Manager at The Very Group, Data Demand Programme, Financial Services tribe. Write the Monday morning weekly status digest for {TODAY_UK}.

OVERALL: RED={rag_counts['RED']} AMBER={rag_counts['AMBER']} GREEN={rag_counts['GREEN']}

EPIC STATUS:
{format_epics_for_prompt(epics)}

OPEN DECISIONS / BLOCKERS:
{format_decisions_for_prompt(epics)}

Write a concise weekly status email (not a report — conversational but professional) using this structure:

Subject: [Programme Update] Data Demand FS — w/c {TODAY_UK}

HEADLINE: One sentence overall status.

THIS WEEK:
• One bullet per epic (what moved, what's stuck)

DECISIONS NEEDED:
• Numbered list: what, from whom, by when

BLOCKERS:
• Any hard blockers and who owns resolution

THIS WEEK's FOCUS:
• Top 3 priorities

Use UK English. Be specific. Under 250 words. Sign off as Paul McWilliam, PM — Data Demand FS."""


def build_steerco_prompt(epics: list) -> str:
    rag_counts = {r: sum(1 for e in epics if calc_rag(e) == r) for r in ("RED","AMBER","GREEN")}
    return f"""You are Paul McWilliam, Programme Manager at The Very Group, preparing a formal SteerCo status report for the Data Demand Programme, Financial Services tribe. Date: {TODAY_UK}.

OVERALL RAG: RED={rag_counts['RED']} | AMBER={rag_counts['AMBER']} | GREEN={rag_counts['GREEN']}

EPIC STATUS:
{format_epics_for_prompt(epics)}

OPEN DECISIONS / BLOCKERS:
{format_decisions_for_prompt(epics)}

Write a formal SteerCo report with these exact sections. Use UK English. Be specific and escalation-ready. Approx 400 words.

# Programme Status Report — Data Demand FS
**Date:** {TODAY_UK} | **Period:** Week ending {TODAY_UK} | **Prepared by:** Paul McWilliam, PM

## Executive Summary
(2-3 sentences: overall health, key message, any escalation needed)

## Epic Status Overview
| Epic | Name | RAG | Progress | Next Milestone |
|------|------|-----|----------|----------------|
(one row per epic)

## Decisions Required from SteerCo
(Numbered — decision needed, owner, deadline, impact of delay)

## Blockers and Risks
(Hard blockers + emerging risks with owners)

## Achievements This Period
(What has progressed or completed)

## Next Period Priorities
(Top 3 items for next week)

## Decision Log (Open Items)
(All open decisions: decision | owner | due date)"""


# ── Claude call ───────────────────────────────────────────────────────────────

def call_claude(prompt: str, max_tokens: int = 2000) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    if not all([EMAIL_TO, SMTP_SERVER, SMTP_USER, SMTP_PASS]):
        print("  Email skipped — SMTP credentials not configured (set EMAIL_TO, SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASS secrets).")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        print(f"  Email sent to {EMAIL_TO}")
        return True
    except Exception as exc:
        print(f"  Email failed: {exc}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if not ANTHROPIC_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        return 1

    print(f"Mode: {REPORT_MODE} | Date: {TODAY_UK} | Dry run: {DRY_RUN}")

    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    epics = parse_epics(html)
    if not epics:
        print("ERROR: No epics found in index.html")
        return 1
    print(f"Found {len(epics)} epics: {[e['id'] for e in epics]}")

    # Build prompt and call Claude
    if REPORT_MODE == "steerco":
        prompt   = build_steerco_prompt(epics)
        subject  = f"SteerCo Report — Data Demand FS — {TODAY_UK}"
        filename = f"steerco-{TODAY_STR}.md"
    else:
        prompt   = build_digest_prompt(epics)
        subject  = f"[Programme Update] Data Demand FS — w/c {TODAY_UK}"
        filename = f"digest-{TODAY_STR}.md"

    print("Calling Claude API…")
    report_text = call_claude(prompt)

    # Save report file
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = f"{REPORTS_DIR}/{filename}"

    if not DRY_RUN:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"<!-- Generated {TODAY.isoformat()} | {REPORT_MODE} -->\n\n")
            f.write(report_text)
        print(f"Saved: {report_path}")

        # Attempt email
        send_email(subject, report_text)
    else:
        print("[DRY RUN] Report text:")
        print(report_text[:500] + "…")

    # GitHub Actions step summary
    rag_summary = " | ".join(f"{calc_rag(e)}: {e['id']}" for e in epics)
    summary = f"""**Mode:** {REPORT_MODE}  
**Date:** {TODAY_UK}  
**Epics:** {rag_summary}  
**Report saved:** [{filename}](https://mcfearless75.github.io/fs-portfolio-dashboard/{report_path})  
**Email:** {'sent' if EMAIL_TO and not DRY_RUN else 'not configured / dry run'}
"""
    os.makedirs(".github", exist_ok=True)
    with open(RESULT_PATH, "w") as f:
        f.write(summary)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
