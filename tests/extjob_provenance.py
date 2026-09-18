#!/usr/bin/env python3
"""Read-only inspector fixtures; never extract or execute archive members."""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib/opu'))
SPEC = importlib.util.spec_from_file_location('extjob_provenance', ROOT / 'lib/opu/extjob_provenance.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, content, options in entries:
            member = tarfile.TarInfo(name)
            member.uid = options.get('uid', 0)
            member.gid = options.get('gid', 54321)
            member.mode = options.get('mode', 0o4750)
            member.type = options.get('type', tarfile.REGTYPE)
            member.linkname = options.get('linkname', '')
            member.size = len(content) if member.isreg() else 0
            archive.addfile(member, io.BytesIO(content) if member.isreg() else None)
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.member = 'dbhome_1/bin/extjob'
        self.content = b'fixture executable bytes; never executed\n'

    def test_member_digest_and_privilege_metadata_without_extraction(self):
        payload = archive_bytes([(self.member, self.content, {}), ('dbhome_1/other', b'other', {})])
        with patch.object(tarfile.TarFile, 'extract', side_effect=AssertionError('extraction forbidden')), \
                patch.object(tarfile.TarFile, 'extractall', side_effect=AssertionError('extraction forbidden')):
            member, count = M.archive_member(io.BytesIO(payload), self.member)
        self.assertEqual(member['sha256'], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(member['mode'], '4750')
        self.assertEqual(member['uid'], 0)
        self.assertEqual(count, 2)

    def test_duplicate_and_link_member_are_rejected(self):
        variants = [
            [(self.member, self.content, {}), ('./' + self.member, self.content, {})],
            [(self.member, b'', {'type': tarfile.SYMTYPE, 'linkname': '/outside'})],
            [(self.member, b'', {'type': tarfile.LNKTYPE, 'linkname': 'dbhome_1/other'})],
        ]
        for entries in variants:
            with self.subTest(entries=entries), self.assertRaises(M.trust.Blocked):
                M.archive_member(io.BytesIO(archive_bytes(entries)), self.member)

    def test_malformed_missing_and_traversal_archive_are_rejected(self):
        for payload in (b'not gzip', archive_bytes([('dbhome_1/other', b'x', {})]),
                        archive_bytes([(self.member, self.content, {}), ('../outside', b'x', {})])):
            with self.subTest(size=len(payload)), self.assertRaises((M.trust.Blocked, OSError)):
                M.archive_member(io.BytesIO(payload), self.member)

    def test_archive_hash_mismatch_prevents_parsing(self):
        payload = archive_bytes([(self.member, self.content, {})])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'archive.tar.gz'
            path.write_bytes(payload)
            safe_file = M.trust.safe_file
            with patch.object(M.trust, 'safe_file', side_effect=lambda p: safe_file(p, root_owned=False)), \
                    patch.object(M, 'archive_member') as parse:
                with self.assertRaisesRegex(M.trust.Blocked, 'retained audit'):
                    M.inspect_archive(path, '0' * 64, self.member)
                parse.assert_not_called()

    def test_matching_hash_checks_same_fd_and_member_metadata(self):
        payload = archive_bytes([(self.member, self.content, {'uid': 54321, 'mode': 0o750})])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'archive.tar.gz'
            path.write_bytes(payload)
            safe_file = M.trust.safe_file
            with patch.object(M.trust, 'safe_file', side_effect=lambda p: safe_file(p, root_owned=False)):
                archive, member = M.inspect_archive(path, hashlib.sha256(payload).hexdigest(), self.member)
            self.assertTrue(archive['same_open_descriptor_verified'])
            self.assertEqual(member['uid'], 54321)
            self.assertEqual(member['mode'], '0750')

    def test_archive_path_is_bounded_by_sealed_database_and_home(self):
        target = {'database_unique_name': 'ORCL', 'oracle_home': '/u01/oracle/dbhome_1'}
        accepted = '/u02/opu-backup/ORCL/run/oracle-home/dbhome_1.tar.gz'
        self.assertEqual(str(M.archive_path(accepted, target)[0]), accepted)
        for path in ('/etc/shadow', '/u02/opu-backup/OTHER/run/oracle-home/dbhome_1.tar.gz',
                     '/u02/opu-backup/ORCL/run/oracle-home/other.tar.gz',
                     '/u02/opu-backup/ORCL/../OTHER/run/oracle-home/dbhome_1.tar.gz'):
            with self.subTest(path=path), self.assertRaises(M.trust.Blocked):
                M.archive_path(path, target)


class CurrentTests(unittest.TestCase):
    def test_regular_current_file_digest_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'dbhome_1'
            (home / 'bin').mkdir(parents=True)
            executable = home / 'bin/extjob'
            executable.write_bytes(b'current fixture')
            target = {'oracle_home': str(home)}
            report = M.inspect_current(target)
            self.assertEqual(report['sha256'], hashlib.sha256(b'current fixture').hexdigest())
            self.assertTrue(report['same_open_descriptor_verified'])
            executable.unlink()
            executable.symlink_to('/etc/passwd')
            with self.assertRaises(OSError):
                M.inspect_current(target)

    def test_current_hardlink_and_bin_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'dbhome_1'
            (home / 'bin').mkdir(parents=True)
            executable = home / 'bin/extjob'
            executable.write_bytes(b'current fixture')
            os.link(executable, home / 'duplicate')
            with self.assertRaises(M.trust.Blocked):
                M.inspect_current({'oracle_home': str(home)})
            (home / 'duplicate').unlink()
            (home / 'bin').rename(home / 'actual-bin')
            (home / 'bin').symlink_to(home / 'actual-bin')
            with self.assertRaises(OSError):
                M.inspect_current({'oracle_home': str(home)})

    def test_changed_content_is_distinguished_from_reference_privileges(self):
        args = argparse.Namespace(plan_id='p', actor='lead', archive='/unused', archive_sha256='a' * 64)
        plan = {'plan_sha256': 'b' * 64}
        member = {'sha256': 'c' * 64, 'size': 10, 'uid': 0, 'mode': '4750'}
        current = {'sha256': 'd' * 64, 'size': 10}
        with patch.object(M, 'target_context', return_value=(plan, {}, {})), \
                patch.object(M, 'archive_path', return_value=(Path('/unused'), 'dbhome_1/bin/extjob')), \
                patch.object(M, 'inspect_archive', return_value=({}, member)), \
                patch.object(M, 'inspect_current', return_value=current):
            result = M.inspect(args, MagicMock())
        self.assertFalse(result['bytes_match'])
        self.assertTrue(result['reference_root_setuid'])
        self.assertFalse(result['mutation_authorized'])
        M.trust.sealed(result)

    def test_cli_requires_root_and_has_no_mutation_mode(self):
        with patch.object(M.os, 'geteuid', return_value=1234), patch('builtins.print') as output:
            code = M.main(['--plan-id', 'p', '--actor', 'lead', '--archive', '/unused', '--archive-sha256', 'a' * 64])
        self.assertEqual(code, 65)
        report = json.loads(output.call_args.args[0])
        self.assertFalse(report['mutation_authorized'])
        self.assertTrue(report['read_only'])

    def test_untrusted_archive_stays_blocked_but_reports_current_digest(self):
        args = argparse.Namespace(plan_id='p', actor='lead', archive='/unused', archive_sha256='a' * 64)
        current = {'sha256': 'd' * 64, 'size': 10, 'uid': 54321, 'mode': '0751'}
        with patch.object(M, 'target_context', return_value=({'plan_sha256': 'b' * 64}, {}, {})), \
                patch.object(M, 'archive_path', return_value=(Path('/unused'), 'dbhome_1/bin/extjob')), \
                patch.object(M, 'inspect_current', return_value=current), \
                patch.object(M, 'inspect_archive', side_effect=M.trust.Blocked('archive is not root protected')):
            report = M.inspect(args, MagicMock())
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['current']['sha256'], 'd' * 64)
        self.assertFalse(report['archive']['verified'])
        self.assertIsNone(report['bytes_match'])
        self.assertIsNone(report['reference_root_setuid'])
        self.assertFalse(report['mutation_authorized'])
        M.trust.sealed(report)

    def test_scope_change_during_archive_comparison_invalidates_inspection(self):
        args = argparse.Namespace(plan_id='p', actor='lead', archive='/unused', archive_sha256='a' * 64)
        before = ({'plan_sha256': 'b' * 64, 'state': 'paused'}, {}, {'final_task_result_sha256': 'c' * 64})
        after = ({'plan_sha256': 'b' * 64, 'state': 'running'}, {}, {'final_task_result_sha256': None})
        member = {'sha256': 'd' * 64, 'size': 10, 'uid': 0, 'mode': '4750'}
        with patch.object(M, 'target_context', side_effect=[before, after]), \
                patch.object(M, 'archive_path', return_value=(Path('/unused'), 'dbhome_1/bin/extjob')), \
                patch.object(M, 'inspect_current', return_value={'sha256': 'd' * 64, 'size': 10}), \
                patch.object(M, 'inspect_archive', return_value=({}, member)):
            with self.assertRaisesRegex(M.trust.Blocked, 'changed during inspection'):
                M.inspect(args, MagicMock())


class TargetContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='opu-extjob-context-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(M, 'ROOT', self.root))
        self.plan_root = self.root / 'var/webapp-plans'
        self.enterContext(patch.dict(os.environ, {'OPU_PLAN_STATE_DIR': str(self.plan_root)}))
        self.directory = self.plan_root / 'plans/p'
        (self.directory / 'tasks').mkdir(parents=True)
        self.args = argparse.Namespace(plan_id='p', actor='operator')
        self.target = {'database_unique_name': 'ORCL', 'oracle_home': '/u01/dbhome_1', 'owner': 'oracle', 'oracle_sid': 'ORCL'}
        self.plan = {'plan_id': 'p', 'plan_sha256': 'b' * 64, 'state': 'paused', 'intent': 'patch_apply',
                     'nodes': ['source'], 'target': self.target,
                     'procedure': {'adapter': 'database_single_instance_opatch'},
                     'maintenance_window': {'start': '2020-01-01T00:00:00Z', 'end': '2020-01-01T01:00:00Z'}}
        self.auth = M.trust.seal({'plan_id': 'p', 'plan_sha256': 'b' * 64, 'actor': 'operator', 'decision': 'execution_authorized'})
        self.enterContext(patch.object(M.trust, 'read_controller_authority', return_value=json.dumps(self.auth).encode()))
        self.enterContext(patch.object(M.trust, 'root_parents'))
        self.binding = M.trust.seal({'plan_id': 'p', 'plan_sha256': 'b' * 64, 'target': self.target})
        self.enterContext(patch.object(M.trust, 'read_file', return_value=json.dumps(self.binding).encode()))
        self.native_authority = self.enterContext(patch.object(M.trust, 'verify_native_apply_authority'))
        self.tasks = {}
        for task_id, stage in (('001-precheck-source', 'precheck'), ('002-apply-source', 'apply'),
                               ('003-validate-source', 'validate'), ('004-datapatch-local', 'datapatch'),
                               ('005-final-validate-local', 'final_validate')):
            self.tasks[task_id] = {'plan_id': 'p', 'plan_sha256': 'b' * 64, 'task_id': task_id, 'stage': stage,
                                   'status': 'failed' if stage == 'final_validate' else 'succeeded',
                                   'evidence_sha256': 'd' * 64, 'task_result_sha256': 'e' * 64}
            (self.directory / 'tasks' / (task_id + '.json')).write_text('{}')
        self.runner = MagicMock()
        self.runner.native.side_effect = lambda _root, command, _plan, *args: deepcopy(self.plan if command == 'status' else self.tasks[args[0]])

    def test_paused_failed_context_preserves_all_custody_calls_and_ignores_expired_window(self):
        plan, target, authority = M.target_context(self.args, self.runner)
        self.assertEqual((plan, target), (self.plan, self.target))
        self.assertEqual(authority['plan_state'], 'paused')
        self.assertEqual(authority['final_task_status'], 'failed')
        self.assertEqual(authority['final_task_result_sha256'], 'e' * 64)
        self.assertEqual(authority['native_datapatch_evidence_sha256'], 'd' * 64)
        self.native_authority.assert_called_once()
        self.assertEqual([call.args[1] for call in self.runner.native.call_args_list], ['status'] + ['task-status'] * 5)

    def test_versioned_inspector_retains_legacy_plan_paths(self):
        base = self.root.resolve()
        code = base / '.opu-runtimes' / ('a' * 64)
        code.mkdir(parents=True)
        with patch.object(M, 'ROOT', code), patch.dict(os.environ, {'OPU_PLAN_STATE_DIR': str(base / 'var/webapp-plans')}):
            plan, target, authority = M.target_context(self.args, self.runner)
            self.assertEqual((plan, target), (self.plan, self.target))
            self.assertEqual(authority['plan_state'], 'paused')
            self.assertTrue(all(call.args[0] == base / 'var/webapp-plans' for call in self.runner.native.call_args_list))
            self.runner.native.reset_mock()
            with patch.dict(os.environ, {'OPU_PLAN_STATE_DIR': str(code / 'var/webapp-plans')}), self.assertRaises(M.trust.Blocked):
                M.target_context(self.args, self.runner)
            self.runner.native.assert_not_called()

    def test_running_pending_remains_supported_but_other_pairs_are_blocked(self):
        self.plan['state'] = 'running'
        final = self.tasks['005-final-validate-local']
        final['status'] = 'pending'
        self.assertIsNone(M.target_context(self.args, self.runner)[2]['final_task_result_sha256'])
        for plan_state, task_status in (('paused', 'pending'), ('running', 'failed'), ('paused', 'unknown'),
                                        ('paused', 'running'), ('succeeded', 'succeeded')):
            self.plan['state'], final['status'] = plan_state, task_status
            with self.subTest(plan_state=plan_state, task_status=task_status), self.assertRaises(M.trust.Blocked):
                M.target_context(self.args, self.runner)

    def test_missing_result_seal_sql_custody_or_unresolved_other_task_is_rejected(self):
        for task_id, field, value in (('005-final-validate-local', 'task_result_sha256', None),
                                       ('004-datapatch-local', 'evidence_sha256', None),
                                       ('003-validate-source', 'status', 'unknown')):
            with self.subTest(task_id=task_id, field=field), patch.dict(self.tasks[task_id], {field: value}):
                with self.assertRaises(M.trust.Blocked):
                    M.target_context(self.args, self.runner)
        self.runner.native.side_effect = M.trust.Blocked('task custody is changed')
        with self.assertRaisesRegex(M.trust.Blocked, 'custody is changed'):
            M.target_context(self.args, self.runner)


if __name__ == '__main__':
    unittest.main()
