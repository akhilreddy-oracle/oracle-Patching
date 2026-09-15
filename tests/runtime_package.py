"""Run discovery and queue tools using only an extracted sync payload."""
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'webapp'))
import tools_sync


@contextmanager
def local_installer(commands):
    """Exercise the shipped installer with real archives and a local shell."""
    uploads = []
    executions = []

    def push(_alias, destination, payload, **_kwargs):
        path = Path(destination)
        uploads.append(path)
        path.write_bytes(payload)

    def run(_alias, script, **_kwargs):
        result = subprocess.run([shutil.which('bash'), '-c', script], capture_output=True,
                                text=True, env={**os.environ, 'PATH': str(commands)}, timeout=60)
        executions.append(result)
        return result

    try:
        with patch.object(tools_sync.remote, 'push_file', side_effect=push), \
             patch.object(tools_sync.remote, 'run_remote_shell', side_effect=run):
            yield uploads, executions
    finally:
        for path in uploads:
            path.unlink(missing_ok=True)


def installer_commands(base, supported):
    """Isolate Python discovery from any additional interpreters on the machine."""
    commands = base / 'commands'
    commands.mkdir()
    old_python = commands / 'python3'
    old_python.write_text('#!/bin/sh\nexit 69\n')
    old_python.chmod(0o755)
    # The rejection path needs rm for the installer's early EXIT trap.
    utilities = ['rm']
    if supported:
        (commands / 'python3.9').symlink_to(sys.executable)
        utilities += ['mkdir', 'mktemp', 'tar', 'gzip', 'mv', 'chmod']
        # macOS has no flock CLI. Lock the actual inherited descriptor using
        # fcntl, preserving the shell installer's locking and command boundary.
        flock = commands / 'flock'
        flock.write_text('#!/usr/bin/env python3.9\n'
                         'import fcntl, sys\n'
                         'assert sys.argv[1:3] == ["-w", "120"]\n'
                         'fcntl.flock(int(sys.argv[3]), fcntl.LOCK_EX)\n')
        flock.chmod(0o755)
    for name in utilities:
        (commands / name).symlink_to(shutil.which(name))
    return commands


class RuntimePackage(unittest.TestCase):
    def test_clean_sync_payload_executes_discovery_and_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(tools_sync._build_tarball()), mode='r:gz') as archive:
                archive.extractall(root, filter='data')
            env = {**os.environ, 'OPU_AGENT_QUEUE_DIR': str(root / 'queue'), 'OPU_AGENT_ENROLLMENT_REQUIRED': '0'}
            result = subprocess.run([str(root / 'bin/opu-agent-work-pull'), '--node', 'node1', '--agent-id', 'agent1'],
                                    capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'idle')
            result = subprocess.run([str(root / 'bin/opu-agent'), 'operations'], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('discover', result.stdout)
            self.assertFalse((root / 'webapp/hosts.json').exists())
            self.assertFalse((root / 'webapp/var').exists())
            self.assertTrue((root / 'webapp/runtime_paths.py').is_file())

    def test_fingerprint_changes_on_deletion_or_mode_even_with_same_newest_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in tools_sync.SYNC_DIRS:
                (root / name).mkdir()
                (root / name / 'file').write_text('content')
            (root / 'webapp').mkdir()
            for name in tools_sync.RUNTIME_MODULES:
                (root / 'webapp' / name).write_text('pass')
            with patch.object(tools_sync, 'ROOT', root):
                first = tools_sync.local_fingerprint()
                (root / 'lib/file').chmod(0o755)
                second = tools_sync.local_fingerprint()
                self.assertNotEqual(first, second)
                (root / 'operations/file').unlink()
                self.assertNotEqual(second, tools_sync.local_fingerprint())

    def test_cached_host_cannot_hide_source_changes(self):
        key = 'fixture:/fixture'
        with patch.object(tools_sync, 'local_fingerprint', return_value='new'), \
             patch.object(tools_sync.remote, 'pull_file', return_value=b'new') as pull:
            tools_sync._verified_at[key] = (tools_sync.time.monotonic(), 'old')
            result = tools_sync.ensure_tools('fixture', '/fixture', False)
        self.assertFalse(result['cached'])
        pull.assert_called_once()

    def test_archive_and_stamp_share_one_snapshot_when_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            for name in tools_sync.SYNC_DIRS:
                (source / name).mkdir()
                (source / name / 'file').write_text('original')
            (source / 'webapp').mkdir()
            for name in tools_sync.RUNTIME_MODULES:
                (source / 'webapp' / name).write_text('pass')
            (source / 'lib/opu').mkdir()
            helper = source / 'lib/opu/python.sh'
            captured_helper = (ROOT / 'lib/opu/python.sh').read_text() + '\n# captured helper\n'
            helper.write_text(captured_helper)
            build = tools_sync._build_tarball
            uploaded = []
            scripts = []
            def change_during_build(snapshot):
                (source / 'bin/file').write_text('changed after capture')
                helper.write_text('# helper changed after capture\n')
                return build(snapshot)
            def run(_alias, script, **_kwargs):
                scripts.append(script)
                return subprocess.CompletedProcess([], 0, '', '')
            with patch.object(tools_sync, 'ROOT', source), \
                 patch.object(tools_sync, '_build_tarball', side_effect=change_during_build), \
                 patch.object(tools_sync.remote, 'push_file', side_effect=lambda _a, _p, payload, **_kw: uploaded.append(payload)), \
                 patch.object(tools_sync.remote, 'run_remote_shell', side_effect=run):
                result = tools_sync.ensure_tools('snapshot-fixture', '/fixture', False, force=True)
            with tarfile.open(fileobj=io.BytesIO(uploaded[0]), mode='r:gz') as archive:
                captured = [(item.name, item.mode, archive.extractfile(item).read()) for item in archive]
            self.assertEqual(result['fingerprint'], tools_sync.local_fingerprint(captured))
            self.assertEqual(dict((name, data) for name, _mode, data in captured)['bin/file'], b'original')
            self.assertIn(captured_helper, scripts[0])
            self.assertNotIn('# helper changed after capture', scripts[0])

    def test_installer_selects_versioned_python_and_preserves_host_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / 'managed runtime'
            (target / 'bin').mkdir(parents=True)
            stale = target / 'bin/stale-tool'
            stale.write_text('old runtime')
            (target / 'webapp/var').mkdir(parents=True)
            config = target / 'webapp/hosts.json'
            config.write_text('{"hosts": [{"id": "keep-this-host"}]}')
            state = target / 'webapp/var/local-state.json'
            state.write_text('{"keep": "existing state"}')
            stamp = target / tools_sync.STAMP_NAME
            stamp.write_text('old fingerprint\n')
            commands = installer_commands(base, supported=True)
            with local_installer(commands) as (uploads, executions):
                result = tools_sync.ensure_tools('versioned-python-fixture', str(target), False, force=True)
                self.assertEqual(len(uploads), 1)
                self.assertFalse(uploads[0].exists())
                self.assertEqual(executions[0].returncode, 0, executions[0].stderr)
            self.assertTrue(result['synced'])
            self.assertEqual(stamp.read_text().strip(), result['fingerprint'])
            self.assertFalse(stale.exists())
            self.assertEqual(json.loads(config.read_text())['hosts'][0]['id'], 'keep-this-host')
            self.assertEqual(json.loads(state.read_text()), {'keep': 'existing state'})
            self.assertEqual(list(target.glob('.opu-tools-new.*')), [])
            env = {**os.environ, 'OPU_AGENT_QUEUE_DIR': str(base / 'queue'),
                   'OPU_AGENT_ENROLLMENT_REQUIRED': '0'}
            pull = subprocess.run([str(target / 'bin/opu-agent-work-pull'), '--node', 'node1',
                                   '--agent-id', 'agent1'], capture_output=True, text=True,
                                  env=env, timeout=30)
            self.assertEqual(pull.returncode, 0, pull.stderr)
            self.assertEqual(json.loads(pull.stdout)['status'], 'idle')
            operations = subprocess.run([str(target / 'bin/opu-agent'), 'operations'],
                                        capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(operations.returncode, 0, operations.stderr)
            self.assertIn('discover', operations.stdout)

    def test_failed_target_interpreter_preserves_installed_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / 'managed runtime'
            (target / 'bin').mkdir(parents=True)
            sentinel = target / 'bin/existing-tool'
            sentinel.write_text('original runtime')
            stamp = target / tools_sync.STAMP_NAME
            stamp.write_text('original fingerprint\n')
            commands = installer_commands(base, supported=False)
            with local_installer(commands) as (uploads, executions):
                with self.assertRaises(tools_sync.remote.RemoteError) as failure:
                    tools_sync.ensure_tools('old-python-fixture', str(target), False, force=True)
                self.assertEqual(executions[0].returncode, 69)
                self.assertIn('Python 3.9 or newer', str(failure.exception))
                self.assertIn('exit 69', str(failure.exception))
                self.assertEqual(len(uploads), 1)
                self.assertFalse(uploads[0].exists())
            self.assertEqual(sentinel.read_text(), 'original runtime')
            self.assertEqual(stamp.read_text(), 'original fingerprint\n')
            self.assertEqual(list(target.glob('.opu-tools-new.*')), [])

    def test_silent_installer_failure_reports_exit_code(self):
        with patch.object(tools_sync.remote, 'push_file'), \
             patch.object(tools_sync.remote, 'run_remote_shell',
                          return_value=subprocess.CompletedProcess([], 23, '', '')):
            with self.assertRaises(tools_sync.remote.RemoteError) as failure:
                tools_sync.ensure_tools('silent-failure-fixture', '/fixture', False, force=True)
        self.assertIn('exit 23', str(failure.exception))
        self.assertIn('without diagnostic output', str(failure.exception))

    def test_sync_script_is_valid_bash_and_contains_all_runtime_components(self):
        captured = []
        def run(_alias, script, **_kwargs):
            captured.append(script)
            return subprocess.CompletedProcess([], 0, '', '')
        with patch.object(tools_sync.remote, 'push_file'), \
             patch.object(tools_sync.remote, 'run_remote_shell', side_effect=run):
            tools_sync.ensure_tools('fixture', '/path with spaces/opu', True, force=True)
        parsed = subprocess.run(['bash', '-n'], input=captured[0], text=True, capture_output=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn('operations', captured[0])
        self.assertIn('agent_worker.py', captured[0])
        self.assertIn('flock', captured[0])


if __name__ == '__main__':
    unittest.main()
