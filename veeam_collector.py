"""Veeam B&R REST (v1.3-rev2) -> SQLite collector. Stdlib only, run it from cron:

  */15 * * * * python3 /opt/backup-dash/veeam_collector.py

Settings come from .env next to this file (real environment variables win over it):
     VEEAM_URL (default https://192.168.1.34:9419), VEEAM_USER, VEEAM_PASS, DB_PATH (default backup_dash.db),
     VEEAM_CA_FILE (exported VBR cert) or VEEAM_INSECURE=1 (skip TLS check, POC only), LOOKBACK_DAYS (first run, default 30).
Self-check (no server needed): python3 veeam_collector.py --selftest
"""
import json, os, sqlite3, ssl, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    # KEY=value lines, '#' comments, optional surrounding quotes; no inline comments so passwords may contain '#'
    if not os.path.exists(path):
        return
    for line in open(path, encoding='utf-8'):
        key, sep, val = line.strip().partition('=')
        if sep and not key.startswith('#'):
            val = val.strip()
            if len(val) > 1 and val[0] == val[-1] and val[0] in '"\'':
                val = val[1:-1]
            os.environ.setdefault(key.strip(), val)


load_env(os.path.join(HERE, '.env'))
VENDOR = 'veeam'
BASE = os.environ.get('VEEAM_URL', 'https://192.168.1.34:9419').rstrip('/')
DB_PATH = os.path.join(HERE, os.environ.get('DB_PATH', 'backup_dash.db'))  # relative paths = next to this file
SCHEMA = os.path.join(HERE, 'schema.sql')

CTX = ssl.create_default_context(cafile=os.environ.get('VEEAM_CA_FILE'))
if os.environ.get('VEEAM_INSECURE') == '1':  # VBR ships a self-signed cert
    CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE

# --- normalization: Veeam enums -> schema.sql values ---------------------------------
DB_SESSIONS = {'SqlLogBackup', 'OracleLogBackup', 'PostgreSqlLogBackup'}
BACKUP_SESSIONS = {'BackupJob', 'AgentBackup', 'EndpointBackup', 'BackupCopyJob', 'PlatformBackupJob'} | DB_SESSIONS
FILE_SESSIONS = {'FileLevelRestore', 'LinuxFileLevelRestore', 'NasRestore', 'FileInstantRecovery'}
VM_PLATFORMS = {'VMware', 'HyperV', 'CloudDirector', 'Nutanix', 'Proxmox', 'AWSEC2', 'AzureCompute', 'GCE',
                'WindowsPhysical', 'LinuxPhysical', 'LinuxPhysicalPpc'}
FILE_JOBS = {'FileBackup', 'FileBackupCopy', 'ObjectStorageBackup'}
VM_JOB_HINTS = ('VSphere', 'HyperV', 'CloudDirector', 'Nutanix', 'Proxmox', 'CloudBackup', 'AgentBackup')
STATUS = {'Success': 'SUCCESS', 'Warning': 'WARNING', 'Failed': 'FAILED'}


def kind(session_type):
    if session_type in BACKUP_SESSIONS:
        return 'BACKUP'
    if 'Restore' in session_type or session_type.endswith('InstantRecovery') or session_type == 'InstantDiskRecovery':
        return 'RESTORE'
    return None  # infra / maintenance noise, not stored


def workload(session_type='', platform=None, job_type=None):
    if session_type in DB_SESSIONS or platform in ('MongoDb', 'ApplicationBackupRepository'):
        return 'DB'
    if session_type in FILE_SESSIONS or platform == 'UnstructuredData' or job_type in FILE_JOBS:
        return 'FILE'
    if platform in VM_PLATFORMS or any(h in (job_type or '') for h in VM_JOB_HINTS):
        return 'VM'
    return 'OTHER'


def status(s):
    if s.get('state') != 'Stopped':
        return 'RUNNING'
    return STATUS.get((s.get('result') or {}).get('result'), 'FAILED')  # 'None' on a stopped session = didn't finish


def utc(s):
    if not s or s.startswith('0001'):
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# --- HTTP -----------------------------------------------------------------------------
def call(path, token=None, data=None, **params):
    url = BASE + path + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, data=data, headers={'x-api-version': '1.3-rev2', 'Accept': 'application/json'})
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            if data and r.headers.get('Date'):  # login only: VBR stamps sessions/restore points with its own clock
                skew = (datetime.now(timezone.utc) - parsedate_to_datetime(r.headers['Date'])).total_seconds()
                if abs(skew) > 300:
                    print(f'WARNING: Veeam server clock is {skew / 3600:+.1f} h off; backup times and RPO will be wrong', file=sys.stderr)
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ApiError(f'{e.code} {path}: {e.read()[:500].decode(errors="replace")}')


class ApiError(Exception):
    pass


def optional(name, fn, default):
    # Community Edition answers 400 "License restrictions apply." on some endpoints; skip that section, keep the rest
    try:
        return fn()
    except ApiError as e:
        print(f'skipped {name}: {e}', file=sys.stderr)
        return default


def login():
    form = {'grant_type': 'password', 'username': os.environ['VEEAM_USER'], 'password': os.environ['VEEAM_PASS']}
    return call('/api/oauth2/token', data=urllib.parse.urlencode(form).encode())['access_token']


def paged(token, path, **params):
    skip = 0
    while True:
        page = call(path, token, skip=skip, limit=500, **params)
        yield from page['data']
        skip += len(page['data'])
        if not page['data'] or skip >= (page.get('pagination') or {}).get('total', 0):
            return


# --- collect --------------------------------------------------------------------------
def collect(db, token):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    job_types = optional('jobs', lambda: {j['id']: j.get('type') for j in paged(token, '/api/v1/jobs/states')}, {})

    # re-pull from the oldest still-running session (so it gets its final status), else from the newest seen
    since = db.execute("""SELECT coalesce((SELECT min(start_time) FROM runs WHERE vendor=? AND status='RUNNING'),
                                          (SELECT max(start_time) FROM runs WHERE vendor=?))""", (VENDOR, VENDOR)).fetchone()[0]
    since = since or (datetime.now(timezone.utc) - timedelta(days=int(os.environ.get('LOOKBACK_DAYS', 30)))).strftime('%Y-%m-%d %H:%M:%S')
    sessions = optional('sessions', lambda: list(paged(token, '/api/v1/sessions', createdAfterFilter=since.replace(' ', 'T') + 'Z')), [])
    runs = [(VENDOR, s['id'], s.get('jobId'), s.get('name'), kind(s['sessionType']),
             workload(s['sessionType'], s.get('platformName'), job_types.get(s.get('jobId'))),
             status(s), utc(s['creationTime']), utc(s.get('endTime')),
             (s.get('result') or {}).get('message'), s['sessionType'])
            for s in sessions if kind(s.get('sessionType', ''))]

    cap = [(VENDOR, 'STORAGE', r['name'], r.get('capacityGB'), r.get('usedSpaceGB'), 'GB', now)
           for r in optional('storage', lambda: list(paged(token, '/api/v1/backupInfrastructure/repositories/states', excludeExtents='true')), [])]
    lic = optional('license', lambda: call('/api/v1/license', token), {})
    for key, total, used, unit in [('instanceLicenseSummary', 'licensedInstancesNumber', 'usedInstancesNumber', 'instances'),
                                   ('socketLicenseSummary', 'licensedSocketsNumber', 'usedSocketsNumber', 'sockets'),
                                   ('capacityLicenseSummary', 'licensedCapacityTb', 'usedCapacityTb', 'TB')]:
        if lic.get(key):
            cap.append((VENDOR, 'LICENSE', f"{lic.get('edition')} {unit}", lic[key].get(total), lic[key].get(used), unit, now))

    # ponytail: one restorePoints call per object; fine for POC scale, switch to a bulk createdAfter scan at ~1000s of objects
    objs = []
    for o in optional('objects', lambda: list(paged(token, '/api/v1/backupObjects')), []):
        rp = optional('restorePoints', lambda: call('/api/v1/restorePoints', token, backupObjectIdFilter=o['id'],
                                                   orderColumn='CreationTime', orderAsc='false', limit=1)['data'], [])
        objs.append((VENDOR, o['id'], o.get('name'), workload(platform=o.get('platformName')),
                     utc(rp[0]['creationTime']) if rp else None, int(bool(o.get('lastRunFailed'))), now))

    with db:  # all-or-nothing write
        db.executemany('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)', runs)
        db.executemany('INSERT OR REPLACE INTO capacity VALUES (?,?,?,?,?,?,?)', cap)
        db.executemany('INSERT OR REPLACE INTO protected_objects VALUES (?,?,?,?,?,?,?)', objs)
    print(f'{now} runs={len(runs)} capacity={len(cap)} objects={len(objs)}')


def selftest():
    assert kind('BackupJob') == 'BACKUP' and kind('SqlLogBackup') == 'BACKUP'
    assert kind('RestoreVm') == kind('FileLevelRestore') == kind('InstantRecovery') == 'RESTORE'
    assert kind('RepositoryMaintenance') is None and kind('ConfigurationBackup') is None
    assert workload('BackupJob', 'VMware') == 'VM' and workload('AgentBackup', None, 'LinuxAgentBackup') == 'VM'
    assert workload('BackupJob', None, 'FileBackup') == 'FILE' and workload('OracleLogBackup', 'VMware') == 'DB'
    assert workload('BackupJob') == 'OTHER'
    assert status({'state': 'Working'}) == 'RUNNING'
    assert status({'state': 'Stopped', 'result': {'result': 'Warning'}}) == 'WARNING'
    assert status({'state': 'Stopped', 'result': {'result': 'None'}}) == 'FAILED'
    assert utc('2026-10-05T10:00:00.1234567+05:30') == '2026-10-05 04:30:00' and utc('0001-01-01T00:00:00') is None
    db = sqlite3.connect(':memory:')
    db.executescript(open(SCHEMA).read())
    db.executemany('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
        ('veeam', '1', 'j', 'a', 'BACKUP', 'VM', 'FAILED', '2026-10-05 01:00:00', None, 'x', 'BackupJob'),
        ('veeam', '2', 'j', 'a', 'BACKUP', 'VM', 'SUCCESS', '2026-10-05 02:00:00', None, '', 'BackupJob'),
        ('commvault', '1', 'j', 'b', 'RESTORE', 'DB', 'SUCCESS', '2026-10-05 01:00:00', '2026-10-05 01:30:00', '', 'x')])
    assert db.execute('SELECT * FROM v_backup_summary').fetchall() == [('veeam', 2, 1, 0, 1)]
    assert db.execute('SELECT * FROM v_failures').fetchall() == [('veeam', 'VM', 1)]
    assert db.execute('SELECT avg_rto_min FROM v_rto').fetchone()[0] == 30.0
    # same session seen again on the next poll (RUNNING -> FAILED) must update in place, not duplicate
    db.execute("INSERT OR REPLACE INTO runs VALUES ('veeam','3','j','a','BACKUP','VM','RUNNING','2026-10-05 03:00:00',NULL,'','BackupJob')")
    db.execute("INSERT OR REPLACE INTO runs VALUES ('veeam','3','j','a','BACKUP','VM','FAILED','2026-10-05 03:00:00','2026-10-05 03:10:00','x','BackupJob')")
    assert db.execute("SELECT count(*), max(status) FROM runs WHERE run_id='3'").fetchone() == (1, 'FAILED')
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False) as f:
        f.write('# c\nT_A=x#y\nT_B="q w"\nT_C=\'\'\n\nT_D = 1 \n')
    load_env(f.name)
    os.remove(f.name)
    assert (os.environ['T_A'], os.environ['T_B'], os.environ['T_C'], os.environ['T_D']) == ('x#y', 'q w', '', '1')
    print('selftest ok')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
    else:
        db = sqlite3.connect(DB_PATH)
        db.executescript(open(SCHEMA).read())
        try:
            collect(db, login())
        except ApiError as e:
            sys.exit(str(e))
