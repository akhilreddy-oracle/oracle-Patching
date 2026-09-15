#!/usr/bin/env python3
"""Disposable deployment packaging/admission tests; no root, systemd or SSH."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('controller_deployment', ROOT / 'deploy/controller.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='opu-deploy-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'source'
        for name in deploy.SOURCE_DIRS:
            (self.source / name).mkdir(parents=True)
        for name, content in {'webapp/server.py': 'pass\n', 'deploy/controller.py': 'pass\n',
                              'scripts/requirements.txt': '', 'webapp/requirements-sso.txt': ''}.items():
            (self.source / name).write_text(content)
        (self.source / 'deploy/templates').mkdir()
        for name in ('oracle-patching.service', 'controller.env', 'nginx.conf', 'opu-ollama.service'):
            (self.source / 'deploy/templates' / name).write_bytes((ROOT / 'deploy/templates' / name).read_bytes())
        self.bundle = self.base / 'release.tar.gz'

    def package(self):
        return deploy.build(self.source, self.bundle, 'fixture-release')

    def config(self):
        values = {'public_hostname': 'patching.lab.example', 'enable_ollama': False}
        for name in (*deploy.INPUT_FILES, 'tls_certificate', 'tls_private_key'):
            target = self.base / name
            target.write_text('fixture\n')
            target.chmod(0o600)
            values[name] = str(target)
        Path(values['hosts_file']).write_text('{"hosts": []}')
        Path(values['principals_file']).write_text(json.dumps({'principals': [
            {'actor': role, 'roles': [role], 'token_sha256': hashlib.sha256(role.encode()).hexdigest()}
            for role in ('requester', 'approver', 'operator')]}))
        path = self.base / 'deployment.json'
        path.write_text(json.dumps(values))
        path.chmod(0o600)
        return values, path

    def test_bundle_excludes_local_credentials_state_fixtures_and_dependencies(self):
        for relative in ('webapp/var/plans', 'webapp/recovery_fixtures', 'webapp/__pycache__'):
            target = self.source / relative
            target.mkdir(parents=True)
            (target / 'secret').write_text('MUST_NOT_SHIP')
        for name in ('hosts.json', '.env', 'secrets.json', 'debug.log'):
            (self.source / 'webapp' / name).write_text('MUST_NOT_SHIP')
        result = self.package()
        manifest, contents = deploy.read_bundle(self.bundle, result['bundle_sha256'])
        self.assertEqual(manifest['release_id'], 'fixture-release')
        self.assertFalse(any(b'MUST_NOT_SHIP' in content for content in contents.values()))
        self.assertIn('webapp/server.py', contents)
        self.assertIn('deploy/templates/oracle-patching.service', contents)
        with self.assertRaises(FileExistsError):
            self.package()

    def test_source_symlinks_and_checksum_drift_are_rejected(self):
        secret = self.base / 'private'
        secret.write_text('private')
        link = self.source / 'webapp/link.py'
        link.symlink_to(secret)
        with self.assertRaisesRegex(ValueError, 'links'):
            self.package()
        self.assertFalse(self.bundle.exists())
        link.unlink()
        self.package()
        with self.assertRaisesRegex(ValueError, 'checksum'):
            deploy.read_bundle(self.bundle, '0' * 64)

    def test_archive_traversal_links_and_duplicate_members_are_rejected(self):
        for kind in ('traversal', 'link', 'duplicate'):
            with self.subTest(kind=kind):
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode='w:gz') as archive:
                    item = tarfile.TarInfo('../outside' if kind == 'traversal' else 'webapp/server.py')
                    if kind == 'link':
                        item.type, item.linkname = tarfile.SYMTYPE, '/etc/shadow'
                    archive.addfile(item)
                    if kind == 'duplicate':
                        archive.addfile(item)
                path = self.base / (kind + '.tar.gz')
                path.write_bytes(raw.getvalue())
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    deploy.read_bundle(path, deploy.sha(raw.getvalue()))
                self.assertFalse((self.base / 'outside').exists())

    def test_manifest_tampering_is_rejected_even_with_matching_archive_hash(self):
        result = self.package()
        manifest, contents = deploy.read_bundle(self.bundle, result['bundle_sha256'])
        contents['webapp/server.py'] = b'changed source'
        raw = io.BytesIO()
        contents['deployment-manifest.json'] = deploy.canonical(manifest)
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            for name, content in contents.items():
                item = tarfile.TarInfo(name); item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
        path = self.base / 'changed.tar.gz'
        path.write_bytes(raw.getvalue())
        with self.assertRaisesRegex(ValueError, 'verification'):
            deploy.read_bundle(path, deploy.sha(raw.getvalue()))

    def test_configuration_requires_distinct_active_roles_and_safe_tls_values(self):
        values, path = self.config()
        self.assertEqual(deploy.load_config(path, admin=False)['public_hostname'], values['public_hostname'])
        people = Path(values['principals_file'])
        original = people.read_text()
        people.write_text(json.dumps({'principals': [{'actor': 'admin', 'roles': ['requester', 'approver', 'operator'], 'token_sha256': 'a' * 64}]}))
        with self.assertRaisesRegex(ValueError, 'distinct active'):
            deploy.load_config(path, admin=False)
        people.write_text(original)
        entries = json.loads(original)
        entries['principals'][1]['expires_at'] = '2000-01-01T00:00:00Z'
        people.write_text(json.dumps(entries))
        with self.assertRaisesRegex(ValueError, 'distinct active'):
            deploy.load_config(path, admin=False)
        people.write_text(original)
        values['public_hostname'] = 'lab.example; return 200;'
        path.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, 'hostname'):
            deploy.load_config(path, admin=False)

    def test_external_or_cloud_assistant_configuration_is_rejected(self):
        values, path = self.config()
        assistant = self.base / 'assistant.json'
        values['assistant_config_file'] = str(assistant)
        path.write_text(json.dumps(values))
        for config in ({'enabled': True, 'model': 'local-model', 'base_url': 'https://external.example/v1'},
                       {'enabled': True, 'model': 'model:cloud'},
                       {'enabled': True, 'model': 'REPLACE_WITH_MODEL'}):
            assistant.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                deploy.load_config(path, admin=False)
        assistant.write_text('{"enabled":true,"model":"local-model"}')
        self.assertEqual(deploy.load_config(path, admin=False)['assistant_config_file'], str(assistant))

    def test_existing_install_or_state_refuses_all_mutation(self):
        values, _ = self.config()
        result = self.package()
        manifest, contents = deploy.read_bundle(self.bundle, result['bundle_sha256'])
        existing = self.base / 'existing-state'
        existing.mkdir(); sealed = existing / 'plan.json'
        sealed.write_text('sealed absolute paths must not move')
        with patch.object(deploy, 'STATE_PARENT', existing), patch.object(deploy.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(deploy.os, 'geteuid', return_value=0), patch.object(deploy.subprocess, 'run') as run:
            self.assertIn(str(existing), deploy.fresh_conflicts())
            with self.assertRaisesRegex(ValueError, 'existing paths'):
                deploy.install_fresh(manifest, contents, values)
            run.assert_not_called()
        self.assertEqual(sealed.read_text(), 'sealed absolute paths must not move')

    def test_python_39_is_rejected_before_creating_installation_paths(self):
        values, _ = self.config()
        result = self.package()
        manifest, contents = deploy.read_bundle(self.bundle, result['bundle_sha256'])
        install = self.base / 'not-created'
        with patch.object(deploy, 'INSTALL', install), \
             patch.object(deploy.sys, 'version_info', (3, 9, 23)), \
             patch.object(deploy.os, 'geteuid', return_value=0), \
             patch.object(deploy.subprocess, 'run') as run:
            admission = deploy.preflight(manifest, values)
            self.assertTrue(any('Python 3.10 or later' in message for message in admission['blockers']))
            with self.assertRaisesRegex(ValueError, 'Python 3.10 or later'):
                deploy.install_fresh(manifest, contents, values)
            run.assert_not_called()
        self.assertFalse(install.exists())
        with patch.object(deploy.sys, 'version_info', (3, 10, 0)):
            admission = deploy.preflight(manifest, values)
            self.assertFalse(any('Python 3.10 or later' in message for message in admission['blockers']))

    def test_fresh_install_stages_stopped_services_without_ssh_or_old_state(self):
        values, _ = self.config()
        result = self.package()
        manifest, contents = deploy.read_bundle(self.bundle, result['bundle_sha256'])
        for name in ('INSTALL', 'CONFIG', 'STATE_PARENT', 'SSH_HOME', 'UNIT', 'NGINX'):
            self.enterContext(patch.object(deploy, name, self.base / name.lower()))
        self.enterContext(patch.object(deploy, 'STATE', deploy.STATE_PARENT / 'controller'))
        commands = []
        def command(argv, **kwargs):
            commands.append(argv)
            if argv[0] == 'useradd':
                deploy.SSH_HOME.mkdir()
            return SimpleNamespace(returncode=0)
        original = deploy.regular
        self.enterContext(patch.object(deploy, 'regular', side_effect=lambda path, **kwargs: original(path, maximum=kwargs.get('maximum', deploy.MAX_BUNDLE))))
        self.enterContext(patch.object(deploy, 'preflight', return_value={'status': 'ready_for_fresh_install', 'blockers': [], 'services_will_start': False}))
        self.enterContext(patch.object(deploy.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(deploy.os, 'chown'))
        self.enterContext(patch.object(deploy.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())))
        self.enterContext(patch.object(deploy.subprocess, 'run', side_effect=command))
        installed = deploy.install_fresh(manifest, contents, values)
        self.assertEqual(installed['status'], 'installed_stopped')
        self.assertFalse(installed['services_will_start'])
        self.assertEqual((deploy.INSTALL / 'current').resolve(), deploy.INSTALL / 'releases/fixture-release')
        self.assertFalse(deploy.NGINX.exists())
        self.assertEqual([cmd for cmd in commands if cmd[0] == 'systemctl'], [['systemctl', 'daemon-reload']])
        self.assertFalse(any(cmd[0] in {'ssh', 'scp', 'chown', 'chmod'} for cmd in commands))
        self.assertEqual(list(deploy.STATE.iterdir()), [])
        self.assertEqual((deploy.CONFIG / 'principals.json').stat().st_mode & 0o777, 0o640)

    def test_template_network_and_credential_boundaries(self):
        nginx = (ROOT / 'deploy/templates/nginx.conf').read_text()
        http = nginx.split('server {', 2)[1]
        self.assertIn('access_log off;', http)
        self.assertIn('error_log /dev/null crit;', http)
        self.assertNotIn('$request_uri', nginx)
        self.assertIn('proxy_pass http://127.0.0.1:8765;', nginx)
        service = (ROOT / 'deploy/templates/oracle-patching.service').read_text()
        self.assertIn('User=opu-controller', service)
        self.assertIn('RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6', service)
        self.assertNotIn('PrivateNetwork=yes', service)
        ollama = (ROOT / 'deploy/templates/opu-ollama.service').read_text()
        self.assertIn('OLLAMA_HOST=127.0.0.1:11434', ollama)
        self.assertIn('OLLAMA_NO_CLOUD=1', ollama)


if __name__ == '__main__':
    unittest.main()
