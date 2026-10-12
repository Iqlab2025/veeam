# Backup Dashboard POC — Project Notes

Living handover doc. Read this first in a new session; update it whenever something changes.
**No passwords in this file** — credentials live only in `.env` (gitignore it, `chmod 600` on Linux).

_Last updated: 2026-10-08_

## Goal
Grafana dashboard across Veeam, Commvault and Cohesity. **POC = Veeam only.** Requirements (initial phase):
1. Total / successful / failed backups
2. Failed backups split by vendor → VM / FILE / DB level
3. Storage utilization trend per backup tool
4. License utilization trend per backup tool
5. Backup job trend graph
6. RPO and RTO details

Department-wise items were dropped from the POC list by the user.

## Environment
| What | Where |
|---|---|
| Veeam B&R (REST API v1.3-rev2) | `https://192.168.1.34:9419` (self-signed cert), user `administrator` |
| Veeam license | Was Community Edition (API blocked: "License restrictions apply."), **now 30-day EnterprisePlus trial** installed 2026-10-05 |
| Grafana | `http://192.168.1.170:3000`, v13.2.3, plugin `frser-sqlite-datasource` installed |
| Rocky 10 VM | Planned home for collector + DB at `/opt/backup-dash` (cron every 15 min) |
| Dev machine | Windows, project at `D:\vs code\veeam`, Python 3.13 |

## Files
- `schema.sql` — vendor-neutral schema + dashboard views. Tables: `runs` (one row per session, PK vendor+run_id, upsert), `capacity` (STORAGE/LICENSE snapshot per poll → trends), `protected_objects` (latest backup per object → RPO). Views: `v_backup_summary`, `v_failures`, `v_job_trend`, `v_capacity_trend`, `v_rpo`, `v_rto`.
- `veeam_collector.py` — stdlib-only collector. Reads `.env` next to itself. `--selftest` runs offline asserts. Each API section is wrapped in `optional()` so a blocked endpoint is skipped, not fatal.
- `.env` — VEEAM_URL, VEEAM_USER, VEEAM_PASS, VEEAM_INSECURE=1, VEEAM_CA_FILE, DB_PATH, LOOKBACK_DAYS.
- `grafana_dashboard.json` — "Backup Overview" dashboard, 7 panels, asks for the SQLite datasource on import.
- `backup_dash.db` — SQLite data (created by the collector).

## Key decisions
- Commvault/Cohesity get their own collector script later but write the **same tables** with `vendor='commvault'/'cohesity'`; normalized values: workload VM|FILE|DB|OTHER, kind BACKUP|RESTORE, status SUCCESS|WARNING|FAILED|RUNNING, UTC text times, storage in GB.
- Dedup: session ID is the key; re-poll starts at the oldest RUNNING session so it gets its final status.
- History: first run back-fills `LOOKBACK_DAYS` of sessions; storage/license/RPO history only exists from the first poll onward (Veeam API gives current values only).
- RPO = target (dashboard variable) vs actual data-loss window (time since last restore point) → MET/BREACHED. RTO = measured restore session duration, no target yet.
- Agent (physical) machine backups count as VM-level. Use a **file-share (NAS) job** for FILE-level; an agent file-level job is indistinguishable from a machine job via the API.
- Claude does not log in to Grafana/Veeam with the user's passwords (safety rule) — the user runs authenticated steps.

## Status / log
- 2026-10-05: Schema + collector written, selftest passing. Community Edition blocked the API → trial license installed → all endpoints OK.
- 2026-10-05: DB has only storage (Default Backup Repository, 150 GB, 0 used) and license (EnterprisePlus 1000 instances, 0 used) rows. **No backup jobs exist in Veeam yet**, so runs / RPO / RTO are empty.
- 2026-10-05: Dashboard JSON made. User reports **"No data" in Grafana** at 192.168.1.170 — diagnosing (likely DB file not on the Grafana host, or file permission). Expected: only Storage + License panels have data until jobs run.

- 2026-10-05: Grafana runs in **Docker** on the Rocky VM (`railtel`); container re-created with `-v /home/rocky/veeam:/data:ro`, so datasource path = `/data/backup_dash.db`. Collector folder on VM is `/home/rocky/veeam` (not /opt/backup-dash).
- 2026-10-05: Panel error `missing named argument "__unixEpochFrom()"` — this frser plugin version doesn't expand `$__unixEpochFrom()`. Dashboard queries switched to Grafana globals `${__from:date:seconds}` / `${__to:date:seconds}`. Awaiting re-import result. No Grafana connector/tool is available to Claude in this session.

- 2026-10-05: Fixed dashboard imported and working; user's first job (agent backup, failed) shows in Grafana.
- 2026-10-06: Drill-down added: dashboard variable `status` (total|success|warning|failed). Clicking a Stat tile (data link uses `${__field.name}`, so stat column names must stay total/success/warning/failed) or a failure bar reloads the dashboard with that status; new panel "Backup runs: $status" (id 8, right under the stats) lists started/ended/duration/job/vendor/workload/status/message.

- 2026-10-06: User wanted details on a separate page, not an in-place filter. Table moved to its own dashboard `grafana_runs_dashboard.json` (uid `backup-runs`, variables status / vendor / workload, link back to overview). Overview (uid `backup-overview`) has no variables now; Stat tile and failure-bar data links open `/d/backup-runs/...` in a new tab with `${__url_time_range}`. The bar query also returns vendor + workload columns for the link.
- Note: "No rows" for warning was correct (0 warning runs), not a data bug.

- 2026-10-06: RPO/RTO empty explained: no successful backup yet (no restore points) and no restores. Offered job-level RPO from `runs` (shows never-succeeded jobs in red) — not built yet, awaiting user.
- 2026-10-08: **Veeam IP changed to 192.168.1.34** (.env, script default, notes updated; API answers 401 = reachable). VM copy of `.env` must be updated by the user.
- 2026-10-08: Overview storage/license trend panels replaced by Stat counters (latest snapshot per vendor: used, total, used_pct with 75/90% thresholds). Clicking opens new dashboard `grafana_capacity_dashboard.json` (uid `backup-capacity`) holding both trend graphs, new tab, keeps time range.

- 2026-10-08: Explained to user: Veeam API has **no RPO/RTO fields**; both are computed (RPO from newest restore point per object, RTO from restore session durations). Caveat: file-level restore sessions stay open while the restore browser is open, so they overstate RTO.
- 2026-10-08: First successful backup → RPO shows (user saw 0.8 h = 48 min). RPO unit `h` did NOT scale down (showed "0.800 hour"), so the RPO panel now selects `rpo` in **seconds** with unit `s` (renders 48 min / 5.2 hour / 1.3 day); red threshold 86400 s = 24 h. `v_rpo` view still returns hours.
- 2026-10-08: User pointed out RPO = *target* (max tolerable data loss), not just backup age. RPO panel is now "RPO compliance": dashboard variable `rpo_target_hours` (24 default; 1/4/12/48) + columns rpo_target, data_loss_window (age of last good restore point, seconds, unit `s`), rpo_status MET/BREACHED (never-backed-up = BREACHED). One global target for now; per-workload/per-job targets would need a targets table.
- 2026-10-12: Added overview panel id 9 "Failed backups by level": stat tiles VM / FILE / DB / OTHER (failed BACKUP runs in range, zeros shown, red when ≥1). Field names = runs-dashboard `workload` values; click opens backup-runs with status=failed&workload=<tile>.
- 2026-10-12: A **Grafana MCP connector** appeared in the session but returns `401 Invalid API key` — user must fix its service-account token before Claude can read/update dashboards directly. Until then: edit JSON files, user imports with Overwrite.
- 2026-10-12: RPO looked 6 h stale right after a job. Root cause: **Veeam server (192.168.1.34) clock ~6.1 h behind real UTC** (HTTP Date header 23:56 GMT vs real 06:03 UTC) — likely wrong time zone + drift. Not a code bug. Collector now prints `WARNING: Veeam server clock is ±X h off` at login when skew > 5 min. User to fix the Windows time zone/NTP on the VBR server; history recorded before the fix keeps the wrong times.
- Note: in Grafana the runs table converts times to the browser's zone (IST), while RPO `last_backup` shows raw UTC text — looks like a 5:30 mismatch but both are stored in UTC.

## Dashboards (3 files, all ask for the SQLite datasource on import)
- `grafana_dashboard.json` — Backup Overview (uid backup-overview)
- `grafana_runs_dashboard.json` — Backup Runs detail table (uid backup-runs)
- `grafana_capacity_dashboard.json` — Capacity Trends (uid backup-capacity)

## Next steps
1. User to import `grafana_capacity_dashboard.json`, re-import overview (overwrite), update VEEAM_URL in the VM's `.env`.
2. In Veeam: add infrastructure, create VM / file-share / (DB) jobs, schedule hourly, force one failure, do restores.
3. Move collector to the Rocky VM + cron.
4. Later: RPO history snapshot, RPO/RTO targets, Commvault + Cohesity collectors.
