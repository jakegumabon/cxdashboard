#!/usr/bin/env python3
"""
Pipeline: Zendesk Explore zip → extract CSVs → RAW_TICKETS + OPS JSON → inject into HTML template → final dashboard.

Usage:
  python csv_to_dashboard.py <zip_file> [--template template/dashboard.html] [--output docs/index.html]

Or with pre-extracted CSVs:
  python csv_to_dashboard.py --csv-dir <dir_with_csvs> [--template template/dashboard.html] [--output docs/index.html]
"""

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────

# Patterns to match CSV filenames inside the zip (case-insensitive partial match)
SR_PATTERN      = "support_requests"
TR_PATTERN      = "technical_request"   # replaces product_issue from Apr 1 2026
PI_PATTERN      = "product_issue"       # kept for backward-compat with old zips
OPS_NEW_PATTERN = "new_tickets"
OPS_REO_PATTERN = "reopened_tickets"

# Fallback exact filenames (for manually placed CSVs)
SR_CSV  = "BI Customer Contacts - Support Requests plus Question  Guidance.csv"
TR_CSV  = "BI Customer Contacts - Technical Request.csv"
PI_CSV  = "BI Customer Contacts - Product Issue.csv"       # legacy fallback
OPS_NEW = "BI Operational Volume - New tickets.csv"
OPS_REO = "BI Operational Volume - Reopened tickets.csv"

# Cutover date: on/after this date SR uses Escalation Type field for escalation;
# before this date SR uses on-hold proxy (preserves March baseline for trend)
SR_ESC_CUTOVER = "2026-04-01"

# Sub-columns in the SR/QG CSV whose non-empty value gives Cat Lv 2
SR_SUB_COLUMNS = [
    "Account Access & Restrictions",
    "Account Management",
    "Cards & Transactions",
    "Disputes, Fraud & Refunds",
    "Payments & Transfers",
    "Product Usage & Guidance",
    "Settlement, Repayment & Fees",
]

# Sub-columns in the TR CSV whose non-empty value gives Cat Lv 2 (from Apr 1 2026)
TR_SUB_COLUMNS = [
    "3DS", "API", "ATM", "Cards (CaaS)", "Crypto", "Files", "KYC",
    "Security", "Transactions", "Wallets", "Webhooks",
    "Accounts (Direct)", "Cards (Direct)", "Dashboard (Direct)", "Payments (Direct)",
]

# Technical Category values whose display name differs from the sub-column header
# (columns now match category names directly — no remaps needed)
TR_CAT_TO_SUBCOL = {}

# Sub-columns in the PI CSV whose non-empty value gives Cat Lv 2 (legacy)
PI_SUB_COLUMNS = [
    "Authentication & Security Issues",
    "Card Functionality Issues",
    "Digital Wallets Issues",
    "Transactions Issues",
    "Settlements Issues",
    "UI & UX Issues",
    "Payments Issues",
    "Logistics & Fulfillment Issues",
    "Fraud & Risk Issues",
    "Integration Issues",
    "API Issues",
]

DOW_MAP = {0: "1Mon", 1: "2Tue", 2: "3Wed", 3: "4Thu", 4: "5Fri", 5: "6Sat", 6: "7Sun"}
DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Support Category values that don't match sub-column headers exactly
SR_CAT_TO_SUBCOL = {
    "Statements, Repayments & Fees": "Settlement, Repayment & Fees",
    "Product Usage & Limits": "Product Usage & Guidance",
}

# Normalize cat1 values to canonical names
CAT1_NORMALIZE = {
    "Statements, Repayments & Fees": "Settlement, Repayment & Fees",
    "Product Usage & Guidance": "Product Usage & Limits",
}

# Agent → Team mapping
ASSIGNEE_GROUP = {
    # IM
    "Yen Lin": "IM", "Samson Fok": "IM", "Ethan Liou": "IM",
    "Nicholas Tan": "IM", "Michael Tjendra": "IM",
    # CS
    "Timothy Hernandez": "CS", "Ediline Postrano": "CS", "Ian Soliman": "CS",
    "Joe Flores": "CS", "Lorenz Agustin": "CS", "Mike Cunanan": "CS",
    "LA Carlos": "CS", "Eunice Suñga": "CS", "Patric Pangan": "CS",
    "Herbert Acpal": "CS", "Racky Manlapaz": "CS",
    # TS
    "Chang": "TS", "Jose Shardin": "TS", "Jaiber Kler": "TS",
    "Jan Rivera": "TS", "Bogdan Hristovski": "TS", "Gilbert Arces": "TS",
    "John Jacinto": "TS", "Raymark Cayanan": "TS", "Hans Wang": "TS",
    "Allen Reyes": "TS", "Adrian Maranan": "TS",
    # CG (not CX — excluded from team comparison)
    "Chloe Wong": "CG", "Angela Shih": "CG", "Catherine Sun": "CG",
    "Joris Thibault-Gabily": "CG", "Lebin Looi": "CG", "Maxwell Li": "CG",
    "Michael Chang": "CG", "Sam Lee": "CG", "Robin Koh": "CG",
    # Admin
    "Clyde": "Admin", "Franco": "Admin", "Rollie Villaflor": "Admin",
    # Card Ops
    "Ming Ming Ooi": "Card Ops", "Weilam Thum": "Card Ops",
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def safe_float(val, default=0.0):
    if val is None:
        return default
    val = str(val).strip()
    if val == "" or val == " ":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def safe_int(val, default=0):
    return int(safe_float(val, default))


def get_dow(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return DOW_MAP[dt.weekday()]


def get_dow_label(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return DOW_LABELS[dt.weekday()]


def find_cat2(row, sub_columns, primary_category="", cat_to_subcol=None):
    lookup = primary_category
    if cat_to_subcol and primary_category in cat_to_subcol:
        lookup = cat_to_subcol[primary_category]
    if lookup:
        for col in sub_columns:
            if col == lookup or col.startswith(lookup):
                val = row.get(col, "").strip()
                if val and val != " ":
                    return val
    for col in sub_columns:
        val = row.get(col, "").strip()
        if val and val != " ":
            return val
    return ""


# ── Extract zip ─────────────────────────────────────────────────────────────

def identify_csv(filenames, pattern, fallback_name):
    """Find the CSV filename matching a pattern inside the zip."""
    pattern_lower = pattern.lower()
    for fn in filenames:
        basename = os.path.basename(fn).lower()
        if pattern_lower in basename and basename.endswith(".csv"):
            return fn
    # Try fallback exact match
    for fn in filenames:
        if os.path.basename(fn) == fallback_name:
            return fn
    return None


def extract_zip(zip_path, extract_dir):
    """Extract zip and return paths to the CSVs.

    Returns a dict with keys: sr, tr (or pi as fallback), ops_new, ops_reo.
    'tr_mode' key is True if a Technical Request file was found, False if
    falling back to legacy Product Issue.
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)
        filenames = zf.namelist()

    sr      = identify_csv(filenames, SR_PATTERN,      SR_CSV)
    tr      = identify_csv(filenames, TR_PATTERN,      TR_CSV)
    pi      = identify_csv(filenames, PI_PATTERN,      PI_CSV)   # legacy fallback
    ops_new = identify_csv(filenames, OPS_NEW_PATTERN, OPS_NEW)
    ops_reo = identify_csv(filenames, OPS_REO_PATTERN, OPS_REO)

    # Keyword fallback scan
    if not sr or not (tr or pi) or not ops_new or not ops_reo:
        for fn in filenames:
            bn = os.path.basename(fn).lower()
            if not bn.endswith(".csv"):
                continue
            if not sr and ("support_request" in bn or "question" in bn or "guidance" in bn):
                sr = fn
            elif not tr and "technical_request" in bn:
                tr = fn
            elif not pi and "product_issue" in bn:
                pi = fn
            elif not ops_new and "new_ticket" in bn:
                ops_new = fn
            elif not ops_reo and ("reopened" in bn or "reopen" in bn):
                ops_reo = fn

    # Last resort: match by Zendesk Explore tab names
    if not sr or not (tr or pi) or not ops_new or not ops_reo:
        csv_files = [fn for fn in filenames if fn.lower().endswith(".csv")]
        print(f"  Available CSVs in zip: {csv_files}", file=sys.stderr)
        for fn in csv_files:
            bn = os.path.basename(fn).lower()
            if not sr and "customer_contacts" in bn and "product" not in bn and "technical" not in bn:
                sr = fn
            elif not tr and "customer_contacts" in bn and "technical" in bn:
                tr = fn
            elif not pi and "customer_contacts" in bn and "product" in bn:
                pi = fn
            elif not ops_new and "operational" in bn and "new" in bn:
                ops_new = fn
            elif not ops_reo and "operational" in bn and "reopen" in bn:
                ops_reo = fn

    # Decide: use TR if found, otherwise fall back to legacy PI
    tr_mode = tr is not None
    second_file = tr if tr_mode else pi
    second_label = "Technical Request" if tr_mode else "Product Issue (legacy)"
    second_key   = "tr"

    paths = {}
    for key, val, label, required in [
        ("sr",        sr,          "Support Requests + Q&G", True),
        (second_key,  second_file, second_label,             True),
        ("ops_new",   ops_new,     "Ops New Tickets",        False),
        ("ops_reo",   ops_reo,     "Ops Reopened Tickets",   False),
    ]:
        if val is None:
            if required:
                print(f"ERROR: Could not find {label} CSV in zip", file=sys.stderr)
                sys.exit(1)
            else:
                print(f"  ⚠ {label}: not found (ops data will be skipped)")
                paths[key] = None
        else:
            paths[key] = os.path.join(extract_dir, val)
            print(f"  {label}: {val}")

    paths["tr_mode"] = tr_mode
    return paths


# ── Parse Support Requests + Question/Guidance ──────────────────────────────

def parse_sr(csv_path):
    tickets = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticket_id = row.get("Ticket ID", "").strip()
            if not ticket_id:
                continue
            cat1 = row.get("Support Category", "").strip()
            cat2 = find_cat2(row, SR_SUB_COLUMNS, primary_category=cat1, cat_to_subcol=SR_CAT_TO_SUBCOL)
            cat1 = CAT1_NORMALIZE.get(cat1, cat1)
            date_str = row.get("Ticket created - Date", "").strip()
            esc_type = row.get("Escalation Type", "").strip()
            if date_str >= SR_ESC_CUTOVER and esc_type:
                # Apr 1 2026+ : use explicit Escalation Type field
                within_cx = "Yes" if esc_type == "No Escalation" else "No"
            else:
                # Pre-Apr 1 (or missing field): use on-hold proxy to preserve March baseline
                on_hold = safe_float(row.get("On-hold time (hrs)"))
                within_cx = "Yes" if on_hold == 0.0 else "No"
            oc = row.get("Other Category", "").strip()
            esc_int = row.get("Internal Escalation", "").strip()
            esc_ext = row.get("External Escalation", "").strip()
            reopens = int(float(row.get("Reopens", "0") or 0))
            product = row.get("Product", "").strip()
            direct_product = row.get("Direct Product", "").strip()
            caas_product = row.get("CaaS Product", "").strip()
            ticket = {
                "id": safe_int(ticket_id),
                "date": row.get("Ticket created - Date", "").strip(),
                "hour": safe_int(row.get("Ticket created - Hour")),
                "status": row.get("Ticket status", "").strip(),
                "channel": row.get("Ticket Channel v2", "").strip(),
                "assignee": row.get("Assignee name", "").strip(),
                "cat1": cat1,
                "cat2": cat2,
                "req_type": row.get("Request Type", "").strip(),
                "within_cx": within_cx,
                "esc_int": esc_int,
                "esc_ext": esc_ext,
                "reopens": reopens,
                "product": product,
                "direct_product": direct_product,
                "caas_product": caas_product,
                "clean": safe_int(row.get("Tickets not merged and not dispute", 1)),
                "res": round(safe_float(row.get("Full resolution time (hrs)")), 2),
                "rw": round(safe_float(row.get("Requester wait time (hrs)")), 2),
                "aw": round(safe_float(row.get("Agent wait time (hrs)")), 2),
                "surveyed": safe_int(row.get("Surveyed satisfaction tickets")),
                "good": safe_int(row.get("Good satisfaction tickets")),
                "bad": safe_int(row.get("Bad satisfaction tickets")),
                "dow": get_dow(row.get("Ticket created - Date", "").strip()),
            }
            if oc and oc != " ":
                ticket["oc"] = oc
            tickets.append(ticket)
    return tickets


# ── Parse Product Issue ─────────────────────────────────────────────────────

def parse_pi(csv_path):
    tickets = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticket_id = row.get("Ticket ID", "").strip()
            if not ticket_id:
                continue
            cat1 = row.get("Issue Category", "").strip()
            cat2 = find_cat2(row, PI_SUB_COLUMNS, primary_category=cat1)
            cat1 = CAT1_NORMALIZE.get(cat1, cat1)
            esc_type = row.get("Escalation Type", "").strip()
            within_cx = "Yes" if esc_type == "No Escalation" else "No"
            oc = row.get("Other Category", "").strip()
            ticket = {
                "id": safe_int(ticket_id),
                "date": row.get("Ticket created - Date", "").strip(),
                "hour": safe_int(row.get("Ticket created - Hour")),
                "status": row.get("Ticket status", "").strip(),
                "channel": row.get("Ticket Channel v2", "").strip(),
                "assignee": row.get("Assignee name", "").strip(),
                "cat1": cat1,
                "cat2": cat2,
                "req_type": "Product Issue",
                "within_cx": within_cx,
                "clean": safe_int(row.get("Tickets not merged and not dispute", 1)),
                "res": round(safe_float(row.get("Full resolution time (hrs)")), 2),
                "rw": round(safe_float(row.get("Requester wait time (hrs)")), 2),
                "aw": round(safe_float(row.get("Agent wait time (hrs)")), 2),
                "surveyed": safe_int(row.get("Surveyed satisfaction tickets")),
                "good": safe_int(row.get("Good satisfaction tickets")),
                "bad": safe_int(row.get("Bad satisfaction tickets")),
                "dow": get_dow(row.get("Ticket created - Date", "").strip()),
            }
            if oc and oc != " ":
                ticket["oc"] = oc
            tickets.append(ticket)
    return tickets


# ── Parse Technical Request (replaces Product Issue from Apr 1 2026) ────────

def parse_tr(csv_path):
    tickets = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticket_id = row.get("Ticket ID", "").strip()
            if not ticket_id:
                continue
            cat1_raw = row.get("Technical Category", "").strip()
            cat2 = find_cat2(row, TR_SUB_COLUMNS, primary_category=cat1_raw, cat_to_subcol=TR_CAT_TO_SUBCOL)
            # Normalize display name (strip "(Direct)" suffix variants)
            cat1 = TR_CAT_TO_SUBCOL.get(cat1_raw, cat1_raw)
            esc_type = row.get("Escalation Type", "").strip()
            within_cx = "Yes" if esc_type == "No Escalation" else "No"
            oc = row.get("Other Category", "").strip()
            tr_reopens = int(float(row.get("Reopens", "0") or 0))
            ticket = {
                "id": safe_int(ticket_id),
                "date": row.get("Ticket created - Date", "").strip(),
                "hour": safe_int(row.get("Ticket created - Hour")),
                "status": row.get("Ticket status", "").strip(),
                "channel": row.get("Ticket Channel v2", "").strip(),
                "assignee": row.get("Assignee name", "").strip(),
                "cat1": cat1,
                "cat2": cat2,
                "req_type": "Technical Request",
                "within_cx": within_cx,
                "reopens": tr_reopens,
                "clean": safe_int(row.get("Tickets not merged and not dispute", 1)),
                "res": round(safe_float(row.get("Full resolution time (hrs)")), 2),
                "rw": round(safe_float(row.get("Requester wait time (hrs)")), 2),
                "aw": round(safe_float(row.get("Agent wait time (hrs)")), 2),
                "surveyed": safe_int(row.get("Surveyed satisfaction tickets")),
                "good": safe_int(row.get("Good satisfaction tickets")),
                "bad": safe_int(row.get("Bad satisfaction tickets")),
                "dow": get_dow(row.get("Ticket created - Date", "").strip()),
            }
            if oc and oc != " ":
                ticket["oc"] = oc
            tickets.append(ticket)
    return tickets


# ── Parse Operational Volume ────────────────────────────────────────────────

def parse_ops(new_csv_path, reopen_csv_path):
    new_by_date = defaultdict(int)
    new_by_date_hour = defaultdict(lambda: defaultdict(int))
    with open(new_csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row.get("Ticket created - Date", "").strip()
            hour = safe_int(row.get("Ticket created - Hour"))
            count = safe_int(row.get("Tickets not merged and not dispute"))
            if not date:
                continue
            new_by_date[date] += count
            new_by_date_hour[date][hour] += count

    reo_by_date = defaultdict(int)
    reo_by_date_hour = defaultdict(lambda: defaultdict(int))
    with open(reopen_csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row.get("Update - Date", "").strip()
            hour = safe_int(row.get("Update - Hour"))
            count = safe_int(row.get("Reopen Count by End-user and Admin"))
            if not date:
                continue
            reo_by_date[date] += count
            reo_by_date_hour[date][hour] += count

    all_dates = sorted(set(list(new_by_date.keys()) + list(reo_by_date.keys())))
    n_days = len(all_dates)
    total_new = sum(new_by_date.values())
    total_reopen = sum(reo_by_date.values())
    total = total_new + total_reopen

    daily = {
        "labels": all_dates,
        "new": [new_by_date.get(d, 0) for d in all_dates],
        "reopen": [reo_by_date.get(d, 0) for d in all_dates],
    }

    dow_count = defaultdict(int)
    for d in all_dates:
        dow_count[get_dow_label(d)] += 1

    def build_heatmap(by_date_hour):
        dow_hour_total = defaultdict(lambda: defaultdict(float))
        for d in all_dates:
            dow_label = get_dow_label(d)
            for h in range(24):
                dow_hour_total[dow_label][h] += by_date_hour[d][h]
        heatmap = {}
        for dow in DOW_LABELS:
            heatmap[dow] = {}
            n = dow_count.get(dow, 1)
            for h in range(24):
                heatmap[dow][str(h)] = round(dow_hour_total[dow][h] / n, 2)
        return heatmap

    heatmap_new = build_heatmap(new_by_date_hour)
    heatmap_reopen = build_heatmap(reo_by_date_hour)
    heatmap_all = {}
    for dow in DOW_LABELS:
        heatmap_all[dow] = {}
        for h in range(24):
            heatmap_all[dow][str(h)] = round(
                heatmap_new[dow][str(h)] + heatmap_reopen[dow][str(h)], 2
            )

    return {
        "summary": {
            "total_new": total_new, "total_reopen": total_reopen, "total": total,
            "n_days": n_days,
            "period_from": all_dates[0] if all_dates else "",
            "period_to": all_dates[-1] if all_dates else "",
        },
        "daily": daily,
        "heatmap_new": heatmap_new,
        "heatmap_reopen": heatmap_reopen,
        "heatmap_all": heatmap_all,
    }


# ── Inject into template ───────────────────────────────────────────────────

def inject_into_template(template_path, output_path, raw_tickets, ops, assignee_group):
    """Replace placeholders in template HTML with actual JSON data."""
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Build ASSIGNEE_GROUP JS object string
    ag_lines = []
    team_comments = {"IM": "IM", "CS": "CS", "TS": "TS", "CG": "CG (not CX)", "Admin": "Admin", "Card Ops": "Card Ops"}
    for team in ["IM", "CS", "TS", "CG", "Admin", "Card Ops"]:
        entries = [f"'{name}':'{grp}'" for name, grp in assignee_group.items() if grp == team]
        if entries:
            comment = team_comments.get(team, team)
            ag_lines.append(f"  // {comment}")
            ag_lines.append("  " + ",".join(entries) + ",")
    ag_js = "{\n" + "\n".join(ag_lines) + "\n}"

    # Replace placeholders
    html = re.sub(
        r'const ASSIGNEE_GROUP\s*=\s*/\*\{\{ASSIGNEE_GROUP\}\}\*/\s*\{[^}]*\}\s*;',
        f'const ASSIGNEE_GROUP = {ag_js};',
        html
    )
    html = re.sub(
        r'const RAW_TICKETS\s*=\s*/\*\{\{RAW_TICKETS\}\}\*/\s*\[\s*\]\s*;',
        f'const RAW_TICKETS = {json.dumps(raw_tickets, ensure_ascii=False)};',
        html
    )
    html = re.sub(
        r'const OPS\s*=\s*/\*\{\{OPS\}\}\*/\s*\{\s*\}\s*;',
        f'const OPS = {json.dumps(ops, ensure_ascii=False)};',
        html
    )

    # Fix hardcoded date input values to match actual data range
    if raw_tickets:
        dates = sorted(set(t["date"] for t in raw_tickets if t.get("date")))
        min_date = dates[0]
        max_date = dates[-1]
        # Replace date-from value and min
        # NOTE: Use \g<N> notation — never bare \N followed by digits (e.g. \2 + "2026" = \22026 = group 22, empty)
        html = re.sub(
            r'(<input[^>]*id="date-from"[^>]*value=")[^"]*("[^>]*min=")[^"]*(")',
            rf'\g<1>{min_date}\g<2>{min_date}\g<3>',
            html
        )
        # Replace date-to value and min
        html = re.sub(
            r'(<input[^>]*id="date-to"[^>]*value=")[^"]*("[^>]*min=")[^"]*(")',
            rf'\g<1>{max_date}\g<2>{min_date}\g<3>',
            html
        )
        print(f"  Date inputs set to: {min_date} → {max_date}")
        # Update static filter-status text (also updated dynamically by IIFE on page load)
        html = re.sub(
            r'id="filter-status">[^<]*<',
            f'id="filter-status">All data \u00b7 {len(raw_tickets)} tickets<',
            html
        )

    # Detect agents in ticket data not mapped to any team — inject a notice
    all_assignees = set(t["assignee"] for t in raw_tickets if t.get("assignee"))
    mapped_names  = set(assignee_group.keys())
    unmapped = sorted(all_assignees - mapped_names)

    if unmapped:
        names_str = ", ".join(unmapped)
        unmapped_html = (
            f'<div style="margin:8px 0;padding:10px 14px;background:#2a1f0a;border:1px solid #f5a623;'
            f'border-radius:6px;color:#f5a623;font-size:12px;">'
            f'&#9888;&nbsp; <strong>{len(unmapped)} agent{"s" if len(unmapped)>1 else ""} with no team assigned:</strong>'
            f'&nbsp;{names_str}&nbsp;—&nbsp;their tickets are excluded from team comparisons.'
            f'</div>'
        )
        print(f"  ⚠ {len(unmapped)} unmapped agent(s): {names_str}")
    else:
        unmapped_html = ""
        print("  ✓ All agents mapped to a team")
    html = html.replace("/*{{UNMAPPED_NOTICE}}*/", unmapped_html)

    # Build non-CX tooltip (⚠ icon next to ticket count in filter bar)
    NON_CX_GROUPS = ["CG", "Admin", "Card Ops"]
    tooltip_rows = []
    total_non_cx = 0
    for group in NON_CX_GROUPS:
        members = sorted(name for name, grp in assignee_group.items() if grp == group)
        in_data  = sorted(name for name in members if any(t.get("assignee") == name for t in raw_tickets))
        count    = sum(1 for t in raw_tickets if t.get("assignee") in members)
        if in_data and count > 0:
            total_non_cx += count
            tooltip_rows.append(
                f'<div class="non-cx-tip-row">'
                f'<div class="non-cx-tip-group">{group} &mdash; {count} ticket{"s" if count>1 else ""}</div>'
                f'<div class="non-cx-tip-members">{", ".join(in_data)}</div>'
                f'</div>'
            )

    if tooltip_rows:
        rows_html = "\n".join(tooltip_rows)
        tooltip_html = (
            f'<span class="non-cx-tip">'
            f'<span class="non-cx-tip-icon">&#9888;</span>'
            f'<div class="non-cx-tip-box">'
            f'<div style="font-weight:700;color:#f5a623;margin-bottom:10px;font-size:11.5px;">'
            f'{total_non_cx} tickets from non-CX teams</div>'
            f'<div style="color:var(--text3);font-size:10.5px;margin-bottom:10px;">'
            f'Included in overall totals but excluded from CX team comparisons.</div>'
            f'{rows_html}'
            f'</div>'
            f'</span>'
        )
    else:
        tooltip_html = ""
    html = html.replace("/*{{NON_CX_TOOLTIP}}*/", tooltip_html)

    # Inject pipeline run timestamp (HKT = UTC+8)
    from datetime import timezone, timedelta
    hkt = timezone(timedelta(hours=8))
    generated_at = datetime.now(hkt).strftime("%b %d, %Y · %H:%M HKT")
    html = html.replace("{{GENERATED_AT}}", generated_at)
    print(f"  Generated at: {generated_at}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ Dashboard written to {output_path} ({len(html) // 1024} KB)")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build CX Dashboard from Zendesk data")
    parser.add_argument("zip_file", nargs="?", help="Path to Zendesk Explore zip file")
    parser.add_argument("--csv-dir", help="Directory with pre-extracted CSVs (alternative to zip)")
    parser.add_argument("--template", default="template/dashboard.html", help="Path to HTML template")
    parser.add_argument("--output", default="docs/index.html", help="Output HTML path")
    args = parser.parse_args()

    if not args.zip_file and not args.csv_dir:
        print("ERROR: Provide either a zip file or --csv-dir", file=sys.stderr)
        sys.exit(1)

    # Determine CSV paths
    if args.zip_file:
        import tempfile
        extract_dir = tempfile.mkdtemp(prefix="cx_csv_")
        print(f"Extracting {args.zip_file}...")
        paths = extract_zip(args.zip_file, extract_dir)
    else:
        csv_dir = args.csv_dir
        # For --csv-dir: prefer TR file if present, fall back to PI
        tr_path = os.path.join(csv_dir, TR_CSV)
        pi_path = os.path.join(csv_dir, PI_CSV)
        tr_mode = os.path.exists(tr_path)
        paths = {"tr_mode": tr_mode}
        for key, fname, required in [
            ("sr",  SR_CSV,               True),
            ("tr",  TR_CSV if tr_mode else PI_CSV, True),
            ("ops_new", OPS_NEW,          False),
            ("ops_reo", OPS_REO,          False),
        ]:
            p = os.path.join(csv_dir, fname)
            if os.path.exists(p):
                paths[key] = p
            elif required:
                print(f"ERROR: {key} file not found: {p}", file=sys.stderr)
                sys.exit(1)
            else:
                paths[key] = None

    # Parse
    print("Parsing Support Requests + Question/Guidance...")
    sr_tickets = parse_sr(paths["sr"])
    print(f"  → {len(sr_tickets)} tickets")

    if paths.get("tr_mode"):
        print("Parsing Technical Request...")
        second_tickets = parse_tr(paths["tr"])
        second_label = "Technical Request"
    else:
        print("Parsing Product Issue (legacy)...")
        second_tickets = parse_pi(paths["tr"])
        second_label = "Product Issue"
    print(f"  → {len(second_tickets)} tickets")

    all_tickets = sr_tickets + second_tickets
    all_tickets.sort(key=lambda t: (t["date"], t["hour"], t["id"]))
    print(f"  → {len(all_tickets)} total tickets")

    if paths.get("ops_new") and paths.get("ops_reo"):
        print("Parsing Operational Volume...")
        ops = parse_ops(paths["ops_new"], paths["ops_reo"])
        print(f"  → {ops['summary']['total_new']} new, {ops['summary']['total_reopen']} reopen")
    else:
        print("⚠ Operational Volume CSVs missing — OPS tab will show no data")
        print("  → Add 'BI Operational Volume - New tickets' and 'BI Operational Volume - Reopened tickets'")
        print("     to your Zendesk Explore 'For Claude' scheduled delivery to enable this tab.")
        ops = {
            "summary": {"total_new": 0, "total_reopen": 0, "total": 0, "n_days": 0,
                        "period_from": "", "period_to": ""},
            "daily": {"labels": [], "new": [], "reopen": []},
            "heatmap_new": {}, "heatmap_reopen": {}, "heatmap_all": {},
        }

    # Inject into template
    print(f"\nInjecting into template: {args.template}")
    inject_into_template(args.template, args.output, all_tickets, ops, ASSIGNEE_GROUP)

    # Summary
    mode_str = "TR mode" if paths.get("tr_mode") else "PI mode (legacy)"
    print(f"\n── Summary ({mode_str}) ──")
    print(f"Tickets: {len(all_tickets)}")
    print(f"Date range: {all_tickets[0]['date']} to {all_tickets[-1]['date']}")
    print(f"Ops: {ops['summary']['total_new']} new + {ops['summary']['total_reopen']} reopen")
    print(f"Dashboard: {args.output}")


if __name__ == "__main__":
    main()
