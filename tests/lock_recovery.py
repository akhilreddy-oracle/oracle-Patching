#!/usr/bin/env python3
"""No Oracle/SSH operations: process fixtures and isolated command mocks."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('lock_recovery', ROOT / 'lib/opu/lock_recovery.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

TARGET = {'uid': 54321, 'owner': 'oracle', 'canonical_home': '/u02/oracle/dbhome',
          'oracle_home': '/u01/oracle/dbhome', 'oracle_sid': 'ORCL',
          'database_unique_name': 'ORCL', 'listener': 'LISTENER'}
HOLDER = {'pid': 99777, 'starttime': 123456, 'uid': 54321,
          'exe': TARGET['canonical_home'] + '/bin/oracle', 'argv': ['ora_pmon_ORCL'],
          'fds': [7], 'classification': 'database_service'}
SESSION = {'pid': HOLDER['pid'], 'process_starttime': HOLDER['starttime'], 'candidate_holder': True,
           'sid': 71, 'serial': 892, 'process_address': '001122AA', 'process_matches': 1,
           'process_session_count': 1, 'session_type': 'USER', 'session_status': 'INACTIVE',
           'server': 'DEDICATED', 'program': 'client', 'module': 'metrics', 'action': None,
           'client_info': None, 'username': 'APPUSER', 'machine': 'clienthost',
           'last_call_seconds': 1200, 'transaction_exists': False, 'transaction_address_present': False}


def session_evidence(rows=(), candidates=()):
    rows = [dict(row) for row in rows]
    return {'status': 'observed', 'all_user_sessions_complete': True, 'running_rman_rows': 0,
            'collector_sid_excluded': 999, 'candidate_pids': list(candidates), 'unmapped_pids': [],
            'target_user_sessions': rows, 'sessions': [row for row in rows if row['pid'] in candidates]}


class ProcessTests(unittest.TestCase):
    def test_only_exact_oracle_background_is_accepted(self):
        self.assertEqual(M.classify_holder(HOLDER, TARGET), 'database_service')
        for replacement in ({'uid': 0}, {'argv': ['oracleORCL', '(LOCAL=NO)']},
                            {'argv': ['ora_pmon_OTHER']}, {'argv': ['ora_pmon_ORCL', 'extra']},
                            {'exe': '/other/bin/oracle'}, {'exe': HOLDER['exe'] + ' (deleted)'}):
            with self.subTest(replacement=replacement):
                self.assertEqual(M.classify_holder({**HOLDER, **replacement}, TARGET), 'unknown')

    def test_listener_requires_exact_home_name_and_allowed_args(self):
        listener = {**HOLDER, 'exe': TARGET['canonical_home'] + '/bin/tnslsnr',
                    'argv': [TARGET['oracle_home'] + '/bin/tnslsnr', 'LISTENER', '-inherit']}
        self.assertEqual(M.classify_holder(listener, TARGET), 'listener_service')
        for argv in ([listener['argv'][0], 'OTHER'], [listener['argv'][0], 'LISTENER', '-extra'],
                     ['/other/bin/tnslsnr', 'LISTENER']):
            self.assertEqual(M.classify_holder({**listener, 'argv': argv}, TARGET), 'unknown')

    def test_fixture_proc_reads_every_matching_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / 'lock'
            lock.write_text('persistent')
            proc = root / 'proc'
            process = proc / str(HOLDER['pid'])
            (process / 'fd').mkdir(parents=True)
            (process / 'fd/7').symlink_to(lock)
            (process / 'fd/11').symlink_to(lock)
            (process / 'stat').write_text('99777 (oracle (background)) S ' + '0 ' * 18 + '123456 0\n')
            (process / 'status').write_text('Uid:\t54321\t54321\t54321\t54321\n')
            (process / 'cmdline').write_bytes(b'ora_pmon_ORCL\0')
            (process / 'exe').symlink_to(HOLDER['exe'])
            found = M.holders(M.identity(lock.stat()), TARGET, proc)
            self.assertEqual(found, [{**HOLDER, 'fds': [7, 11]}])
            (process / 'cmdline').write_bytes(b'unknown-job\0')
            self.assertEqual(M.holders(M.identity(lock.stat()), TARGET, proc)[0]['classification'], 'unknown')

    def test_active_rman_blocks_even_without_host_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / '99777'
            process.mkdir()
            (process / 'cmdline').write_bytes(b'/u01/oracle/bin/rman\0target\0/\0')
            with self.assertRaisesRegex(M.Blocked, 'RMAN'):
                M.require_no_executor(root)

    def test_changed_pid_identity_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / 'lock'
            lock.touch()
            proc = root / 'proc'
            process = proc / '99777'
            (process / 'fd').mkdir(parents=True)
            (process / 'fd/7').symlink_to(lock)
            a = {key: value for key, value in HOLDER.items() if key not in ('fds', 'classification')}
            with patch.object(M, 'proc_identity', side_effect=[a, {**a, 'starttime': 123457}]):
                with self.assertRaisesRegex(M.Blocked, 'changed'):
                    M.holders(M.identity(lock.stat()), TARGET, proc)

    def test_foregrounds_are_diagnostic_candidates_but_remain_unknown(self):
        candidate = {**HOLDER, 'argv': ['oracleORCL', '(LOCAL=NO)']}
        self.assertTrue(M.foreground_candidate(candidate, TARGET))
        self.assertEqual(M.classify_holder(candidate, TARGET), 'unknown')
        for changed in ({'uid': 0}, {'exe': '/other/bin/oracle'},
                        {'argv': ['oracleOTHER', '(LOCAL=NO)']}, {'argv': ['oracleORCL', '(LOCAL=YES)']}):
            self.assertFalse(M.foreground_candidate({**candidate, **changed}, TARGET))


class SessionDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {**HOLDER, 'argv': ['oracleORCL', '(LOCAL=NO)'], 'classification': 'unknown'}
        self.row = {'pid': str(HOLDER['pid']), 'sid': 71, 'serial': 892,
                    'process_address': '001122AA', 'process_matches': 1, 'process_session_count': 1,
                    'session_type': 'USER', 'session_status': 'INACTIVE', 'server': 'DEDICATED',
                    'program': 'client\nprogram', 'module': 'client module', 'username': 'APPUSER',
                    'action': None, 'client_info': None, 'machine': 'client host', 'last_call_seconds': 3600,
                    'transaction_exists': 0, 'transaction_address_present': 0}

    def output(self, *rows, rman=0):
        return '\n'.join('OPU_SESSION|' + json.dumps(row) for row in rows) + f'\nOPU_COLLECTOR|999\nOPU_RMAN|{rman}\n'

    def test_session_pid_mapping_is_sanitized_and_read_only(self):
        runner = M.Runner()
        with patch.object(runner, 'sql', return_value=self.output(self.row)) as sql:
            result = runner.sessions(TARGET, [self.candidate])
        self.assertEqual(result['sessions'][0]['pid'], HOLDER['pid'])
        self.assertEqual(result['sessions'][0]['process_starttime'], HOLDER['starttime'])
        self.assertEqual(result['sessions'][0]['program'], 'clientprogram')
        self.assertFalse(result['sessions'][0]['transaction_exists'])
        self.assertTrue(result['read_only'])
        self.assertEqual(result['unmapped_pids'], [])
        statement = sql.call_args.args[1]
        self.assertIn('s.paddr = p.addr', statement)
        self.assertIn('t.ses_addr = s.saddr', statement)
        self.assertIn("sys_context('USERENV', 'SID')", statement)
        self.assertIn("v$rman_status where status like 'RUNNING%'", statement)
        self.assertNotIn('sql_text', statement.lower())
        self.assertNotIn('APPUSER', statement)

    def test_active_transaction_and_unmapped_process_remain_visible(self):
        runner = M.Runner()
        self.row['transaction_exists'] = 1
        self.row['session_status'] = 'ACTIVE'
        with patch.object(runner, 'sql', return_value=self.output(self.row)):
            result = runner.sessions(TARGET, [self.candidate, {**self.candidate, 'pid': 99888}])
        self.assertTrue(result['sessions'][0]['transaction_exists'])
        self.assertEqual(result['unmapped_pids'], [99888])

    def test_invalid_pid_and_unrequested_native_mapping_are_rejected(self):
        runner = M.Runner()
        with patch.object(runner, 'sql') as sql:
            with self.assertRaises(M.Blocked):
                runner.sessions(TARGET, [{**self.candidate, 'pid': "1') OR 1=1--"}])
            sql.assert_not_called()
        self.row['pid'] = '99888'
        self.row['session_type'] = 'BACKGROUND'
        with patch.object(runner, 'sql', return_value=self.output(self.row)):
            with self.assertRaisesRegex(M.Blocked, 'unrelated background'):
                runner.sessions(TARGET, [self.candidate])

    def test_other_target_user_work_and_running_rman_are_reported(self):
        runner = M.Runner()
        other = {**self.row, 'pid': '99888', 'sid': 72, 'session_status': 'ACTIVE',
                 'transaction_exists': 1, 'transaction_address_present': 1}
        with patch.object(runner, 'sql', return_value=self.output(self.row, other, rman=2)):
            result = runner.sessions(TARGET, [self.candidate])
        self.assertEqual(len(result['sessions']), 1)
        self.assertEqual(len(result['target_user_sessions']), 2)
        self.assertTrue(result['all_user_sessions_complete'])
        self.assertEqual(result['running_rman_rows'], 2)
        self.assertEqual(result['collector_sid_excluded'], 999)
        self.assertTrue(result['target_user_sessions'][1]['transaction_address_present'])

    def test_missing_rman_proof_and_collector_rows_are_rejected(self):
        runner = M.Runner()
        with patch.object(runner, 'sql', return_value=self.output(self.row).replace('OPU_RMAN|0\n', '')):
            with self.assertRaisesRegex(M.Blocked, 'RMAN'):
                runner.sessions(TARGET, [self.candidate])
        self.row['sid'] = 999
        with patch.object(runner, 'sql', return_value=self.output(self.row)):
            with self.assertRaisesRegex(M.Blocked, 'collector session was not excluded'):
                runner.sessions(TARGET, [self.candidate])

    def test_incomplete_inactive_mapping_does_not_grant_inspection_eligibility(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / 'host.lock'
            lock.touch()
            runner = MagicMock()
            runner.sessions.return_value = {'status': 'observed', 'sessions': [self.row], 'diagnostic_only': True}
            args = argparse.Namespace(plan_id='p', task_id='003-validate-node', run_id='a' * 32, actor='lead')
            with patch.object(M, 'target_context', return_value=({'plan_sha256': 'b' * 64}, {'status': 'pending', 'task_definition_sha256': 'c' * 64}, TARGET)), \
                    patch.object(M, 'validate_wrapper', return_value={'exit_code': 75}), \
                    patch.object(M, 'require_no_executor'), \
                    patch.object(M, 'open_lock', return_value=(os.open(lock, os.O_RDWR), M.identity(lock.stat()))), \
                    patch.object(M, 'lock_held', return_value=True), \
                    patch.object(M, 'holders', return_value=[self.candidate]):
                result = M.inspect(args, runner)
            self.assertEqual(result['status'], 'blocked')
            self.assertFalse(result['recovery_eligible'])
            self.assertIn('complete target USER session evidence', result['blockers'][0])
            self.assertEqual(result['foreground_session_diagnostics']['status'], 'observed')
            self.assertEqual(result['holders'][0]['classification'], 'unknown')

    def test_no_foreground_candidates_still_queries_all_users_and_rman(self):
        runner = M.Runner()
        with patch.object(runner, 'sql', return_value=self.output(self.row)) as sql:
            result = runner.sessions(TARGET, [])
        self.assertEqual(result['sessions'], [])
        self.assertEqual(len(result['target_user_sessions']), 1)
        self.assertIn('p.spid in (NULL)', sql.call_args.args[1])
        self.assertEqual(result['running_rman_rows'], 0)


class SessionEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {**HOLDER, 'argv': ['oracleORCL', '(LOCAL=NO)'], 'classification': 'unknown'}
        self.proof = session_evidence([SESSION], [HOLDER['pid']])

    def test_exact_idle_foreground_gets_only_stable_session_identity(self):
        enriched = M.eligible_holders([self.candidate], TARGET, self.proof)
        self.assertEqual(enriched[0]['classification'], 'database_session')
        self.assertEqual(enriched[0]['session_identity'], {key: SESSION[key] for key in
                                                          ('pid', 'process_starttime', 'process_address', 'sid', 'serial')})
        self.assertNotIn('last_call_seconds', enriched[0]['session_identity'])
        self.assertEqual(M.classify_holder(self.candidate, TARGET), 'unknown')

    def test_active_other_user_and_transactions_block_every_restart(self):
        for change in ({'session_status': 'ACTIVE'}, {'transaction_exists': True},
                       {'transaction_address_present': True}, {'server': 'SHARED'}):
            other = {**SESSION, 'pid': 99888, 'sid': 72, 'candidate_holder': False, **change}
            proof = session_evidence([SESSION, other], [HOLDER['pid']])
            with self.subTest(change=change), self.assertRaises(M.Blocked):
                M.eligible_holders([self.candidate], TARGET, proof)
        # The same checks apply when no session currently holds the host lock.
        proof = session_evidence([{**SESSION, 'session_status': 'ACTIVE'}], [])
        with self.assertRaises(M.Blocked):
            M.eligible_holders([HOLDER], TARGET, proof)

    def test_rman_and_maintenance_labels_block(self):
        self.proof['running_rman_rows'] = 1
        with self.assertRaisesRegex(M.Blocked, 'RMAN'):
            M.eligible_holders([self.candidate], TARGET, self.proof)
        for label in ('RMAN backup', '/home/oracle/OPatch/opatch', 'datapatch'):
            proof = session_evidence([{**SESSION, 'module': label}], [HOLDER['pid']])
            with self.subTest(label=label), self.assertRaisesRegex(M.Blocked, 'maintenance client'):
                M.eligible_holders([self.candidate], TARGET, proof)

    def test_missing_ambiguous_and_reused_pid_mappings_block(self):
        for change in ({'process_matches': 2}, {'process_session_count': 2},
                       {'process_starttime': 99999}, {'process_address': None}, {'sid': None}):
            proof = session_evidence([{**SESSION, **change}], [HOLDER['pid']])
            with self.subTest(change=change), self.assertRaises(M.Blocked):
                M.eligible_holders([self.candidate], TARGET, proof)
        self.proof['sessions'] *= 2
        with self.assertRaisesRegex(M.Blocked, 'ambiguous'):
            M.eligible_holders([self.candidate], TARGET, self.proof)
        self.proof['sessions'] = []
        self.proof['unmapped_pids'] = [HOLDER['pid']]
        with self.assertRaisesRegex(M.Blocked, 'no verified Oracle session'):
            M.eligible_holders([self.candidate], TARGET, self.proof)


class RecordTests(unittest.TestCase):
    def test_seal_is_idempotent_and_detects_tampering(self):
        sealed = M.seal({'value': 'verified'})
        self.assertEqual(M.seal(sealed), sealed)
        M.sealed(sealed)
        with self.assertRaises(M.Blocked):
            M.sealed({**sealed, 'value': 'changed'})

    def test_lock_rejects_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'lock'
            path.touch()
            link = Path(temp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(M.Blocked):
                M.safe_file(link, root_owned=False)
            link.unlink()
            os.link(path, link)
            with self.assertRaises(M.Blocked):
                M.safe_file(path, root_owned=False)

    def test_wrapper_accepts_only_exact_preclaim_failure(self):
        values = {'rc': b'75\n', 'stdout': b'', 'stderr': (M.LOCK_ERROR + '\n').encode(), 'pid': b'997779999\n'}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            with patch.object(M, 'read_file', side_effect=lambda p: values[p.name]):
                self.assertEqual(M.validate_wrapper(path)['exit_code'], 75)
                for key, changed in (('rc', b'1\n'), ('stdout', b'claimed task'), ('stderr', b'unknown error')):
                    original = values[key]
                    values[key] = changed
                    with self.assertRaises(M.Blocked):
                        M.validate_wrapper(path)
                    values[key] = original

    def test_all_oracle_children_close_fds_and_use_fixed_argv(self):
        runner = M.Runner()
        completed = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(M.subprocess, 'run', return_value=completed) as run:
            runner.sql(TARGET, 'startup;')
        args, kwargs = run.call_args
        self.assertTrue(kwargs['close_fds'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(args[0][0:4], ['/usr/sbin/runuser', '-u', 'oracle', '--'])
        self.assertIn('whenever sqlerror exit failure', kwargs['input'])
        self.assertNotIn('OPU_SINGLE_INSTANCE_TEST_MODE', kwargs['env'])

    def test_cli_cannot_enable_test_mode_or_override_lock(self):
        with patch.object(M.os, 'geteuid', return_value=1000):
            with patch('builtins.print') as output:
                rc = M.main(['inspect', '--plan-id', 'p', '--task-id', '003-validate-node',
                             '--run-id', 'a' * 32, '--actor', 'lead'])
        self.assertEqual(rc, 65)
        self.assertIn('require root', output.call_args.args[0])

    def test_controller_owned_authority_is_bounded_and_not_group_world_writable(self):
        with tempfile.TemporaryDirectory() as temp:
            plan_dir = Path(temp)
            authority = plan_dir / 'authorization.json'
            authority.write_bytes(b'controller-owned sealed bytes')
            authority.chmod(0o600)
            self.assertEqual(M.read_controller_authority(authority, plan_dir), b'controller-owned sealed bytes')
            authority.chmod(0o620)
            with self.assertRaisesRegex(M.Blocked, 'group/world'):
                M.read_controller_authority(authority, plan_dir)
            authority.chmod(0o600)
            with self.assertRaisesRegex(M.Blocked, 'unsupported'):
                M.read_controller_authority(plan_dir / 'evidence.json', plan_dir)
            authority.unlink()
            authority.symlink_to(plan_dir / 'outside')
            with self.assertRaises(M.Blocked):
                M.read_controller_authority(authority, plan_dir)

    def test_authority_replaced_with_fifo_after_validation_never_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            authority = Path(temp) / 'authorization.json'
            authority.write_text('{}')
            # Bound the actual file-open regression in a child: a blocking
            # implementation must fail the test instead of hanging the suite.
            script = '''
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location('lock_recovery', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
def replace_with_fifo(path):
    original = path.lstat()
    path.unlink(); os.mkfifo(path)
    return original
try:
    m._read_checked_file(pathlib.Path(sys.argv[2]), replace_with_fifo)
except m.Blocked as exc:
    assert 'changed while opening' in str(exc)
    sys.exit(0)
sys.exit(1)
'''
            result = subprocess.run([sys.executable, '-c', script, str(ROOT / 'lib/opu/lock_recovery.py'),
                                     str(authority)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_oratab_ownership_and_primary_group_are_supported(self):
        account = argparse.Namespace(pw_uid=54321, pw_gid=54321)
        info = argparse.Namespace(st_uid=54321, st_gid=54321, st_mode=stat.S_IFREG | 0o664,
                                  st_nlink=1, st_dev=1, st_ino=22)
        def read(path, validator):
            self.assertEqual(path, Path('/etc/oratab'))
            validator(path)
            return b'ORCL:/u01/oracle/dbhome:N\n'
        with patch.object(M, 'root_parents') as parents, patch.object(M.pwd, 'getpwnam', return_value=account), \
                patch.object(M.Path, 'lstat', return_value=info), patch.object(M, '_read_checked_file', side_effect=read):
            content, metadata = M.read_oratab(TARGET)
            self.assertIn(b'ORCL:', content)
            self.assertEqual(metadata['uid'], 54321)
            self.assertEqual(metadata['mode'], '0o664')
            parents.assert_called_once_with(Path('/etc/oratab'))
            info.st_uid, info.st_gid, info.st_mode = 0, 0, stat.S_IFREG | 0o644
            self.assertEqual(M.read_oratab(TARGET)[1]['uid'], 0)

    def test_oratab_foreign_owner_world_write_and_foreign_writable_group_block_with_metadata(self):
        account = argparse.Namespace(pw_uid=54321, pw_gid=54321)
        base = {'st_uid': 54321, 'st_gid': 54321, 'st_mode': stat.S_IFREG | 0o664,
                'st_nlink': 1, 'st_dev': 1, 'st_ino': 22}
        for changed in ({'st_uid': 999}, {'st_mode': stat.S_IFREG | 0o666}, {'st_gid': 999},
                        {'st_mode': stat.S_IFLNK | 0o777}, {'st_nlink': 2}):
            with self.subTest(changed=changed):
                info = argparse.Namespace(**{**base, **changed})
                with patch.object(M, 'root_parents'), patch.object(M.pwd, 'getpwnam', return_value=account), \
                        patch.object(M.Path, 'lstat', return_value=info), \
                        patch.object(M, '_read_checked_file', side_effect=lambda p, validator: validator(p)):
                    with self.assertRaises(M.Blocked) as error:
                        M.read_oratab(TARGET)
                    self.assertEqual(error.exception.diagnostics['oratab']['uid'], info.st_uid)
                    self.assertEqual(error.exception.diagnostics['oratab']['path'], '/etc/oratab')


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan_root = self.root / 'var/webapp-plans'
        task_dir = self.plan_root / 'plans/p/tasks'
        task_dir.mkdir(parents=True)
        for name in ('002-apply-node', '003-validate-node'):
            (task_dir / (name + '.json')).touch()
        self.home = self.root / 'home'
        self.home.mkdir()
        clock = dt.datetime.now(dt.timezone.utc)
        self.plan = {'state': 'running', 'intent': 'patch_apply', 'requester': 'requester',
                     'plan_sha256': 'b' * 64, 'procedure': {'adapter': 'database_single_instance_opatch'},
                     'nodes': ['node'], 'maintenance_window': {'start': (clock - dt.timedelta(minutes=10)).isoformat(),
                                                               'end': (clock + dt.timedelta(hours=1)).isoformat()},
                     'target': {'database_unique_name': 'ORCL', 'oracle_home': str(self.home), 'owner': 'oracle'}}
        approval = M.seal({'decision': 'approved', 'requester': 'requester', 'actor': 'manager',
                          'plan_id': 'p', 'plan_sha256': 'b' * 64})
        approval_bytes = M.canonical(approval)
        auth = M.seal({'decision': 'execution_authorized', 'actor': 'lead', 'plan_id': 'p',
                      'plan_sha256': 'b' * 64, 'approval': {'sha256': M.digest(approval_bytes), 'record_sha256': approval['record_sha256']}})
        auth_bytes = M.canonical(auth)
        self.task = {'task_id': '003-validate-node', 'task_definition_sha256': 'c' * 64,
                     'status': 'pending', 'stage': 'validate', 'node': 'node',
                     'authorization_sha256': M.digest(auth_bytes), 'authorization_record_sha256': auth['record_sha256']}
        self.previous = {'task_id': '002-apply-node', 'status': 'succeeded', 'stage': 'apply'}
        claim = {'schema_version': '1.0', 'task_id': self.previous['task_id'], 'plan_id': 'p',
                 'plan_sha256': 'b' * 64, 'stage': 'apply', 'node': 'node',
                 'adapter': 'database_single_instance_opatch', 'authorization_sha256': M.digest(auth_bytes),
                 'authorization_record_sha256': auth['record_sha256']}
        claim['task_definition_sha256'] = M.digest(M.canonical(claim))
        claim.update(status='running', claimed_by='lead')
        evidence = M.seal({'plan_id': 'p', 'plan_sha256': 'b' * 64, 'task_id': self.previous['task_id'],
                          'stage': 'apply', 'actor': 'lead', 'status': 'succeeded',
                          'postcondition': {'status': 'passed'}, 'exit_code': 0})
        evidence_bytes = M.canonical(evidence)
        self.previous.update(task_definition_sha256=claim['task_definition_sha256'],
                             evidence_sha256=M.digest(evidence_bytes), evidence_record_sha256=evidence['record_sha256'])
        binding = M.seal({'plan_id': 'p', 'plan_sha256': 'b' * 64,
                          'target': {**self.plan['target'], 'oracle_sid': 'ORCL', 'listener': 'LISTENER'}})
        self.files = {'approval.json': approval_bytes, 'authorization.json': auth_bytes,
                      'claim.json': M.canonical(claim), 'evidence.json': evidence_bytes,
                      'target-binding.json': M.canonical(binding), 'oratab': f'ORCL:{self.home}:N\n'.encode()}
        self.runner = MagicMock()
        self.runner.native.side_effect = lambda _r, command, _p, task=None: (
            self.plan if command == 'status' else self.previous if task == '002-apply-node' else self.task)
        self.args = argparse.Namespace(plan_id='p', task_id='003-validate-node', actor='lead')
        self.patchers = [patch.object(M, 'ROOT', self.root),
                         patch.dict(os.environ, {'OPU_PLAN_STATE_DIR': str(self.plan_root)}),
                         patch.object(M, 'read_file', side_effect=lambda p: self.files[p.name]),
                         patch.object(M, 'read_controller_authority', side_effect=lambda p, _d: self.files[p.name]),
                         patch.object(M, 'read_oratab', side_effect=lambda _target: (self.files['oratab'], {'path': '/etc/oratab'})),
                         patch.object(M, 'root_parents'),
                         patch.object(M.pwd, 'getpwnam', return_value=argparse.Namespace(pw_uid=self.home.stat().st_uid))]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_pending_validation_with_bound_authorizer_is_accepted(self):
        if os.geteuid() == 0:
            self.skipTest('fixture Oracle owner must be non-root')
        plan, task, target = M.target_context(self.args, self.runner)
        self.assertEqual(plan['plan_sha256'], 'b' * 64)
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(target['oracle_sid'], 'ORCL')

    def test_versioned_code_reconciles_existing_stable_plan(self):
        if os.geteuid() == 0:
            self.skipTest('fixture Oracle owner must be non-root')
        base = self.root.resolve()
        code = base / '.opu-runtimes' / ('a' * 64)
        code.mkdir(parents=True)
        with patch.object(M, 'ROOT', code), patch.dict(os.environ, {'OPU_PLAN_STATE_DIR': str(base / 'var/webapp-plans')}):
            plan, task, target = M.target_context(self.args, self.runner)
            self.assertEqual(plan['plan_sha256'], self.plan['plan_sha256'])
            self.assertEqual(target['oracle_sid'], 'ORCL')
            self.assertEqual(task['status'], 'pending')
            self.assertTrue(all(call.args[0] == base / 'var/webapp-plans' for call in self.runner.native.call_args_list))
            native = M.Runner()
            with patch.object(native, 'run', return_value='{}') as run:
                native.native(base / 'var/webapp-plans', 'status', 'p')
            self.assertEqual(run.call_args.args[0][3], str(code / 'bin/opu-patch-plan'))
            self.args.run_id = 'f' * 32
            with patch.object(M, 'validate_wrapper', side_effect=M.Blocked('path verified; stop before process inspection')) as wrapper:
                inspected = M.inspect(self.args, self.runner)
            self.assertIn('path verified', inspected['blockers'][0])
            wrapper.assert_called_once_with(base / 'var/webapp-runs/p/003-validate-node' / self.args.run_id)
            self.runner.native.reset_mock()
            with patch.dict(os.environ, {'OPU_PLAN_STATE_DIR': str(code / 'var/webapp-plans')}), self.assertRaises(M.Blocked):
                M.target_context(self.args, self.runner)
            self.runner.native.assert_not_called()

    def test_other_actor_and_modified_authorization_block(self):
        self.args.actor = 'other'
        with self.assertRaisesRegex(M.Blocked, 'authorizer'):
            M.target_context(self.args, self.runner)
        self.args.actor = 'lead'
        changed = json.loads(self.files['authorization.json'])
        changed['actor'] = 'other'
        self.files['authorization.json'] = M.canonical(changed)
        with self.assertRaisesRegex(M.Blocked, 'integrity'):
            M.target_context(self.args, self.runner)

    def test_claimed_task_failed_apply_and_closed_window_block(self):
        self.task['claimed_by'] = 'lead'
        with self.assertRaisesRegex(M.Blocked, 'already claimed'):
            M.target_context(self.args, self.runner)
        del self.task['claimed_by']
        self.previous['status'] = 'failed'
        with self.assertRaisesRegex(M.Blocked, 'apply did not succeed'):
            M.target_context(self.args, self.runner)
        self.plan['maintenance_window']['end'] = '2000-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(M.Blocked, 'window'):
            M.target_context(self.args, self.runner)

    def test_resealed_controller_authorization_cannot_replace_native_authority(self):
        # An attacker who can edit a tar-mirrored document can compute its hash,
        # but cannot replace the root-protected apply claim's exact digest.
        auth = json.loads(self.files['authorization.json'])
        auth['authorized_at'] = '2026-09-14T00:00:00Z'
        auth = M.seal(auth)
        self.files['authorization.json'] = M.canonical(auth)
        self.task['authorization_sha256'] = M.digest(self.files['authorization.json'])
        self.task['authorization_record_sha256'] = auth['record_sha256']
        with self.assertRaisesRegex(M.Blocked, 'root-protected native apply claim'):
            M.target_context(self.args, self.runner)

    def test_native_actor_and_evidence_mismatch_block(self):
        original = self.files['claim.json']
        claim = json.loads(original)
        claim['claimed_by'] = 'other'
        self.files['claim.json'] = M.canonical(claim)
        with self.assertRaisesRegex(M.Blocked, 'native apply claim'):
            M.target_context(self.args, self.runner)
        self.files['claim.json'] = original
        evidence = json.loads(self.files['evidence.json'])
        evidence['actor'] = 'other'
        self.files['evidence.json'] = M.canonical(M.seal(evidence))
        with self.assertRaisesRegex(M.Blocked, 'native apply result'):
            M.target_context(self.args, self.runner)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audit = self.root / 'audit'
        self.audit.mkdir()
        self.host_lock = self.root / 'host.lock'
        self.host_lock.touch()
        self.lock_identity = M.identity(self.host_lock.stat())
        self.args = argparse.Namespace(plan_id='p', task_id='003-validate-node', run_id='a' * 32, actor='lead')
        self.checked = M.seal({'schema_version': '1.0', 'plan_id': 'p', 'task_id': self.args.task_id,
                              'run_id': self.args.run_id, 'actor': 'lead', 'status': 'eligible',
                              'recovery_eligible': True, 'blockers': [], 'plan_sha256': 'b' * 64,
                              'task_definition_sha256': 'c' * 64, 'original_task_status': 'pending',
                              'target': TARGET, 'wrapper': {'exit_code': 75},
                              'lock': {'identity': self.lock_identity}, 'holders': [HOLDER],
                              'health_before': {'dbid': '12345'}})
        self.runner = MagicMock()
        self.runner.health.return_value = {'dbid': '12345', 'listener_ready': True}
        self.runner.sessions.return_value = session_evidence()
        self.clock = dt.datetime.now(dt.timezone.utc)
        self.plan = {'plan_sha256': 'b' * 64, 'maintenance_window': {
            'start': (self.clock - dt.timedelta(minutes=5)).isoformat(),
            'end': (self.clock + dt.timedelta(minutes=5)).isoformat()}}
        self.patchers = [
            patch.object(M, 'AUDIT_ROOT', self.audit),
            patch.object(M, 'HOST_LOCK', self.host_lock),
            patch.object(M, 'protected_directory', side_effect=lambda p: p.mkdir(exist_ok=True)),
            patch.object(M, 'open_lock', side_effect=lambda p: (os.open(p, os.O_RDWR), M.identity(p.stat()))),
            patch.object(M, 'safe_file', side_effect=lambda p: p.stat()),
            patch.object(M, 'inspect', return_value=self.checked),
            patch.object(M, 'target_context', return_value=(self.plan, {'task_definition_sha256': 'c' * 64}, TARGET)),
            patch.object(M, 'validate_wrapper', return_value=self.checked['wrapper']),
            patch.object(M, 'lock_held', side_effect=[True, False]),
            patch.object(M, 'holders', side_effect=[[HOLDER], [HOLDER], [], []]),
            patch.object(M, 'require_no_executor'),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_success_preserves_inode_and_seals_health_without_task_mutation(self):
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['lock']['identity_after'], self.lock_identity)
        self.assertTrue(result['lock_inode_preserved'])
        self.assertTrue(result['plan_and_task_unchanged'])
        self.assertEqual(result['original_task_status'], 'pending')
        self.assertEqual(result['service_health']['dbid'], '12345')
        saved = json.loads((self.audit / 'p' / ('a' * 32) / 'result.json').read_text())
        M.sealed(saved)
        self.assertEqual(saved, result)
        self.assertEqual([call.args[1] for call in self.runner.sql.call_args_list],
                         ['shutdown immediate;', 'startup;', 'alter system register;'])
        with self.assertRaisesRegex(M.Blocked, 'already attempted'):
            M.recover(self.args, self.runner)

    def test_changed_holder_never_stops_services(self):
        M.holders.side_effect = [[{**HOLDER, 'starttime': 999}], [{**HOLDER, 'starttime': 999}]]
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'recovery_required')
        self.runner.oracle.assert_not_called()
        self.runner.sql.assert_not_called()

    def test_remaining_unknown_holder_does_not_restart_without_host_lock(self):
        M.holders.side_effect = [[HOLDER], [HOLDER], [{**HOLDER, 'classification': 'unknown'}], [{**HOLDER, 'classification': 'unknown'}]]
        M.lock_held.side_effect = [True, True]
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual([call.args[1] for call in self.runner.sql.call_args_list], ['shutdown immediate;'])
        self.assertNotIn('lock_released', result)

    def test_shutdown_failure_restores_listener_only_for_unchanged_database_holders(self):
        self.runner.sql.side_effect = M.Blocked('shutdown failed')
        M.holders.side_effect = [[HOLDER], [HOLDER], [HOLDER]]
        M.lock_held.side_effect = [True, True]
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual([call.args[1] for call in self.runner.sql.call_args_list], ['shutdown immediate;'])
        self.assertEqual([call.args[2] for call in self.runner.oracle.call_args_list],
                         [['stop', 'LISTENER'], ['start', 'LISTENER']])
        self.assertEqual(result['guard_health']['dbid'], '12345')

    def test_changed_dbid_is_never_completed(self):
        self.runner.health.return_value = {'dbid': 'DIFFERENT'}
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertIn('DBID changed', result['blockers'][0])

    def test_unknown_inspection_never_creates_attempt(self):
        M.inspect.return_value = {'recovery_eligible': False, 'blockers': ['unknown lock holder']}
        with self.assertRaisesRegex(M.Blocked, 'unknown lock holder'):
            M.recover(self.args, self.runner)
        self.runner.oracle.assert_not_called()
        self.assertFalse((self.audit / 'p').exists())

    def test_idle_foreground_recovery_rechecks_stable_session_identity(self):
        candidate = {**HOLDER, 'argv': ['oracleORCL', '(LOCAL=NO)'], 'classification': 'unknown'}
        proof = session_evidence([SESSION], [HOLDER['pid']])
        self.checked['holders'] = M.eligible_holders([candidate], TARGET, proof)
        fresh = session_evidence([{**SESSION, 'last_call_seconds': 1210}], [HOLDER['pid']])
        self.runner.sessions.return_value = fresh
        M.holders.side_effect = [[candidate], [candidate], [], []]
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'completed')
        self.runner.sessions.assert_called_once_with(TARGET, [candidate])

    def test_session_changed_after_inspection_never_stops_services(self):
        candidate = {**HOLDER, 'argv': ['oracleORCL', '(LOCAL=NO)'], 'classification': 'unknown'}
        proof = session_evidence([SESSION], [HOLDER['pid']])
        self.checked['holders'] = M.eligible_holders([candidate], TARGET, proof)
        self.runner.sessions.return_value = session_evidence([{**SESSION, 'serial': 9999}], [HOLDER['pid']])
        M.holders.side_effect = [[candidate], [candidate]]
        result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertIn('session identities changed', result['blockers'][0])
        self.runner.oracle.assert_not_called()
        self.runner.sql.assert_not_called()

    def test_window_expiring_during_session_check_never_stops_services(self):
        class Clock(dt.datetime):
            value = self.clock

            @classmethod
            def now(cls, tz=None):
                return cls.value if tz is not None else cls.value.replace(tzinfo=None)

        def context(*_args):
            M.require_open_window(self.plan)
            return self.plan, {'task_definition_sha256': 'c' * 64}, TARGET

        def sessions(*_args):
            Clock.value += dt.timedelta(minutes=10)
            return session_evidence()

        M.target_context.side_effect = context
        self.runner.sessions.side_effect = sessions
        with patch.object(M.dt, 'datetime', Clock):
            result = M.recover(self.args, self.runner)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertIn('maintenance window is not open', result['blockers'])
        self.runner.oracle.assert_not_called()
        self.runner.sql.assert_not_called()
        self.assertFalse((self.audit / 'p' / self.args.run_id / 'outage-started.json').exists())


if __name__ == '__main__':
    unittest.main()
