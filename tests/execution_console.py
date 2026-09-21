#!/usr/bin/env python3
"""Persistent execution and strictly observational SSH fixtures; no live calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'webapp'))
import diagnostics
import execution_console as console
import pipeline_runner as runner
import planctl


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.enterContext(patch.object(runner, 'RUNS_DIR', self.root / 'runs'))
        self.enterContext(patch.object(runner, 'RUNS', {}))
        self.enterContext(patch.object(runner, '_ACTIVE_KEYS', {}))
        self.host = {'id': 'source', 'node_name': 'source', 'ssh_alias': 'source', 'remote_root': '/opt/opu', 'sudo': True}
        self.record = runner.RunRecord('a' * 12, 'execute', 'plan:p:execute')
        self.record.status = 'unknown'
        self.record.context = {'detached_execution': True, 'plan_id': 'p', 'task_id': '004-datapatch-local',
                               'node': 'source', 'host_id': 'source', 'ssh_alias': 'source', 'remote_root': '/opt/opu',
                               'remote_run_dir': '/opt/opu/var/webapp-runs/p/004-datapatch-local/' + 'b' * 32,
                               'execution_host_configuration_sha256': planctl.execution_host_binding(self.host),
                               'task_definition_sha256': 'c' * 64, 'task_retry_count': 0}
        self.task = {'task_id': '004-datapatch-local', 'stage': 'datapatch', 'adapter': 'database_single_instance_opatch',
                     'task_definition_sha256': 'c' * 64, 'retry_count': 0, 'status': 'pending'}
        runner.RUNS[self.record.run_id] = self.record
        runner._ACTIVE_KEYS[self.record.key] = self.record.run_id
        self.plan = {'plan_sha256': 'd' * 64, 'state': 'paused'}
        self.enterContext(patch.object(planctl, 'status', return_value=self.plan))
        self.enterContext(patch.object(planctl, '_run', return_value=self.task))
        self.enterContext(patch.object(planctl, '_resolve_node_host', return_value=self.host))
        self.ssh = self.enterContext(patch.object(console.remote, 'run_remote_raw'))
        self.sync = self.enterContext(patch.object(planctl, '_sync_plan_to_host'))
        self.launch = self.enterContext(patch.object(planctl, '_run_detached_remote'))
        self.payload = {'plan_id': 'p', 'task_id': self.task['task_id'], 'remote_launch_id': 'b' * 32,
                        'read_only': True, 'terminal_result_verified': False,
                        'worker_heartbeat': {'available': True, 'last_renewed_at': 100},
                        'logs': {'stdout.log': {'text': 'password=hidden-secret\nAuthorization: Bearer credential\nphase complete'}}}
        self.ssh.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(self.payload), stderr='')

    def test_observe_is_fixed_bound_redacted_and_never_reconciles(self):
        value = console.observe('p', self.record.run_id)
        self.assertEqual(self.record.status, 'unknown')
        self.assertEqual(runner.active_run_id(self.record.key), self.record.run_id)
        self.assertNotIn('hidden-secret', json.dumps(value)); self.assertNotIn('credential', json.dumps(value))
        self.assertEqual(self.record.observation, value)
        args = self.ssh.call_args.args[1]
        self.assertEqual(args[:3], ['/usr/bin/env', 'python3', '-c'])
        self.assertEqual(args[4:], ['/opt/opu', 'p', self.task['task_id'], 'b' * 32, 'c' * 64, '0', 'single-instance'])
        self.sync.assert_not_called(); self.launch.assert_not_called()

    def test_changed_scope_or_attempt_stops_before_ssh(self):
        for key, value in [('remote_root', '/changed'), ('host_id', 'other'), ('remote_run_dir', '/etc/shadow'), ('task_retry_count', 1)]:
            with self.subTest(key=key), patch.dict(self.record.context, {key: value}), self.assertRaises(planctl.PlanError):
                console.observe('p', self.record.run_id)
        with self.assertRaises(planctl.PlanError): console.observe('other', self.record.run_id)
        with self.assertRaises(planctl.PlanError): console.observe('p', '../anything')
        self.ssh.assert_not_called(); self.sync.assert_not_called()

    def test_historical_launch_only_reads_its_wrapper(self):
        del self.record.context['task_definition_sha256']; del self.record.context['task_retry_count']
        console.observe('p', self.record.run_id)
        self.assertEqual(self.ssh.call_args.args[1][-3:], ['', '', ''])

    def test_observation_rejects_missing_or_invalid_launch_host_binding(self):
        for value in (None, '', 'not-a-digest'):
            with self.subTest(value=value), patch.dict(self.record.context, {'execution_host_configuration_sha256': value}):
                with self.assertRaises(planctl.PlanError):
                    console.observe('p', self.record.run_id)
        self.ssh.assert_not_called()
        self.assertIsNone(self.record.observation)

    def test_observation_rejects_privilege_and_full_host_mapping_drift(self):
        for changes in ({'sudo': False}, {'label': 'Reconfigured host'},
                        {'nodes': [{'name': 'source', 'ssh_alias': 'another-route'}]}):
            with self.subTest(changes=changes), patch.dict(self.host, changes):
                with self.assertRaises(planctl.PlanError):
                    console.observe('p', self.record.run_id)
        self.ssh.assert_not_called()
        self.assertIsNone(self.record.observation)

    def test_binding_change_during_observation_does_not_attach_previous_host_logs(self):
        def advanced(*args, **kwargs):
            self.record.context['execution_host_configuration_sha256'] = 'e' * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.payload), stderr='')
        self.ssh.side_effect = advanced
        with self.assertRaises(planctl.PlanError):
            console.observe('p', self.record.run_id)
        self.assertIsNone(self.record.observation)

    def test_advanced_context_does_not_attach_old_observation(self):
        def advanced(*args, **kwargs):
            self.record.context['remote_run_dir'] += 'f'
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.payload), stderr='')
        self.ssh.side_effect = advanced
        with self.assertRaises(planctl.PlanError): console.observe('p', self.record.run_id)
        self.assertIsNone(self.record.observation)

    def test_response_scope_and_size_rejected(self):
        for text in [json.dumps({**self.payload, 'task_id': 'other'}), ' ' * (1024 * 1024 + 1)]:
            self.ssh.return_value.stdout = text
            with self.assertRaises(planctl.PlanError): console.observe('p', self.record.run_id)

    def test_persisted_timeline_list_and_controller_loss(self):
        self.record.status = 'running'; self.record.owner = {'pid': 99999999, 'instance': 'previous-controller'}
        self.record.event('remote_launch', 'Launching worker', task_id=self.task['task_id'])
        self.record.log('token=secret-old')
        older = runner.RunRecord('e' * 12, 'other', 'recovery:r:backup'); older.created_at = 1; older._persist()
        runner.RUNS.clear()
        records = runner.list_runs('plan:p:')
        self.assertEqual([item['run_id'] for item in records], [self.record.run_id])
        self.assertEqual(records[0]['status'], 'unknown')
        self.assertEqual(records[0]['timeline'][-1]['event'], 'remote_launch')
        self.assertNotIn('secret-old', str(records[0]['log_tail']))
        self.assertEqual(len(runner.list_runs()), 2)

    def test_snapshot_preserves_unknown_and_paused_guidance(self):
        with patch.object(console, 'tasks', return_value=[{**self.task, 'status': 'unknown', 'evidence_verified': False, 'verification_error': 'missing custody'}]):
            value = console.snapshot('p')
        self.assertEqual(value['state'], 'paused'); self.assertIn('reconcile', value['guidance'])
        self.assertEqual(value['tasks'][0]['status'], 'unknown')
        self.assertNotIn('result', value['runs'][0]); self.ssh.assert_not_called()

    def test_unbound_legacy_snapshot_preserves_saved_logs_and_reconciliation_guidance(self):
        self.record.observation = {'logs': {'stdout.log': {'text': 'Saved original launch output'}}}
        for binding in (None, 'invalid'):
            with self.subTest(binding=binding), patch.dict(self.record.context, {'execution_host_configuration_sha256': binding}), \
                    patch.object(console, 'tasks', return_value=[]):
                value = console.snapshot('p')
            run = value['runs'][0]
            self.assertFalse(run['can_observe'])
            self.assertIn('no verified host configuration binding', run['observe_blocked_reason'])
            self.assertEqual(run['observation'], self.record.observation)
            self.assertEqual(run['status'], 'unknown')
            self.assertIn('reconcile', value['guidance'])
        self.ssh.assert_not_called()


class RemoteScriptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.launch = 'b' * 32
        self.wrapper = self.root / 'var/webapp-runs/p/task' / self.launch
        self.wrapper.mkdir(parents=True)
        (self.wrapper / 'rc').write_text('75\n')
        self.task_dir = self.root / 'var/webapp-plans/plans/p/tasks'; self.task_dir.mkdir(parents=True)
        keys = ('schema_version', 'task_id', 'plan_id', 'plan_sha256', 'stage', 'node', 'adapter', 'authorization_sha256', 'authorization_record_sha256')
        task = {key: None for key in keys}
        task.update(schema_version='1.0', task_id='task', plan_id='p', stage='datapatch')
        self.definition = hashlib.sha256(json.dumps(task, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        task.update(task_definition_sha256=self.definition, status='running', claimed_at_epoch=100, lease_renewed_at_epoch=200, lease_expires_epoch=300)
        (self.task_dir / 'task.json').write_text(json.dumps(task))

    def run_script(self, definition=None, generation='0'):
        return subprocess.run([sys.executable, '-c', console._READ_EXISTING, str(self.root), 'p', 'task', self.launch,
                               self.definition if definition is None else definition, generation, ''], capture_output=True, text=True)

    def test_bounded_tail_drops_partial_secret_and_reports_lease(self):
        (self.wrapper / 'stdout').write_text('password=' + 'x' * 70000 + '\nphase running\n')
        output = self.run_script(); self.assertEqual(output.returncode, 0, output.stderr)
        data = json.loads(output.stdout)
        self.assertEqual(data['logs']['wrapper_stdout']['text'], 'phase running\n')
        self.assertTrue(data['logs']['wrapper_stdout']['truncated'])
        self.assertEqual(data['worker_heartbeat']['last_renewed_at'], 200)
        self.assertFalse(data['terminal_result_verified']); self.assertEqual(data['wrapper']['exit_code'], 75)

    def test_symlink_output_is_not_read(self):
        secret = self.root / 'unrelated'; secret.write_text('private contents')
        (self.wrapper / 'stdout').symlink_to(secret)
        output = self.run_script(); self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIsNone(json.loads(output.stdout)['logs']['wrapper_stdout']['text'])
        self.assertNotIn('private contents', output.stdout)

    def test_changed_generation_and_definition_rejected(self):
        for definition, generation in [('a' * 64, '0'), (self.definition, '1')]:
            self.assertNotEqual(self.run_script(definition, generation).returncode, 0)

    def test_historical_wrapper_does_not_claim_native_heartbeat(self):
        output = self.run_script('', '')
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertFalse(json.loads(output.stdout)['worker_heartbeat']['available'])

    def test_redaction_precedes_truncation_and_covers_connect_strings(self):
        value = diagnostics.redact_text('token=' + 'a' * 20000, 50)
        self.assertNotIn('a' * 10, value)
        for value in ['connect sys/secret@ORCL', 'https://user:secret@example.test/', 'password: "secret words"', 'Bearer secret']:
            self.assertNotIn('secret', diagnostics.redact_text(value))

    def test_redaction_covers_auth_headers_environment_and_nested_credentials(self):
        keys = ['client_secret', 'access_token', 'refresh_token', 'id_token', 'OPU_WEBAPP_TOKEN', 'OPU_WEBAPP_API_KEY']
        for key in keys:
            with self.subTest(key=key):
                self.assertNotIn('credential-value', diagnostics.redact_text(f'{key}=credential-value'))
                self.assertEqual(diagnostics.redacted({'nested': {key: 'credential-value'}})['nested'][key], '[REDACTED]')
        for value in ['Authorization: Basic credential-value', 'Proxy-Authorization: Digest credential-value',
                      'sqlplus -S sys/credential-value@ORCL', '{"Authorization":"Bearer credential-value"}']:
            self.assertNotIn('credential-value', diagnostics.redact_text(value))
        value = {'plan_sha256': 'a' * 64, 'task_definition_sha256': 'b' * 64}
        self.assertEqual(diagnostics.redacted(value), value)


if __name__ == '__main__': unittest.main()
