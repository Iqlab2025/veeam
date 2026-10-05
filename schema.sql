-- Vendor-neutral schema: veeam / commvault / cohesity collectors all write these same tables.
-- Normalized values every collector must map to:
--   workload : VM | FILE | DB | OTHER   (VM = machine-level image, incl. physical agent backups)
--   kind     : BACKUP | RESTORE
--   status   : SUCCESS | WARNING | FAILED | RUNNING
--   times    : UTC text 'YYYY-MM-DD HH:MM:SS'
--   storage  : always GB. License unit is vendor-specific (instances / sockets / TB), so it is stored per row.

CREATE TABLE IF NOT EXISTS runs (            -- one row per job session (backup or restore)
  vendor      TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  job_id      TEXT,
  name        TEXT,
  kind        TEXT NOT NULL CHECK (kind IN ('BACKUP','RESTORE')),
  workload    TEXT NOT NULL CHECK (workload IN ('VM','FILE','DB','OTHER')),
  status      TEXT NOT NULL CHECK (status IN ('SUCCESS','WARNING','FAILED','RUNNING')),
  start_time  TEXT NOT NULL,
  end_time    TEXT,
  message     TEXT,
  vendor_type TEXT,                          -- raw vendor session/job type, for drill-down
  PRIMARY KEY (vendor, run_id)
);
CREATE INDEX IF NOT EXISTS runs_start ON runs (start_time);

CREATE TABLE IF NOT EXISTS capacity (        -- point-in-time snapshot per poll => trends
  vendor       TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('STORAGE','LICENSE')),
  name         TEXT NOT NULL,                -- repository name / license type
  total        REAL,
  used         REAL,
  unit         TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  PRIMARY KEY (vendor, kind, name, collected_at)
);

CREATE TABLE IF NOT EXISTS protected_objects (  -- latest state per VM / share / DB => RPO
  vendor          TEXT NOT NULL,
  object_id       TEXT NOT NULL,
  name            TEXT,
  workload        TEXT NOT NULL CHECK (workload IN ('VM','FILE','DB','OTHER')),
  last_backup     TEXT,
  last_run_failed INTEGER,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (vendor, object_id)
);

-- Dashboard views (one per requirement). Add a WHERE on start_time in the dashboard for a time window.
CREATE VIEW IF NOT EXISTS v_backup_summary AS
SELECT vendor, count(*) AS total, sum(status = 'SUCCESS') AS successful,
       sum(status = 'WARNING') AS warning, sum(status = 'FAILED') AS failed
FROM runs WHERE kind = 'BACKUP' GROUP BY vendor;

CREATE VIEW IF NOT EXISTS v_failures AS
SELECT vendor, workload, count(*) AS failed
FROM runs WHERE kind = 'BACKUP' AND status = 'FAILED' GROUP BY vendor, workload;

CREATE VIEW IF NOT EXISTS v_job_trend AS
SELECT date(start_time) AS day, vendor, status, count(*) AS runs
FROM runs WHERE kind = 'BACKUP' GROUP BY day, vendor, status;

CREATE VIEW IF NOT EXISTS v_capacity_trend AS     -- storage + license trends
SELECT collected_at, vendor, kind, unit, sum(total) AS total, sum(used) AS used
FROM capacity GROUP BY collected_at, vendor, kind, unit;

CREATE VIEW IF NOT EXISTS v_rpo AS                -- achieved RPO = hours since last good restore point
SELECT vendor, name, workload, last_backup, last_run_failed,
       round((julianday('now') - julianday(last_backup)) * 24, 1) AS rpo_hours
FROM protected_objects;

CREATE VIEW IF NOT EXISTS v_rto AS                -- achieved RTO = measured restore duration
SELECT vendor, workload, count(*) AS restores,
       round(avg(julianday(end_time) - julianday(start_time)) * 1440, 1) AS avg_rto_min,
       round(max(julianday(end_time) - julianday(start_time)) * 1440, 1) AS max_rto_min
FROM runs WHERE kind = 'RESTORE' AND status IN ('SUCCESS','WARNING') GROUP BY vendor, workload;
