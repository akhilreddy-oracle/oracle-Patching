"""Read-only comparison with retained home-backup bytes, never repair authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile

import lock_recovery as trust


ROOT = Path(__file__).resolve().parents[2]
BACKUP_PREFIX = Path('/u02/opu-backup')
MAX_EXTJOB_BYTES = 16 * 1024 * 1024


def stable(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def metadata(info):
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': format(stat.S_IMODE(info.st_mode), '04o'),
            'size': info.st_size, 'identity': trust.identity(info), 'nlink': info.st_nlink}


def hash_stream(stream, *, limit=None):
    value = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        trust.require(limit is None or total <= limit, 'extjob member exceeds the bounded size limit')
        value.update(chunk)
    return value.hexdigest(), total


def archive_path(value, target):
    path = Path(value)
    prefix = BACKUP_PREFIX / target['database_unique_name']
    trust.require(path.is_absolute() and re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
                  and '..' not in path.parts, 'invalid backup archive path')
    try:
        relative = path.relative_to(prefix)
    except ValueError as exc:
        raise trust.Blocked('archive is outside the sealed database backup prefix') from exc
    basename = PurePosixPath(target['oracle_home']).name
    trust.require(len(relative.parts) >= 3 and relative.parts[-2:] == ('oracle-home', basename + '.tar.gz'),
                  'archive does not have the fixed Oracle-home backup layout')
    return path, basename + '/bin/extjob'


def archive_member(stream, expected_member):
    found = None
    count = 0
    try:
        # Streaming never extracts names or follows archive links. Traversal and
        # absolute entry names are rejected even when they are unrelated files.
        with tarfile.open(fileobj=stream, mode='r|gz', ignore_zeros=True) as archive:
            for member in archive:
                count += 1
                trust.require(count <= 2000000, 'archive has excessive member count')
                name = PurePosixPath(member.name)
                trust.require(not name.is_absolute() and '..' not in name.parts
                              and not re.search(r'[\x00-\x1f\x7f]', member.name), 'archive contains an unsafe member name')
                if str(name) != expected_member:
                    continue
                trust.require(found is None, 'archive contains duplicate extjob members')
                trust.require(member.isreg() and not member.issym() and not member.islnk(),
                              'archive extjob member is not a regular independent file')
                trust.require(0 < member.size <= MAX_EXTJOB_BYTES, 'archive extjob size is outside the permitted range')
                reader = archive.extractfile(member)
                trust.require(reader is not None, 'archive member cannot be read')
                with reader:
                    sha, size = hash_stream(reader, limit=MAX_EXTJOB_BYTES)
                trust.require(size == member.size, 'archive extjob member was truncated')
                found = {'name': expected_member, 'sha256': sha, 'size': size,
                         'uid': member.uid, 'gid': member.gid, 'mode': format(member.mode & 0o7777, '04o'),
                         'regular': True, 'unique': True}
    except (tarfile.TarError, EOFError) as exc:
        raise trust.Blocked(f'backup is not a complete readable tar archive: {exc}') from exc
    trust.require(found is not None, 'archive contains no exact extjob member')
    return found, count


def inspect_archive(path, expected_sha, expected_member):
    trust.require(re.fullmatch(r'[a-f0-9]{64}', expected_sha), 'expected archive SHA must be 64 lowercase hex characters')
    # The fixed archive path and root-owned leaf are additional checks. The
    # supplied retained-audit digest is verified against this same open FD.
    before = trust.safe_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        trust.require(stable(os.fstat(fd)) == stable(before), 'archive changed while opening')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            actual_sha, size = hash_stream(stream)
            trust.require(actual_sha == expected_sha, 'archive SHA does not match the retained audit reference')
            trust.require(stable(os.fstat(fd)) == stable(before), 'archive changed during whole-file hashing')
            stream.seek(0)
            member, members_checked = archive_member(stream, expected_member)
        trust.require(stable(os.fstat(fd)) == stable(before) == stable(trust.safe_file(path)),
                      'archive file or pathname changed during member inspection')
        return {'path': str(path), 'sha256': actual_sha, 'expected_sha256': expected_sha,
                'size': size, 'metadata': metadata(before), 'members_checked': members_checked,
                'same_open_descriptor_verified': True}, member
    finally:
        os.close(fd)


def inspect_current(target):
    home_fd = os.open(target['oracle_home'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    bin_fd = fd = None
    try:
        home_before = os.fstat(home_fd)
        bin_fd = os.open('bin', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
        bin_before = os.fstat(bin_fd)
        fd = os.open('extjob', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=bin_fd)
        before = os.fstat(fd)
        trust.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                      and 0 < before.st_size <= MAX_EXTJOB_BYTES, 'current extjob must be a bounded regular single-link file')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            sha, size = hash_stream(stream, limit=MAX_EXTJOB_BYTES)
        named = os.stat('extjob', dir_fd=bin_fd, follow_symlinks=False)
        trust.require(stable(os.fstat(fd)) == stable(before) == stable(named), 'current extjob changed during hashing')
        trust.require(trust.identity(os.stat('bin', dir_fd=home_fd, follow_symlinks=False)) == trust.identity(bin_before)
                      and trust.identity(os.stat(target['oracle_home'], follow_symlinks=False)) == trust.identity(home_before),
                      'Oracle home or bin directory changed during current-file inspection')
        return {'path': target['oracle_home'] + '/bin/extjob', 'sha256': sha,
                **metadata(before), 'regular': True, 'same_open_descriptor_verified': True}
    finally:
        for opened in (fd, bin_fd, home_fd):
            if opened is not None:
                os.close(opened)


def target_context(args, runner):
    expected_root = trust.deployment_root(ROOT) / 'var/webapp-plans'
    plan_root = Path(os.environ.get('OPU_PLAN_STATE_DIR', str(expected_root)))
    trust.require(plan_root == expected_root, 'plan root must belong to this deployment')
    directory = plan_root / 'plans' / args.plan_id
    plan = runner.native(plan_root, 'status', args.plan_id)
    trust.require(plan.get('state') in {'running', 'paused'} and plan.get('intent', 'patch_apply') == 'patch_apply'
                  and plan.get('procedure', {}).get('adapter') == 'database_single_instance_opatch'
                  and len(plan.get('nodes', [])) == 1, 'only a running or paused standalone apply plan is supported')
    auth_bytes = trust.read_controller_authority(directory / 'authorization.json', directory)
    auth = trust.sealed(json.loads(auth_bytes))
    trust.require(auth.get('actor') == args.actor and auth.get('plan_id') == args.plan_id
                  and auth.get('plan_sha256') == plan['plan_sha256'] and auth.get('decision') == 'execution_authorized',
                  'actor is not the sealed plan authorizer')
    tasks = [runner.native(plan_root, 'task-status', args.plan_id, path.stem)
             for path in sorted((directory / 'tasks').glob('*.json'))]
    node = plan['nodes'][0]
    expected_tasks = {f'001-precheck-{node}': 'precheck', f'002-apply-{node}': 'apply',
                      f'003-validate-{node}': 'validate', '004-datapatch-local': 'datapatch',
                      '005-final-validate-local': 'final_validate'}
    trust.require(len(tasks) == len(expected_tasks) and {task.get('task_id') for task in tasks} == set(expected_tasks)
                  and all(task.get('plan_id') == args.plan_id and task.get('plan_sha256') == plan['plan_sha256']
                          and task.get('stage') == expected_tasks[task['task_id']] for task in tasks),
                  'inspection requires exactly the sealed standalone task chain')
    applies = [task for task in tasks if task.get('stage') == 'apply' and task.get('status') == 'succeeded']
    sql_tasks = [task for task in tasks if task.get('stage') == 'datapatch' and task.get('status') == 'succeeded']
    final_status = 'failed' if plan['state'] == 'paused' else 'pending'
    finals = [task for task in tasks if task.get('stage') == 'final_validate' and task.get('status') == final_status]
    trust.require(len(applies) == len(sql_tasks) == len(finals) == 1
                  and all(task.get('status') == 'succeeded' or task == finals[0] for task in tasks),
                  'inspection requires successful preceding tasks and pending/running-plan or failed/paused-plan final validation')
    final_result_sha = finals[0].get('task_result_sha256') if final_status == 'failed' else None
    trust.require(final_status != 'failed' or isinstance(final_result_sha, str) and re.fullmatch(r'[a-f0-9]{64}', final_result_sha),
                  'failed final validation result seal is missing')
    trust.require(isinstance(sql_tasks[0].get('evidence_sha256'), str)
                  and re.fullmatch(r'[a-f0-9]{64}', sql_tasks[0]['evidence_sha256']), 'datapatch evidence seal is missing')
    trust.verify_native_apply_authority(args, plan, applies[0], auth_bytes, auth)
    binding_path = trust.NATIVE_ROOT / args.plan_id / 'target-binding.json'
    trust.root_parents(binding_path)
    binding = trust.sealed(json.loads(trust.read_file(binding_path)))
    trust.require(binding.get('plan_id') == args.plan_id and binding.get('plan_sha256') == plan['plan_sha256'],
                  'native target binding differs from plan')
    target = binding['target']
    for key in ('database_unique_name', 'oracle_home', 'owner'):
        trust.require(target.get(key) == plan['target'].get(key), 'native target differs from sealed plan')
    for key in ('database_unique_name', 'owner', 'oracle_sid'):
        trust.require(isinstance(target.get(key), str) and trust.IDENTIFIER.fullmatch(target[key]), 'invalid native target identifier')
    trust.require(isinstance(target.get('oracle_home'), str) and re.fullmatch(r'/[A-Za-z0-9_./-]+', target['oracle_home'])
                  and '..' not in PurePosixPath(target['oracle_home']).parts, 'invalid native Oracle home')
    return plan, target, {'authorization_sha256': trust.digest(auth_bytes),
                          'authorization_record_sha256': auth['record_sha256'],
                          'native_apply_evidence_sha256': applies[0]['evidence_sha256'],
                          'native_datapatch_evidence_sha256': sql_tasks[0]['evidence_sha256'],
                          'native_target_binding_record_sha256': binding['record_sha256'],
                          'plan_state': plan['state'], 'final_task_id': finals[0]['task_id'],
                          'final_task_status': final_status, 'final_task_result_sha256': final_result_sha}


def inspect(args, runner):
    plan, target, authority = target_context(args, runner)
    current = inspect_current(target)
    path, member_name = archive_path(args.archive, target)
    report = {'schema_version': '1.0', 'collector': {'name': 'oracle.extjob.provenance', 'version': '1'},
              'status': 'blocked', 'read_only': True, 'plan_id': args.plan_id,
              'plan_sha256': plan['plan_sha256'], 'actor': args.actor, 'target': target,
              'requested_archive': {'path': args.archive, 'sha256': args.archive_sha256},
              'authority': authority, 'current': current,
              'archive': {'path': str(path), 'expected_sha256': args.archive_sha256, 'verified': False},
              'bytes_match': None, 'reference_root_setuid': None,
              'reference_kind': 'retained_backup_audit_digest', 'mutation_authorized': False,
              'observed_at': trust.now()}
    try:
        archive, member = inspect_archive(path, args.archive_sha256, member_name)
    except (trust.Blocked, OSError, ValueError, tarfile.TarError) as exc:
        report['error'] = str(exc)
        try:
            report['archive']['metadata'] = metadata(path.lstat())
            report['archive']['metadata_unverified'] = True
        except OSError:
            pass
        trust.require(target_context(args, runner) == (plan, target, authority), 'plan or task evidence changed during inspection')
        return trust.seal(report)
    report.update(status='inspected', archive={**archive, 'verified': True}, member=member,
                  bytes_match=member['sha256'] == current['sha256'] and member['size'] == current['size'],
                  reference_root_setuid=member['uid'] == 0 and member['mode'] == '4750')
    trust.require(target_context(args, runner) == (plan, target, authority), 'plan or task evidence changed during inspection')
    return trust.seal(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('plan-id', 'actor', 'archive', 'archive-sha256'):
        parser.add_argument('--' + argument, required=True)
    args = parser.parse_args(argv)
    try:
        trust.require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'native provenance inspection requires root on Linux')
        trust.require(trust.IDENTIFIER.fullmatch(args.plan_id) and trust.IDENTIFIER.fullmatch(args.actor), 'invalid plan or actor')
        report = inspect(args, trust.Runner())
        print(json.dumps(report, sort_keys=True))
        return 0 if report['status'] == 'inspected' else 65
    except (trust.Blocked, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(json.dumps(trust.seal({'schema_version': '1.0', 'status': 'blocked', 'read_only': True,
                                   'plan_id': args.plan_id, 'actor': args.actor, 'error': str(exc),
                                   'requested_archive': {'path': args.archive, 'sha256': args.archive_sha256},
                                   'mutation_authorized': False, 'observed_at': trust.now()}), sort_keys=True))
        return 65


if __name__ == '__main__':
    raise SystemExit(main())
