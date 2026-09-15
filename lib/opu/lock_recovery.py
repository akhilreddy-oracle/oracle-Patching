"""Recover one provably inherited Oracle service lock; never change task state.

The CLI has no test mode, command input, process-ID input, or lock-path override.
Tests import pure inspection helpers or substitute the Runner in memory.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
HOST_LOCK = Path('/var/lib/oracle-patching-utility/locks/host-mutation.lock')
AUDIT_ROOT = Path('/var/lib/oracle-patching-utility/lock-recovery')
NATIVE_ROOT = Path('/var/lib/oracle-patching-utility/single-instance/plans')
LOCK_ERROR = 'opu-agent: another Oracle executor owns this host or its lock file is unsafe'
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


class Blocked(Exception):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def require(condition, message):
    if not condition:
        raise Blocked(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sealed(value, field='record_sha256'):
    body = {key: item for key, item in value.items() if key != field}
    require(value.get(field) == digest(canonical(body)), f'{field} integrity check failed')
    return value


def seal(value):
    body = {key: item for key, item in value.items() if key != 'record_sha256'}
    return {**body, 'record_sha256': digest(canonical(body))}


def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def identity(info):
    return [info.st_dev, info.st_ino]


def safe_file(path, *, root_owned=True):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f'unsafe regular file: {path}')
    require(not root_owned or (info.st_uid == 0 and info.st_mode & 0o022 == 0), f'file is not protected by root: {path}')
    return info


def _read_checked_file(path, validator):
    before = validator(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        require(identity(before) == identity(os.fstat(fd)), f'file changed while opening: {path}')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        require(len(data) <= 8 * 1024 * 1024, 'record exceeds size limit')
        require(identity(before) == identity(validator(path)), f'file replaced while reading: {path}')
        return data
    finally:
        os.close(fd)


def read_file(path):
    return _read_checked_file(path, safe_file)


def read_controller_authority(path, plan_dir):
    """Only the two tar-mirrored authority documents may retain controller UID.

    Ownership here is not an authority grant. Their exact bytes must later bind
    to the root-protected native apply claim and completed execution evidence.
    """
    require(path.parent == plan_dir and path.name in {'authorization.json', 'approval.json'},
            'unsupported mirrored authority document')
    directory = plan_dir.lstat()
    require(stat.S_ISDIR(directory.st_mode) and directory.st_mode & 0o022 == 0,
            'mirrored plan directory is unsafe or group/world writable')

    def validate(candidate):
        info = safe_file(candidate, root_owned=False)
        require(info.st_uid in {0, directory.st_uid} and info.st_mode & 0o022 == 0,
                f'mirrored authority has unexpected ownership or group/world write permission: {candidate}')
        return info

    return _read_checked_file(path, validate)


def read_oratab(target):
    """Installer-owned mapping is a crosscheck, never a target selector."""
    path = Path('/etc/oratab')
    metadata = {'path': str(path)}
    try:
        root_parents(path)
        account = pwd.getpwnam(target['owner'])
        require(account.pw_uid == target['uid'], 'Oracle account UID changed before oratab check')

        def validate(candidate):
            info = candidate.lstat()
            metadata.update(uid=info.st_uid, gid=info.st_gid, mode=oct(stat.S_IMODE(info.st_mode)),
                            regular=stat.S_ISREG(info.st_mode), nlink=info.st_nlink,
                            identity=identity(info), oracle_primary_gid=account.pw_gid)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                    'oratab must be a regular non-symlink single-link file')
            require(info.st_uid in {0, target['uid']}, 'oratab owner is neither root nor the sealed Oracle account')
            require(info.st_mode & 0o002 == 0, 'oratab is world writable')
            require(info.st_mode & 0o020 == 0 or info.st_gid == account.pw_gid,
                    'group-writable oratab is not owned by the Oracle primary group')
            return info

        return _read_checked_file(path, validate), metadata
    except (Blocked, OSError) as exc:
        raise Blocked(str(exc), {'oratab': metadata}) from exc


def verify_native_apply_authority(args, plan, previous, auth_bytes, auth):
    """Root execution artifacts anchor authority copied from the controller."""
    task_id = previous['task_id']
    require(isinstance(task_id, str) and IDENTIFIER.fullmatch(task_id), 'invalid preceding apply task identity')
    generation = previous.get('retry_count', 0)
    require(type(generation) is int and 0 <= generation <= 1000000, 'invalid preceding apply attempt')
    leaf = task_id + (f'-retry{generation}' if generation else '')
    native = NATIVE_ROOT / args.plan_id / 'tasks' / leaf
    root_parents(native / 'claim.json')
    claim = json.loads(read_file(native / 'claim.json'))
    definition = {key: claim.get(key) for key in ('schema_version', 'task_id', 'plan_id', 'plan_sha256',
                                                'stage', 'node', 'adapter', 'authorization_sha256',
                                                'authorization_record_sha256')}
    definition_sha = digest(canonical(definition))
    require(claim.get('task_definition_sha256') == definition_sha == previous.get('task_definition_sha256')
            and claim.get('plan_id') == args.plan_id and claim.get('plan_sha256') == plan['plan_sha256']
            and claim.get('task_id') == task_id and claim.get('stage') == 'apply'
            and claim.get('node') == plan['nodes'][0] and claim.get('adapter') == 'database_single_instance_opatch'
            and claim.get('status') == 'running' and claim.get('claimed_by') == args.actor
            and claim.get('retry_count', 0) == generation,
            'root-protected native apply claim does not bind the completed task and actor')
    require(claim.get('authorization_sha256') == digest(auth_bytes)
            and claim.get('authorization_record_sha256') == auth['record_sha256'],
            'mirrored authorization differs from the root-protected native apply claim')
    evidence_bytes = read_file(native / 'evidence.json')
    evidence = sealed(json.loads(evidence_bytes))
    require(digest(evidence_bytes) == previous.get('evidence_sha256')
            and evidence['record_sha256'] == previous.get('evidence_record_sha256')
            and evidence.get('plan_id') == args.plan_id and evidence.get('plan_sha256') == plan['plan_sha256']
            and evidence.get('task_id') == task_id and evidence.get('stage') == 'apply'
            and evidence.get('actor') == args.actor and evidence.get('status') == 'succeeded'
            and evidence.get('postcondition', {}).get('status') == 'passed'
            and evidence.get('exit_code') == 0 and evidence.get('retry_count', 0) == generation,
            'root-protected native apply result does not bind the completed task and actor')


def root_parents(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0,
                f'lock or audit parent is not a protected root directory: {parent}')


def open_lock(path):
    root_parents(path)
    before = safe_file(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(identity(before) == identity(os.fstat(fd)) == identity(safe_file(path)), 'host lock inode changed')
        return fd, identity(before)
    except BaseException:
        os.close(fd)
        raise


def lock_held(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def proc_identity(directory):
    raw = (directory / 'stat').read_text()
    # The comm field may contain spaces or parentheses; fields after its last
    # ')' begin at field 3, so starttime (field 22) has offset 19.
    fields = raw[raw.rindex(')') + 2:].split()
    status = (directory / 'status').read_text()
    uids = re.search(r'^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', status, re.M)
    require(uids is not None and len(set(uids.groups())) == 1, f'mixed process UIDs: {directory.name}')
    argv = [arg.decode('utf-8', 'strict') for arg in (directory / 'cmdline').read_bytes().split(b'\0') if arg]
    return {'pid': int(directory.name), 'starttime': int(fields[19]), 'uid': int(uids[1]),
            'exe': os.readlink(directory / 'exe'), 'argv': argv}


def classify_holder(process, target):
    if process['uid'] != target['uid']:
        return 'unknown'
    home = target['canonical_home']
    argv = process['argv']
    if (process['exe'] == home + '/bin/oracle' and len(argv) == 1
            and re.fullmatch(r'ora_[a-z0-9]+_' + re.escape(target['oracle_sid']), argv[0])):
        return 'database_service'
    if (process['exe'] == home + '/bin/tnslsnr' and len(argv) in (2, 3)
            and argv[0] in (home + '/bin/tnslsnr', target['oracle_home'] + '/bin/tnslsnr')
            and argv[1] == target['listener'] and (len(argv) == 2 or argv[2] == '-inherit')):
        return 'listener_service'
    return 'unknown'


def foreground_candidate(process, target):
    # This selects diagnostic candidates only; it never grants recovery scope.
    return (process.get('uid') == target['uid']
            and process.get('exe') == target['canonical_home'] + '/bin/oracle'
            and process.get('argv') == ['oracle' + target['oracle_sid'], '(LOCAL=NO)'])


def sanitize_session(value):
    if value is None:
        return None
    require(isinstance(value, str), 'session metadata must be text or null')
    return re.sub(r'[\x00-\x1f\x7f]', '', value)[:256]


def eligible_holders(processes, target, diagnostics):
    """Promote only exact, currently idle dedicated Oracle session holders."""
    require(diagnostics.get('status') == 'observed' and diagnostics.get('all_user_sessions_complete') is True,
            'complete target USER session evidence is required')
    require(diagnostics.get('running_rman_rows') == 0 and type(diagnostics.get('running_rman_rows')) is int,
            'running RMAN work prevents a database restart')
    require(type(diagnostics.get('collector_sid_excluded')) is int and diagnostics['collector_sid_excluded'] > 0,
            'collector session exclusion is unverified')
    candidates = [process for process in processes if foreground_candidate(process, target)]
    require(diagnostics.get('candidate_pids') == sorted(process['pid'] for process in candidates),
            'session evidence does not bind the current foreground holder set')
    require(diagnostics.get('unmapped_pids') == [], 'foreground holder has no verified Oracle session')
    users = diagnostics.get('target_user_sessions')
    mappings = diagnostics.get('sessions')
    require(isinstance(users, list) and isinstance(mappings, list), 'session evidence arrays are missing')
    seen_sessions = set()
    for row in users:
        require(isinstance(row, dict) and row.get('session_type') == 'USER'
                and row.get('session_status') == 'INACTIVE' and row.get('server') == 'DEDICATED',
                'every target USER session must be inactive and dedicated before restart')
        require(row.get('transaction_address_present') is False and row.get('transaction_exists') is False,
                'a target USER session has active or unknown transaction state')
        require(type(row.get('pid')) is int and row['pid'] > 1
                and type(row.get('sid')) is int and row['sid'] > 0
                and type(row.get('serial')) is int and row['serial'] >= 0
                and row.get('process_matches') == 1 and type(row.get('process_matches')) is int
                and row.get('process_session_count') == 1 and type(row.get('process_session_count')) is int
                and isinstance(row.get('process_address'), str)
                and re.fullmatch(r'[A-Fa-f0-9]{8,32}', row['process_address']),
                'ambiguous or missing USER process/session identity prevents restart')
        key = (row['sid'], row['serial'])
        require(key not in seen_sessions and row['sid'] != diagnostics['collector_sid_excluded'],
                'duplicate USER session or collector-session evidence')
        seen_sessions.add(key)
        labels = ' '.join(str(row.get(field) or '') for field in ('program', 'module', 'action', 'client_info'))
        require(re.search(r'(?i)\b(?:rman|opatch(?:auto)?|datapatch|sqlpatch|dbua)\b', labels) is None,
                'Oracle maintenance client metadata prevents automatic restart')
    enriched = []
    for process in processes:
        item = dict(process)
        if foreground_candidate(process, target):
            matched = [row for row in mappings if row.get('pid') == process['pid']]
            require(len(matched) == 1, 'foreground PID has missing or ambiguous native session mapping')
            row = matched[0]
            require(row in users and row.get('candidate_holder') is True
                    and row.get('process_starttime') == process['starttime'],
                    'foreground session mapping does not bind the current kernel process identity')
            item['classification'] = 'database_session'
            # Elapsed idle time, observation time and mutable client labels are
            # deliberately absent. Fresh eligibility is verified separately.
            item['session_identity'] = {key: row[key] for key in ('pid', 'process_starttime', 'process_address', 'sid', 'serial')}
        enriched.append(item)
    return enriched


def holders(lock_identity, target, proc_root=Path('/proc')):
    found = []
    for directory in sorted(proc_root.iterdir(), key=lambda p: p.name):
        if not directory.name.isdigit() or int(directory.name) == os.getpid():
            continue
        try:
            descriptors = list((directory / 'fd').iterdir())
            matches = []
            for descriptor in descriptors:
                try:
                    if identity(descriptor.stat()) == lock_identity:
                        matches.append(int(descriptor.name))
                except FileNotFoundError:
                    continue
            if not matches:
                continue
            process = proc_identity(directory)
            process['fds'] = sorted(matches)
            process['classification'] = classify_holder(process, target)
            # Detect PID reuse or changed credentials/executable during scan.
            again = proc_identity(directory)
            require(all(process[key] == again[key] for key in again), 'lock holder changed during inspection')
            found.append(process)
        except FileNotFoundError:
            # A vanished process cannot retain a lock. A fresh second scan is
            # required before any service operation.
            continue
        except (PermissionError, UnicodeError, ValueError) as exc:
            raise Blocked(f'cannot inspect every process descriptor: {directory.name}: {exc}') from exc
    return found


def require_no_executor(proc_root=Path('/proc')):
    prohibited = {'opu-database-single-instance-patch', 'opu-database-single-instance-rollback',
                  'opu-database-recovery-prepare', 'opu-recovery-evidence-collect',
                  'rman', 'opatch', 'opatchauto', 'datapatch', 'sqlpatch', 'sqlpatch.pl'}
    for directory in proc_root.iterdir():
        if not directory.name.isdigit() or int(directory.name) == os.getpid():
            continue
        try:
            argv = (directory / 'cmdline').read_bytes().split(b'\0')
            # Inspect individual arguments, never execute or parse shell input.
            names = {Path(part.decode('utf-8', 'strict')).name for part in argv if part}
            require(not names.intersection(prohibited), f'another Oracle executor or RMAN process is active: {directory.name}')
        except FileNotFoundError:
            continue
        except (PermissionError, UnicodeError) as exc:
            raise Blocked(f'cannot exclude active Oracle executors: {directory.name}: {exc}') from exc


def validate_wrapper(run_dir):
    require(run_dir.is_dir() and not run_dir.is_symlink(), 'launch directory is missing or unsafe')
    rc = read_file(run_dir / 'rc')
    stdout = read_file(run_dir / 'stdout')
    stderr = read_file(run_dir / 'stderr')
    pid_raw = read_file(run_dir / 'pid').decode().strip()
    require(rc.strip() == b'75' and stdout.strip() == b'' and stderr.decode().strip() == LOCK_ERROR,
            'launch is not an exact pre-claim host-lock rejection')
    require(pid_raw.isdigit() and int(pid_raw) > 1, 'invalid launch PID')
    require(not Path('/proc', pid_raw).exists(), 'launch PID is still present; cannot prove termination')
    return {'path': str(run_dir), 'pid': int(pid_raw), 'exit_code': 75,
            'rc_sha256': digest(rc), 'stdout_sha256': digest(stdout), 'stderr_sha256': digest(stderr)}


class Runner:
    def __init__(self):
        self.log_dir = None
        self.sequence = 0

    def run(self, argv, *, data=None, timeout=60):
        result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout,
                                close_fds=True, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                                                    'LANG': 'C', 'LC_ALL': 'C'})
        if self.log_dir:
            self.sequence += 1
            write_once(self.log_dir / f'{self.sequence:03d}.json',
                       {'argv': argv, 'returncode': result.returncode, 'stdout': result.stdout,
                        'stderr': result.stderr, 'finished_at': now()})
        require(result.returncode == 0, f'command failed ({result.returncode}): {Path(argv[0]).name}: {result.stderr[-500:]}')
        return result.stdout

    def native(self, plan_root, command, plan_id, task_id=None):
        argv = ['/usr/bin/env', f'OPU_PLAN_STATE_DIR={plan_root}', 'OPU_PLAN_WORKER_SNAPSHOT=1',
                str(ROOT / 'bin/opu-patch-plan'), command, '--plan-id', plan_id]
        if task_id:
            argv += ['--task-id', task_id]
        return json.loads(self.run(argv))

    def oracle(self, target, binary, args, sql=None, timeout=90):
        argv = ['/usr/sbin/runuser', '-u', target['owner'], '--', '/usr/bin/env',
                f"ORACLE_HOME={target['oracle_home']}", f"ORACLE_SID={target['oracle_sid']}",
                f"PATH={target['oracle_home']}/bin:/usr/bin:/bin", target['oracle_home'] + '/bin/' + binary] + args
        output = self.run(argv, data=sql, timeout=timeout)
        require(not re.search(r'(?m)^\s*(ORA-|SP2-)', output), f'{binary} returned an Oracle error')
        return output

    def sql(self, target, statements, timeout=90):
        sql = 'whenever sqlerror exit failure\nwhenever oserror exit failure\nset pages 0 feedback off heading off echo off trimspool on lines 32767\n' + statements + '\nexit;\n'
        return self.oracle(target, 'sqlplus', ['-L', '-s', '/ as sysdba'], sql, timeout)

    def sessions(self, target, candidates):
        """Read-only process/session/transaction mapping; no client SQL text."""
        pids = sorted({process['pid'] for process in candidates})
        require(len(pids) <= 256 and all(type(pid) is int and 1 < pid <= 4194304 for pid in pids),
                'invalid or excessive foreground process diagnostic scope')
        # The sole variable SQL fragment consists of checked kernel PID
        # integers from the inspected holder list. No external SQL is accepted.
        pid_literals = ','.join("'" + str(pid) + "'" for pid in pids) if pids else 'NULL'
        output = self.sql(target, """
select 'OPU_SESSION|' || json_object(
  'pid' value p.spid, 'sid' value s.sid, 'serial' value s.serial#,
  'process_address' value rawtohex(p.addr),
  'process_matches' value (select count(*) from v$process px where px.spid = p.spid),
  'process_session_count' value (select count(*) from v$session sx where sx.paddr = p.addr),
  'session_type' value s.type, 'session_status' value s.status,
  'server' value s.server, 'program' value s.program, 'module' value s.module,
  'action' value s.action, 'client_info' value s.client_info,
  'username' value s.username, 'machine' value s.machine,
  'last_call_seconds' value s.last_call_et,
  'transaction_address_present' value case when s.taddr is null then 0 else 1 end,
  'transaction_exists' value case when t.addr is null then 0 else 1 end)
from v$process p
full outer join v$session s on s.paddr = p.addr
left join v$transaction t on t.ses_addr = s.saddr
where (p.spid in (""" + pid_literals + """) or s.type = 'USER')
and (s.sid is null or s.sid <> to_number(sys_context('USERENV', 'SID')))
order by p.spid, s.sid
fetch first 1025 rows only;
select 'OPU_COLLECTOR|' || sys_context('USERENV', 'SID') from dual;
select 'OPU_RMAN|' || count(*) from v$rman_status where status like 'RUNNING%';
""")
        rows = []
        collector_rows = []
        rman_rows = []
        identities = {process['pid']: process['starttime'] for process in candidates}
        for line in output.splitlines():
            if line.strip().startswith('OPU_COLLECTOR|'):
                collector_rows.append(line.strip().split('|', 1)[1])
                continue
            if line.strip().startswith('OPU_RMAN|'):
                rman_rows.append(line.strip().split('|', 1)[1])
                continue
            if not line.strip().startswith('OPU_SESSION|'):
                continue
            row = json.loads(line.strip().split('|', 1)[1])
            require(isinstance(row, dict) and ((isinstance(row.get('pid'), str) and row['pid'].isdigit())
                    or (row.get('pid') is None and row.get('session_type') == 'USER')),
                    'invalid native process/session mapping')
            pid = int(row['pid']) if row.get('pid') is not None else None
            require(pid in identities or row.get('session_type') == 'USER', 'session mapping returned an unrelated background process')
            entry = {'pid': pid, 'process_starttime': identities.get(pid), 'candidate_holder': pid in identities}
            for key in ('sid', 'serial', 'last_call_seconds', 'process_matches', 'process_session_count'):
                require(row.get(key) is None or (type(row.get(key)) is int and row[key] >= 0),
                        'invalid numeric session metadata')
                entry[key] = row.get(key)
            for key in ('session_type', 'session_status', 'server', 'program', 'module', 'action', 'client_info',
                        'username', 'machine', 'process_address'):
                entry[key] = sanitize_session(row.get(key))
            for key in ('transaction_exists', 'transaction_address_present'):
                require(type(row.get(key)) is int and row[key] in (0, 1), 'transaction existence could not be determined')
                entry[key] = bool(row[key])
            rows.append(entry)
        require(len(rows) <= 1024, 'session mapping exceeded diagnostic row limit')
        require(len(collector_rows) == 1 and collector_rows[0].isdigit(), 'collector session identity was not established')
        require(len(rman_rows) == 1 and rman_rows[0].isdigit(), 'running RMAN work could not be determined')
        collector_sid = int(collector_rows[0])
        require(all(row['sid'] != collector_sid for row in rows), 'collector session was not excluded')
        mapped = {row['pid'] for row in rows if row['candidate_holder'] and row['sid'] is not None}
        return {'status': 'observed', 'observed_at': now(), 'candidate_pids': pids,
                'sessions': [row for row in rows if row['candidate_holder']],
                'target_user_sessions': [row for row in rows if row['session_type'] == 'USER'],
                'all_user_sessions_complete': True, 'collector_sid_excluded': collector_sid,
                'running_rman_rows': int(rman_rows[0]),
                'unmapped_pids': sorted(set(pids) - mapped), 'read_only': True}

    def health(self, target):
        output = self.sql(target, "select 'OPU|' || d.dbid || '|' || d.db_unique_name || '|' || i.instance_name || '|' || i.status || '|' || d.database_role || '|' || d.open_mode from v$database d cross join v$instance i;")
        rows = [line.strip().split('|') for line in output.splitlines() if line.strip().startswith('OPU|')]
        require(len(rows) == 1 and len(rows[0]) == 7, 'cannot establish database identity and health')
        _, dbid, database, sid, status, role, mode = rows[0]
        require(dbid.isdigit() and database == target['database_unique_name'] and sid == target['oracle_sid']
                and status == 'OPEN' and role == 'PRIMARY' and mode == 'READ WRITE', 'target database is not PRIMARY OPEN READ WRITE')
        listener = self.oracle(target, 'lsnrctl', ['services', target['listener']])
        require(re.search(r'Instance "?' + re.escape(sid) + r'"?, status READY', listener) is not None,
                'bound listener does not report the target instance READY')
        return {'dbid': dbid, 'database_unique_name': database, 'oracle_sid': sid,
                'instance_status': status, 'database_role': role, 'open_mode': mode,
                'listener_ready': True, 'observed_at': now()}


def target_context(args, runner):
    plan_root = Path(os.environ.get('OPU_PLAN_STATE_DIR', str(ROOT / 'var/webapp-plans')))
    require(plan_root == ROOT / 'var/webapp-plans', 'plan state directory must be this deployed tool root/var/webapp-plans')
    plan_dir = plan_root / 'plans' / args.plan_id
    # Native status verifies plan seal; task-status also verifies definition,
    # result seals and complete custody of the preceding apply evidence.
    plan = runner.native(plan_root, 'status', args.plan_id)
    require(plan.get('state') == 'running' and plan.get('intent', 'patch_apply') == 'patch_apply', 'plan must be running patch_apply')
    require(plan['procedure']['adapter'] == 'database_single_instance_opatch' and len(plan['nodes']) == 1,
            'only standalone single-node OPatch apply plans are supported')
    window = plan['maintenance_window']
    epoch = dt.datetime.now(dt.timezone.utc)
    require(dt.datetime.fromisoformat(window['start'].replace('Z', '+00:00')) <= epoch < dt.datetime.fromisoformat(window['end'].replace('Z', '+00:00')),
            'maintenance window is not open')
    auth_bytes = read_controller_authority(plan_dir / 'authorization.json', plan_dir)
    auth = sealed(json.loads(auth_bytes))
    require(auth.get('actor') == args.actor and auth.get('decision') == 'execution_authorized'
            and auth.get('plan_id') == args.plan_id and auth.get('plan_sha256') == plan['plan_sha256'], 'actor is not the sealed authorizer of this plan')
    approval_bytes = read_controller_authority(plan_dir / 'approval.json', plan_dir)
    approval = sealed(json.loads(approval_bytes))
    require(auth.get('approval', {}).get('sha256') == digest(approval_bytes)
            and auth.get('approval', {}).get('record_sha256') == approval['record_sha256']
            and approval.get('plan_id') == args.plan_id and approval.get('plan_sha256') == plan['plan_sha256']
            and approval.get('decision') == 'approved' and approval.get('requester') == plan.get('requester')
            and len({plan.get('requester'), approval.get('actor'), args.actor}) == 3,
            'sealed approval is not bound to this authorization')
    task = runner.native(plan_root, 'task-status', args.plan_id, args.task_id)
    require(task.get('status') == 'pending' and task.get('stage') == 'validate'
            and task.get('node') == plan['nodes'][0] and task.get('retry_count', 0) == 0,
            'only an unclaimed first-attempt pending validate task is recoverable')
    require(not any(key in task for key in ('claimed_by', 'evidence_sha256', 'completed_at_epoch')),
            'task was already claimed or completed')
    require(task.get('authorization_sha256') == digest(auth_bytes) and task.get('authorization_record_sha256') == auth['record_sha256'], 'task authorization changed')
    next_task = runner.native(plan_root, 'next', args.plan_id)
    require(next_task == task, 'requested validate task is not the next pending task')
    task_paths = sorted((plan_dir / 'tasks').glob('*.json'))
    previous = None
    for path in task_paths:
        item = runner.native(plan_root, 'task-status', args.plan_id, path.stem)
        require(item.get('status') != 'running', 'another task is running')
        if item['task_id'] == args.task_id:
            require(previous and previous['stage'] == 'apply' and previous['status'] == 'succeeded', 'preceding sealed apply did not succeed')
            break
        previous = item
    require(previous is not None, 'preceding apply task is missing')
    verify_native_apply_authority(args, plan, previous, auth_bytes, auth)
    binding = sealed(json.loads(read_file(NATIVE_ROOT / args.plan_id / 'target-binding.json')))
    require(binding['plan_id'] == args.plan_id and binding['plan_sha256'] == plan['plan_sha256'], 'native target binding changed')
    target = binding['target']
    for key in ('database_unique_name', 'oracle_home', 'owner'):
        require(target[key] == plan['target'][key], 'bound target differs from plan')
    for key in ('owner', 'oracle_sid', 'listener', 'database_unique_name'):
        require(isinstance(target.get(key), str) and IDENTIFIER.fullmatch(target[key]), f'invalid or missing target {key}')
    require(re.fullmatch(r'/[A-Za-z0-9_./-]+', target['oracle_home']) is not None, 'unsafe Oracle home path')
    target = dict(target)
    target['canonical_home'] = str(Path(target['oracle_home']).resolve(strict=True))
    account = pwd.getpwnam(target['owner'])
    require(account.pw_uid > 0, 'Oracle owner cannot be root')
    target['uid'] = account.pw_uid
    require(Path(target['canonical_home']).stat().st_uid == target['uid'], 'Oracle home owner differs from plan')
    entries = []
    oratab_bytes, oratab_metadata = read_oratab(target)
    for line in oratab_bytes.decode().splitlines():
        fields = line.strip().split(':')
        if line.strip() and not line.lstrip().startswith('#') and len(fields) >= 3:
            if str(Path(fields[1]).resolve()) == target['canonical_home']:
                entries.append(fields[0])
    if entries != [target['oracle_sid']]:
        raise Blocked('oratab does not uniquely bind the target home and SID', {'oratab': oratab_metadata})
    require(not (NATIVE_ROOT / args.plan_id / 'tasks' / args.task_id).exists(), 'native task attempt already exists')
    return plan, task, target


def inspect(args, runner):
    result = {'schema_version': '1.0', 'collector': {'name': 'oracle.database.lock_recovery', 'version': '1'},
              'operation': 'inspect', 'plan_id': args.plan_id, 'task_id': args.task_id, 'run_id': args.run_id,
              'actor': args.actor, 'observed_at': now(), 'status': 'blocked', 'recovery_eligible': False, 'blockers': []}
    try:
        plan, task, target = target_context(args, runner)
        result.update(plan_sha256=plan['plan_sha256'], task_definition_sha256=task['task_definition_sha256'],
                      original_task_status=task['status'], target=target)
        result['wrapper'] = validate_wrapper(ROOT / 'var/webapp-runs' / args.plan_id / args.task_id / args.run_id)
        require_no_executor()
        fd, lock_identity = open_lock(HOST_LOCK)
        try:
            result['lock'] = {'path': str(HOST_LOCK), 'identity': lock_identity, 'safe': True, 'held': lock_held(fd)}
            observed_holders = holders(lock_identity, target)
            result['holders'] = observed_holders
            require(result['lock']['held'] and result['holders'], 'host lock is no longer held by inherited services')
            candidates = [process for process in result['holders'] if foreground_candidate(process, target)]
            try:
                result['foreground_session_diagnostics'] = runner.sessions(target, candidates)
            except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                result['foreground_session_diagnostics'] = {'status': 'unavailable', 'error': str(exc), 'read_only': True}
            result['holders'] = eligible_holders(observed_holders, target, result['foreground_session_diagnostics'])
            require(all(p['classification'] != 'unknown' for p in result['holders']), 'unrecognized lock holder; automatic recovery is forbidden')
            require(observed_holders == holders(lock_identity, target), 'lock holder identities changed; inspect again')
            result['health_before'] = runner.health(target)
            extjob = Path(target['canonical_home']) / 'bin/extjob'
            info = extjob.lstat()
            result['extjob'] = {'path': str(extjob), 'uid': info.st_uid, 'gid': info.st_gid,
                                'mode': oct(stat.S_IMODE(info.st_mode)), 'regular': stat.S_ISREG(info.st_mode), 'nlink': info.st_nlink}
            require(identity(safe_file(HOST_LOCK)) == lock_identity, 'lock inode changed during inspection')
        finally:
            os.close(fd)
        result.update(status='eligible', recovery_eligible=True)
    except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result['blockers'].append(str(exc))
        result.update(getattr(exc, 'diagnostics', {}))
    return seal(result)


def write_once(path, value):
    data = canonical(seal(value)) + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def protected_directory(path):
    if not path.exists():
        protected_directory(path.parent)
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0,
            f'unsafe recovery directory: {path}')


def recover(args, runner):
    protected_directory(AUDIT_ROOT)
    operation_lock = AUDIT_ROOT / 'operation.lock'
    if not operation_lock.exists():
        try:
            os.close(os.open(operation_lock, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_RDWR, 0o600))
        except FileExistsError:
            pass
    exclusion, _ = open_lock(operation_lock)
    try:
        fcntl.flock(exclusion, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checked = inspect(args, runner)
        require(checked['recovery_eligible'], '; '.join(checked['blockers']))
        protected_directory(AUDIT_ROOT / args.plan_id)
        audit = AUDIT_ROOT / args.plan_id / args.run_id
        require(not audit.exists() and not audit.is_symlink(), 'recovery was already attempted; inspect its immutable outcome; do not retry')
        audit.mkdir(mode=0o700)
        write_once(audit / 'inspection.json', checked)
        runner.log_dir = audit
        target = checked['target']
        result = {key: value for key, value in checked.items() if key != 'record_sha256'}
        result.update(operation='recover', status='recovery_required', recovery_eligible=False,
                      started_at=now(), audit_directory=str(audit), plan_and_task_unchanged=True)
        host_fd = None
        owns_host = False
        stopped = False
        try:
            # Fresh authority and process snapshots immediately before outage.
            plan, task, fresh_target = target_context(args, runner)
            require(plan['plan_sha256'] == checked['plan_sha256'] and fresh_target == target
                    and task['task_definition_sha256'] == checked['task_definition_sha256'], 'sealed recovery scope changed')
            require(validate_wrapper(ROOT / 'var/webapp-runs' / args.plan_id / args.task_id / args.run_id) == checked['wrapper'], 'launch records changed')
            host_fd, lock_identity = open_lock(HOST_LOCK)
            require(lock_identity == checked['lock']['identity'] and lock_held(host_fd), 'host lock changed or became free')
            fresh_holders = holders(lock_identity, target)
            fresh_sessions = runner.sessions(target, [process for process in fresh_holders if foreground_candidate(process, target)])
            fresh_eligible = eligible_holders(fresh_holders, target, fresh_sessions)
            require(fresh_holders == holders(lock_identity, target), 'kernel holder identities changed during final workload check')
            require(fresh_eligible == checked['holders'], 'lock holder or session identities changed before recovery')
            result['session_checks_before_outage'] = fresh_sessions
            require_no_executor()
            write_once(audit / 'outage-started.json', {'started_at': now(), 'target': target})
            # Stop only the exact sealed listener and SID. Never signal PIDs.
            stopped = True
            runner.oracle(target, 'lsnrctl', ['stop', target['listener']])
            runner.sql(target, 'shutdown immediate;', timeout=600)
            require(not holders(lock_identity, target), 'processes still hold the host lock after service shutdown')
            fcntl.flock(host_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owns_host = True
            require(identity(safe_file(HOST_LOCK)) == lock_identity, 'host lock inode changed after shutdown')
            # close_fds=True on every subprocess prevents service inheritance.
            runner.sql(target, 'startup;', timeout=600)
            runner.oracle(target, 'lsnrctl', ['start', target['listener']])
            runner.sql(target, 'alter system register;')
            health = None
            for attempt in range(15):
                try:
                    health = runner.health(target)
                    break
                except Blocked:
                    if attempt == 14:
                        raise
                    time.sleep(2)
            require(health['dbid'] == checked['health_before']['dbid'], 'database DBID changed after restart')
            require(not holders(lock_identity, target), 'restarted services unexpectedly inherited the host lock')
            require(identity(safe_file(HOST_LOCK)) == lock_identity, 'host lock inode changed after restart')
            fcntl.flock(host_fd, fcntl.LOCK_UN)
            owns_host = False
            require(not lock_held(host_fd), 'another executor acquired the host lock before final verification')
            result['lock'].update(identity_after=lock_identity, inode_preserved=True,
                                  held_after=False, inherited_holders_after=[])
            result.update(status='completed', health_after=health, service_health=health, lock_released=True,
                          lock_inode_preserved=True, blockers=[])
        except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            result['blockers'] = [str(exc)]
            # Recover listener availability if the original database services
            # still hold the same lock after a failed shutdown. They must all
            # retain their original process identities; this is never grounds
            # to start the database or to touch an unfamiliar process.
            listener_only_guard = False
            if stopped and not owns_host and host_fd is not None:
                try:
                    require(identity(safe_file(HOST_LOCK)) == checked['lock']['identity'], 'guard lock inode changed')
                    remaining = holders(checked['lock']['identity'], target)
                    require_no_executor()
                    if lock_held(host_fd):
                        require(remaining and all(p in checked['holders'] and p['classification'] == 'database_service'
                                                  for p in remaining), 'guard cannot prove inherited database-only holders')
                        listener_only_guard = True
                    else:
                        require(not remaining, 'guard found descriptors after lock release')
                        fcntl.flock(host_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        owns_host = True
                except (Blocked, OSError) as guard_error:
                    result['guard_error'] = str(guard_error)
            if stopped and (owns_host or listener_only_guard):
                if owns_host:
                    try:
                        runner.sql(target, 'startup;', timeout=600)
                    except (Blocked, OSError, subprocess.SubprocessError):
                        pass
                try:
                    runner.oracle(target, 'lsnrctl', ['start', target['listener']])
                except (Blocked, OSError, subprocess.SubprocessError):
                    pass
                try:
                    if owns_host:
                        runner.sql(target, 'alter system register;')
                    result['guard_health'] = runner.health(target)
                except (Blocked, OSError, subprocess.SubprocessError) as guard_error:
                    result['guard_error'] = str(guard_error)
        finally:
            if host_fd is not None:
                os.close(host_fd)
            result['finished_at'] = now()
            write_once(audit / 'result.json', result)
            runner.log_dir = None
        return seal(result)
    finally:
        os.close(exclusion)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('inspect', 'recover'))
    for key in ('plan-id', 'task-id', 'run-id', 'actor'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == 0, 'native lock inspection and recovery require root')
        require(sys.platform.startswith('linux'), 'native lock recovery requires Linux procfs')
        for key in ('plan_id', 'task_id', 'actor'):
            require(IDENTIFIER.fullmatch(getattr(args, key)) is not None, f'invalid {key}')
        require(re.fullmatch(r'[a-f0-9]{32}', args.run_id) is not None, 'run-id must be the 32-hex remote launch suffix')
        runner = Runner()
        result = inspect(args, runner) if args.operation == 'inspect' else recover(args, runner)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in ('eligible', 'completed') else 65
    except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps(seal({'schema_version': '1.0', 'status': 'blocked', 'recovery_eligible': False,
                              'plan_id': args.plan_id, 'task_id': args.task_id, 'run_id': args.run_id,
                              'actor': args.actor, 'blockers': [str(exc)], 'observed_at': now(),
                              **getattr(exc, 'diagnostics', {})}), sort_keys=True))
        return 65


if __name__ == '__main__':
    raise SystemExit(main())
