"""Exercise retained native runtimes with real local installs and no SSH."""
import io
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'webapp'))
import tools_sync
import runtime_install


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
                                text=True, env={**os.environ, 'PATH': str(commands),
                                    'OPU_EXECUTION_LOCK_DIR': str(commands.parent / 'host-locks')}, timeout=60)
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
    for name in utilities:
        (commands / name).symlink_to(shutil.which(name))
    return commands


def copy_runtime_source(directory):
    """Use a complete real package even when a test changes one runtime file."""
    for name, mode, content in tools_sync._snapshot_files():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)


class RuntimePackage(unittest.TestCase):
    def setUp(self):
        tools_sync._verified_at.clear()

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
            copy_runtime_source(root)
            for name in tools_sync.SYNC_DIRS:
                (root / name / 'file').write_text('content')
            with patch.object(tools_sync, 'ROOT', root):
                first = tools_sync.local_fingerprint()
                (root / 'lib/file').chmod(0o755)
                second = tools_sync.local_fingerprint()
                self.assertNotEqual(first, second)
                (root / 'operations/file').unlink()
                self.assertNotEqual(second, tools_sync.local_fingerprint())

    def test_cached_host_cannot_hide_source_changes(self):
        key = 'fixture:False:/fixture'
        digest = 'b' * 64
        receipt = {'fingerprint': digest, 'runtime_root': '/fixture/.opu-runtimes/' + digest, 'synced': False}
        with patch.object(tools_sync, 'local_fingerprint', return_value=digest), \
             patch.object(tools_sync.remote, 'pull_file') as pull, \
             patch.object(tools_sync.remote, 'run_remote_shell', return_value=subprocess.CompletedProcess([], 0, json.dumps(receipt), '')) as verify:
            tools_sync._verified_at[key] = (tools_sync.time.monotonic(), 'a' * 64)
            result = tools_sync.ensure_tools('fixture', '/fixture', False)
        self.assertFalse(result['cached'])
        self.assertEqual(result['runtime_root'], receipt['runtime_root'])
        verify.assert_called_once()
        pull.assert_not_called()

    def test_archive_and_generation_share_one_snapshot_when_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            copy_runtime_source(source)
            for name in tools_sync.SYNC_DIRS:
                (source / name / 'file').write_text('original')
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
                with tarfile.open(fileobj=io.BytesIO(uploaded[0]), mode='r:gz') as archive:
                    captured = [(item.name, item.mode, archive.extractfile(item).read()) for item in archive]
                digest = tools_sync.local_fingerprint(captured)
                return subprocess.CompletedProcess([], 0, json.dumps({'fingerprint': digest,
                    'runtime_root': '/fixture/.opu-runtimes/' + digest, 'synced': True}), '')
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
            base = Path(tmp).resolve()
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
            generation = Path(result['runtime_root'])
            self.assertEqual(generation, target / '.opu-runtimes' / result['fingerprint'])
            self.assertEqual(stamp.read_text(), 'old fingerprint\n')
            self.assertEqual(stale.read_text(), 'old runtime')
            self.assertEqual(json.loads(config.read_text())['hosts'][0]['id'], 'keep-this-host')
            self.assertEqual(json.loads(state.read_text()), {'keep': 'existing state'})
            self.assertEqual(list(target.glob('.opu-tools-new.*')), [])
            env = {**os.environ, 'OPU_AGENT_QUEUE_DIR': str(base / 'queue'),
                   'OPU_AGENT_ENROLLMENT_REQUIRED': '0'}
            pull = subprocess.run([str(generation / 'bin/opu-agent-work-pull'), '--node', 'node1',
                                   '--agent-id', 'agent1'], capture_output=True, text=True,
                                  env=env, timeout=30)
            self.assertEqual(pull.returncode, 0, pull.stderr)
            self.assertEqual(json.loads(pull.stdout)['status'], 'idle')
            operations = subprocess.run([str(generation / 'bin/opu-agent'), 'operations'],
                                        capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(operations.returncode, 0, operations.stderr)
            self.assertIn('discover', operations.stdout)
            runtime_install.verify(generation, result['fingerprint'])

    def test_failed_target_interpreter_preserves_installed_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
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

    def test_active_native_host_can_keep_legacy_code_while_new_generation_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            target = base / 'managed-runtime'
            for directory in ('bin', 'lib', 'operations'):
                (target / directory).mkdir(parents=True)
                (target / directory / 'sentinel').write_bytes(('old-' + directory).encode())
            stamp = target / tools_sync.STAMP_NAME
            stamp.write_bytes(b'old-generation\n')
            originals = {path: path.read_bytes() for path in target.rglob('*') if path.is_file()}
            commands = installer_commands(base, supported=True)
            lock_directory = base / 'host-locks'
            lock_directory.mkdir()
            with (lock_directory / 'host-mutation.lock').open('w') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with local_installer(commands) as (uploads, executions):
                    installed = tools_sync.ensure_tools('busy-native-host-fixture', str(target), False, force=True)
                    self.assertTrue(installed['synced'])
                    self.assertEqual(executions[0].returncode, 0, executions[0].stderr)
                    self.assertFalse(uploads[0].exists())
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)
                self.assertEqual(list(target.glob('.opu-tools-new.*')), [])
                runtime_install.verify(Path(installed['runtime_root']), installed['fingerprint'])

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
        uploaded = []
        snapshot = tools_sync._snapshot_files()
        digest = tools_sync.local_fingerprint(snapshot)
        def run(_alias, script, **_kwargs):
            captured.append(script)
            return subprocess.CompletedProcess([], 0, json.dumps({'fingerprint': digest,
                'runtime_root': '/path with spaces/opu/.opu-runtimes/' + digest, 'synced': True}), '')
        with patch.object(tools_sync, '_snapshot_files', return_value=snapshot), \
             patch.object(tools_sync.remote, 'push_file', side_effect=lambda _a, _p, payload, **_kw: uploaded.append(payload)), \
             patch.object(tools_sync.remote, 'run_remote_shell', side_effect=run):
            tools_sync.ensure_tools('fixture', '/path with spaces/opu', True, force=True)
        parsed = subprocess.run(['bash', '-n'], input=captured[0], text=True, capture_output=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        with tarfile.open(fileobj=io.BytesIO(uploaded[0]), mode='r:gz') as archive:
            names = set(archive.getnames())
        self.assertTrue(any(name.startswith('operations/') for name in names))
        self.assertTrue(all('webapp/' + name in names for name in tools_sync.RUNTIME_MODULES))


class ImmutableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='opu-runtime-generation-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.target = self.base / 'deployment'
        self.target.mkdir()
        self.snapshot = tools_sync._snapshot_files()
        self.commands = installer_commands(self.base, supported=True)
        tools_sync._verified_at.clear()

    def archive(self, snapshot, name='payload.tgz'):
        path = self.base / name
        path.write_bytes(tools_sync._build_tarball(snapshot))
        return path

    def install(self, snapshot=None, target=None):
        snapshot = self.snapshot if snapshot is None else snapshot
        digest = tools_sync.local_fingerprint(snapshot)
        return runtime_install.install(target or self.target, digest, self.archive(snapshot))

    def assert_no_staging(self):
        self.assertFalse(list(self.target.rglob('.opu-tools-new.*')))

    def test_missing_generation_ignores_matching_legacy_stamp(self):
        digest = tools_sync.local_fingerprint(self.snapshot)
        stamp = self.target / tools_sync.STAMP_NAME
        stamp.write_text(digest + '\n')
        with patch.object(tools_sync, '_snapshot_files', return_value=self.snapshot), local_installer(self.commands) as (uploads, runs):
            result = tools_sync.ensure_tools('missing-generation', str(self.target), False)
        self.assertEqual([run.returncode for run in runs], [66, 0])
        self.assertEqual(len(uploads), 1)
        self.assertTrue(result['synced'])
        self.assertEqual(stamp.read_text(), digest + '\n')
        runtime_install.verify(Path(result['runtime_root']), digest)

    def test_same_generation_is_verified_without_replacing_inodes(self):
        installed = self.install()
        generation = Path(installed['runtime_root'])
        before = {path.relative_to(generation): (path.stat().st_ino, path.read_bytes())
                  for path in generation.rglob('*') if path.is_file()}
        with patch.object(tools_sync, '_snapshot_files', return_value=self.snapshot), local_installer(self.commands) as (uploads, runs):
            repeated = tools_sync.ensure_tools('same-generation', str(self.target), False, force=True)
            tools_sync._verified_at.clear()
            verified = tools_sync.ensure_tools('same-generation', str(self.target), False)
        self.assertFalse(repeated['synced'])
        self.assertFalse(verified['synced'])
        self.assertEqual(repeated['runtime_root'], installed['runtime_root'])
        self.assertEqual([run.returncode for run in runs], [0, 0])
        self.assertEqual(len(uploads), 1)
        after = {path.relative_to(generation): (path.stat().st_ino, path.read_bytes())
                 for path in generation.rglob('*') if path.is_file()}
        self.assertEqual(after, before)
        self.assert_no_staging()

    def test_paused_collector_loads_old_library_after_new_generation_publication(self):
        probe = b'''#!/usr/bin/env bash
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [ "${1:-}" = pause ]; then
  printf ready >"$2"
  while [ ! -e "$3" ]; do sleep 0.02; done
fi
. "$ROOT/lib/opu/runtime-race-value.sh"
printf '%s\\n' "$runtime_version"
'''
        old = self.snapshot + [('bin/opu-runtime-race-probe', 0o755, probe),
                               ('lib/opu/runtime-race-value.sh', 0o644, b'runtime_version=old\n')]
        new = [(name, mode, b'runtime_version=new\n' if name == 'lib/opu/runtime-race-value.sh' else content)
               for name, mode, content in old]
        first = self.install(old)
        # A legacy process paused before its first library read is preserved too.
        for name, content in [('bin/opu-runtime-race-probe', probe),
                              ('lib/opu/runtime-race-value.sh', b'runtime_version=legacy\n')]:
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755)
        processes = []
        try:
            for label, root in [('old', Path(first['runtime_root'])), ('legacy', self.target)]:
                ready, resume = self.base / (label + '.ready'), self.base / (label + '.resume')
                process = subprocess.Popen([str(root / 'bin/opu-runtime-race-probe'), 'pause', str(ready), str(resume)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append((label, process, ready, resume))
            deadline = time.monotonic() + 10
            while not all(ready.exists() for _, _, ready, _ in processes) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(all(ready.exists() for _, _, ready, _ in processes), 'collectors did not pause before loading libraries')
            with patch.object(tools_sync, '_snapshot_files', return_value=new), local_installer(self.commands):
                second = tools_sync.ensure_tools('concurrent-generation', str(self.target), False, force=True)
            self.assertNotEqual(first['runtime_root'], second['runtime_root'])
            for label, process, _, resume in processes:
                resume.touch()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), label)
            fresh = subprocess.run([str(Path(second['runtime_root']) / 'bin/opu-runtime-race-probe')],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertEqual(fresh.stdout.strip(), 'new')
            runtime_install.verify(Path(first['runtime_root']), first['fingerprint'])
            runtime_install.verify(Path(second['runtime_root']), second['fingerprint'])
        finally:
            for _, process, _, resume in processes:
                resume.touch()
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_published_corruption_is_rejected_not_reinstalled(self):
        for mutation in ('content', 'extra-file', 'symlink-file', 'hardlink-file', 'writable-file', 'symlink-generation'):
            with self.subTest(mutation=mutation):
                target = self.base / mutation
                installed = self.install(target=target)
                generation = Path(installed['runtime_root'])
                member = generation / 'webapp/agent_worker.py'
                original = member.read_bytes()
                if mutation == 'content':
                    member.chmod(0o644)
                    member.write_bytes(original + b'\n# unexpected change\n')
                    member.chmod(0o444)
                elif mutation == 'writable-file':
                    member.chmod(0o644)
                elif mutation == 'hardlink-file':
                    os.link(member, self.base / 'external-hardlink')
                elif mutation == 'symlink-generation':
                    moved = self.base / 'moved-generation'
                    generation.chmod(0o755)
                    generation.rename(moved)
                    moved.chmod(0o555)
                    generation.symlink_to(moved, target_is_directory=True)
                else:
                    member.parent.chmod(0o755)
                    if mutation == 'extra-file':
                        extra = member.parent / 'unexpected_module.py'
                        extra.write_text('pass\n')
                        extra.chmod(0o444)
                    else:
                        outside = self.base / 'external-module.py'
                        outside.write_bytes(original)
                        member.unlink()
                        member.symlink_to(outside)
                    member.parent.chmod(0o555)
                tools_sync._verified_at.clear()
                with patch.object(tools_sync, '_snapshot_files', return_value=self.snapshot), local_installer(self.commands) as (uploads, runs):
                    with self.assertRaises(tools_sync.remote.RemoteError):
                        tools_sync.ensure_tools('corrupt-' + mutation, str(target), False)
                self.assertEqual(uploads, [], 'corruption must not be treated as a missing generation')
                self.assertEqual(len(runs), 1)
                self.assertNotEqual(runs[0].returncode, 66)
                if mutation == 'content':
                    self.assertEqual(member.read_bytes(), original + b'\n# unexpected change\n')
                if mutation == 'symlink-generation':
                    self.assertTrue(generation.is_symlink())

    def test_incomplete_or_unimportable_package_cannot_publish(self):
        old = self.install()
        cases = {
            'incomplete': [('bin/opu-incomplete', 0o755, b'#!/bin/sh\nexit 0\n')],
            'missing-native-helper': [entry for entry in self.snapshot if entry[0] != 'lib/opu/oracle_inventory.sh'],
            'missing-native-operation': [entry for entry in self.snapshot if entry[0] != 'operations/discovery/oracle_homes.sh'],
            'broken-import': [(name, mode, b'raise RuntimeError("broken runtime import")\n' if name == 'webapp/agent_worker.py' else content)
                              for name, mode, content in self.snapshot],
        }
        for label, snapshot in cases.items():
            digest = tools_sync.local_fingerprint(snapshot)
            with self.subTest(label=label):
                with self.assertRaises((runtime_install.InstallError, OSError, ValueError)):
                    runtime_install.install(self.target, digest, self.archive(snapshot, label + '.tgz'))
                self.assertFalse((self.target / '.opu-runtimes' / digest).exists())
                runtime_install.verify(Path(old['runtime_root']), old['fingerprint'])
                self.assert_no_staging()

    def test_symlinked_runtime_parent_or_installer_input_is_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        linked_parent = self.target / '.opu-runtimes'
        linked_parent.symlink_to(outside, target_is_directory=True)
        digest = tools_sync.local_fingerprint(self.snapshot)
        archive = self.archive(self.snapshot)
        with self.assertRaises((runtime_install.InstallError, OSError)):
            runtime_install.install(self.target, digest, archive)
        self.assertEqual(list(outside.iterdir()), [])
        linked_parent.unlink()
        linked_archive = self.base / 'linked-payload.tgz'
        linked_archive.symlink_to(archive)
        with self.assertRaises((runtime_install.InstallError, OSError)):
            runtime_install.install(self.target, digest, linked_archive)
        self.assertFalse((self.target / '.opu-runtimes' / digest).exists())
        self.assert_no_staging()

    def test_incomplete_local_source_is_rejected_before_upload(self):
        source = self.base / 'incomplete-source'
        copy_runtime_source(source)
        shutil.rmtree(source / 'operations')
        with patch.object(tools_sync, 'ROOT', source), patch.object(tools_sync.remote, 'push_file') as push:
            with self.assertRaises(tools_sync.remote.RemoteError):
                tools_sync.ensure_tools('incomplete-source', str(self.target), False, force=True)
        push.assert_not_called()

    def test_unsafe_archive_members_and_bad_fingerprint_preserve_prior_generation(self):
        old = self.install()
        changed = self.snapshot + [('bin/opu-unused-new-member', 0o755, b'#!/bin/sh\nexit 0\n')]
        expected = tools_sync.local_fingerprint(changed)
        for variant in ('traversal', 'symlink', 'duplicate', 'wrong-fingerprint'):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                for name, mode, content in changed:
                    member = tarfile.TarInfo(name)
                    member.mode, member.size = mode, len(content)
                    archive.addfile(member, io.BytesIO(content))
                if variant != 'wrong-fingerprint':
                    member = tarfile.TarInfo('../outside' if variant == 'traversal' else
                                             'lib/opu/foreign-link' if variant == 'symlink' else changed[0][0])
                    if variant == 'symlink':
                        member.type, member.linkname = tarfile.SYMTYPE, '/outside'
                    archive.addfile(member)
            path = self.base / (variant + '.tgz')
            path.write_bytes(output.getvalue())
            digest = '0' * 64 if variant == 'wrong-fingerprint' else expected
            with self.subTest(variant=variant), self.assertRaises((runtime_install.InstallError, OSError, ValueError)):
                runtime_install.install(self.target, digest, path)
            self.assertFalse((self.target / '.opu-runtimes' / digest).exists())
            runtime_install.verify(Path(old['runtime_root']), old['fingerprint'])
            self.assert_no_staging()


class RuntimeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.host = {'ssh_alias': 'primary', 'remote_root': '/deployment'}
        self.digest = 'a' * 64
        self.receipt = {'ssh_alias': 'primary', 'fingerprint': self.digest,
                        'runtime_root': '/deployment/.opu-runtimes/' + self.digest, 'synced': False}

    def test_exact_alias_and_generation_are_retained_after_source_changes(self):
        second = {**self.receipt, 'ssh_alias': 'node2', 'fingerprint': 'b' * 64,
                  'runtime_root': '/deployment/.opu-runtimes/' + 'b' * 64}
        with patch.object(tools_sync, 'local_fingerprint', side_effect=AssertionError('launch must use returned receipt')):
            path = tools_sync.tool_path(self.host, [self.receipt, second], 'bin/opu-topology-discover', 'node2')
        self.assertEqual(path, second['runtime_root'] + '/bin/opu-topology-discover')

    def test_missing_ambiguous_or_retargeted_receipts_are_rejected(self):
        receipts = [None, [], [None], [self.receipt, self.receipt], [{**self.receipt, 'ssh_alias': 'other'}],
                    [{**self.receipt, 'fingerprint': 'a' * 16}], [{**self.receipt, 'fingerprint': 'A' * 64}],
                    [{**self.receipt, 'runtime_root': '/other/.opu-runtimes/' + self.digest}],
                    [{**self.receipt, 'runtime_root': '/deployment/current'}]]
        for items in receipts:
            with self.subTest(items=items), self.assertRaises(tools_sync.remote.RemoteError):
                tools_sync.tool_path(self.host, items, 'bin/opu-topology-discover')

    def test_entrypoint_cannot_escape_runtime(self):
        for entrypoint in (None, 7, '../bin/opu-agent', 'bin/../opu-agent', '/bin/opu-agent', 'bin/opu-agent;id', 'webapp/agent_worker.py'):
            with self.subTest(entrypoint=entrypoint), self.assertRaises(tools_sync.remote.RemoteError):
                tools_sync.tool_path(self.host, [self.receipt], entrypoint)


if __name__ == '__main__':
    unittest.main()
