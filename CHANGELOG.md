# CX Dashboard — Changelog

---

## 2026-06-03

### IM Metrics tab fixed (data was blank)
- **Root cause**: GitHub Pages on `reaphq/cxdashboard2` was set to legacy mode (deploy from branch), so it served the committed `docs/index.html` which always has `IM_DATA=null`. The GitHub Actions workflow was correctly building and injecting live Google Sheets data, but Pages was ignoring the workflow artifact entirely.
- **Fix**: Switched Pages source from `legacy` → `workflow` (GitHub Actions) via API, then triggered a fresh workflow dispatch. IM Metrics now shows live card program pipeline data (122 programs).

---

## 2026-06-02

### Migrated pipeline from jakegumabon/cxdashboard → reaphq/cxdashboard2
- Created new repo `reaphq/cxdashboard2` with full history pushed from local
- Updated Google Apps Script `GITHUB_REPO` script property to `reaphq/cxdashboard2`
- Updated `origin` remote to point to `reaphq/cxdashboard2`
- Added `GOOGLE_SHEETS_KEY` secret to new repo for IM Metrics Google Sheets integration
- Enabled GitHub Pages with GitHub Actions source, confirmed live at `https://solid-adventure-l4vr1k6.pages.github.io/`

### LA Carlos missing from Capacity tab
- **Root cause**: `capNameAliases` mapped `'LA Carlos'` → `'LA C'` but actual ticket assignee name is just `'LA'`
- **Fix**: Changed alias to `'LA Carlos': 'LA'` in `template/dashboard.html`

### KPI drilldown team filter buttons not working
- **Root cause**: Drilldown function was named `setTeamFilter(team, btn)` which conflicted with an existing `setTeamFilter(group)` function used by the Capacity tab (line ~2615 in template)
- **Fix**: Renamed drilldown function to `setDrilldownTeam(team, btn)` and updated all button `onclick` attributes in the team filter row

### Added team filter (All / CS / TS / IM / CG) to KPI drilldown panel
- New filter row appears inside the drilldown panel when any KPI card is clicked
- Filter resets to "All" on open/close
- `getTeamFilteredTickets()` applies `ASSIGNEE_GROUP` lookup before passing tickets to chart renderers
- Works across all KPIs including SRR channel view and escalation breakdown charts

### IM Metrics tab added
- New tab in the dashboard pulling live card program pipeline data from Google Sheets
- `fetch_im_data()` in `csv_to_dashboard.py` fetches from sheet `1Lek6f3gJLTifx691eQ-xr_8FUNTuRWy49VERNHbbM2Y`, GID `981869513`
- Placeholder `/*{{IM_DATA}}*/null` in template is replaced at build time by the pipeline
- Displays: activation rate, handoff count, avg handling time, stage donut chart, monthly bar chart, full program table with search + stage filter
- Shows "No data — Google Sheets connection not configured" gracefully when `GOOGLE_SHEETS_KEY` secret is absent (local builds)

### Outputs produced
- `unresolved_tickets.xlsx` — 302 unresolved (non-Solved/Closed) tickets, full period, with summary by assignee
- `unassigned_tickets_updated.xlsx` — 68 unassigned open tickets, full period, with summary by status/month/product/category
- Weekly insights report — April vs May comparison (correct monthly cutover)

---

## 2026-05-XX (session prior to 2026-06-02)

### Weekly insights skill wired up
- Extraction script at `/sessions/.../weekly-insights/scripts/extract_metrics.py` reads injected JS constants directly from `docs/index.html`
- Outputs structured JSON for "The Numbers" and "Where to Focus" sections
- Report format: Slack-friendly plain text, no markdown tables

### Resolution time field clarification
- `res` = total resolution time, `rw` = customer wait, `aw` = agent wait
- Used throughout drilldown charts and category analysis

### Escalation definition locked
- Escalation = `within_cx === 'No'` (ticket left CX team)
- March escalation used on-hold proxy; April+ uses Escalation Type field — not directly comparable MoM

### Google Apps Script automation
- `Code.gs` checks Gmail every 30 minutes for Zendesk Explore emails matching subject "For Claude"
- Extracts zip attachment, commits to `data/latest.zip` via GitHub Contents API
- Labels processed emails to avoid reprocessing
- `GITHUB_TOKEN` and `GITHUB_REPO` stored in Script Properties (not hardcoded)
