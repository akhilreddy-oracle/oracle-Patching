"""Code-review regressions for evidence completeness and execution ownership.

Uses real temporary evidence/queue files and OS locks. SSH/Oracle boundaries
are replaced with assertions: a rejected request must never reach them.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'webapp'))
import agent_queue
import auth
import evidence
import pipeline_runner
import pipeline_steps
import planctl
import server


class ControllerEvidenceIntegrity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(evidence, 'VAR_DIR', self.root / 'hosts'))
        self.enterContext(patch.object(planctl, 'PLAN_STATE_DIR', self.root / 'plans'))
        self.enterContext(patch.object(pipeline_runner, 'RUNS_DIR', self.root / 'runs'))
        self.enterContext(patch.object(pipeline_runner, 'RUNS', {}))
        self.enterContext(patch.object(pipeline_runner, '_ACTIVE_KEYS', {}))
        self.enterContext(patch.dict(os.environ, {'OPU_AGENT_QUEUE_DIR': str(self.root / 'queue'),
            'OPU_AGENT_TEST_MODE': '1', 'OPU_AGENT_ENROLLMENT_REQUIRED': '0'}))
        self.host = {'id': 'cluster', 'ssh_alias': 'n1', 'remote_root': '/opt/opu',
                     'nodes': [{'name': 'n1', 'ssh_alias': 'n1'}, {'name': 'n2', 'ssh_alias': 'n2'}]}
        self.input = {'artifact_sha256': 'a' * 64, 'required_opatch_version': '12.2.0.1.51',
                      'oracle_references': [{'kind': 'patch_readme', 'identifier': 'README.html', 'sha256': 'b' * 64}]}
        self.artifact = {'artifact': {'status': 'ready_for_catalog', 'sha256': 'a' * 64,
                                     'readme_files': [{'path': 'README.html', 'sha256': 'b' * 64}]}}

    def test_refresh_preserves_reviewed_requirements_without_rebinding(self):
        evidence.write_evidence('cluster', 'artifact', self.artifact)
        result = pipeline_steps._refresh_procedure_input('cluster', self.input)
        self.assertEqual(result, self.input)
        result['oracle_references'][0]['sha256'] = 'changed'
        self.assertEqual(self.input['oracle_references'][0]['sha256'], 'b' * 64)

    def test_changed_media_or_readme_requires_new_review_not_hash_substitution(self):
        for mutation in ('media', 'readme', 'removed'):
            artifact = copy.deepcopy(self.artifact)
            if mutation == 'media': artifact['artifact']['sha256'] = 'c' * 64
            elif mutation == 'readme': artifact['artifact']['readme_files'][0]['sha256'] = 'c' * 64
            else: artifact['artifact']['readme_files'] = []
            evidence.write_evidence('cluster', 'artifact', artifact)
            with self.subTest(mutation=mutation), self.assertRaises(pipeline_steps.localtools.LocalToolError):
                pipeline_steps._refresh_procedure_input('cluster', self.input)
        self.assertEqual(self.input['artifact_sha256'], 'a' * 64)
        self.assertEqual(self.input['oracle_references'][0]['sha256'], 'b' * 64)

    def test_incomplete_node_index_never_falls_back_to_primary_or_subset(self):
        primary = evidence.write_evidence('cluster', 'snapshot', {'host': {'name': 'n1'}})
        n1 = evidence.write_evidence('cluster', 'snapshot_n1', {'host': {'name': 'n1'}})
        index = {'nodes': [{'name': 'n1', 'evidence': 'snapshot_n1'}, {'name': 'n2', 'evidence': 'snapshot_n2'}]}
        for broken in (None, index, {'nodes': []}, {'nodes': [index['nodes'][0], index['nodes'][0]]}, {'nodes': [None]}):
            evidence.write_evidence('cluster', 'snapshot_nodes', broken)
            with self.subTest(index=broken), self.assertRaises(evidence.EvidenceError):
                evidence.list_snapshot_paths('cluster')
        evidence.clear_evidence('cluster', 'snapshot_nodes')
        self.assertEqual(evidence.list_snapshot_paths('cluster'), [primary])
        n2 = evidence.write_evidence('cluster', 'snapshot_n2', {'host': {'name': 'n2'}})
        evidence.write_evidence('cluster', 'snapshot_nodes', index)
        self.assertEqual(evidence.list_snapshot_paths('cluster'), [n1, n2])

    def test_create_rechecks_approved_evidence_in_worker_before_native_creation(self):
        evidence.write_evidence('cluster', 'procedure_input', {'patch_id': '12345', 'target': {'database_unique_name': 'ORCL'}})
        digest = evidence.creation_binding('cluster', self.host, '12345', 'ORCL')
        evidence.write_evidence('cluster', 'policy', {'recovery': {'require_backup': False}})
        with patch.object(planctl, '_create_from_host_evidence') as create:
            with self.assertRaisesRegex(planctl.PlanError, 'changed after confirmation'):
                planctl.create('p1', 'requester', 'cluster', 'start', 'end', patch_id='12345', database='ORCL',
                    expected_creation_binding_sha256=digest, hosts={'cluster': self.host})
            create.assert_not_called()

    def test_create_holds_host_lock_through_native_sealing(self):
        evidence.write_evidence('cluster', 'procedure_input', {'patch_id': '12345', 'target': {'database_unique_name': 'ORCL'}})
        digest = evidence.creation_binding('cluster', self.host, '12345', 'ORCL')
        def native(*args):
            with self.assertRaisesRegex(evidence.EvidenceError, 'evidence is in use'):
                with evidence.host_lock('cluster'):
                    self.fail('a writer entered while plan inputs were being sealed')
            return {'plan_id': 'p1'}
        with patch.object(planctl, '_create_from_host_evidence', side_effect=native) as create:
            result = planctl.create('p1', 'requester', 'cluster', 'start', 'end', patch_id='12345', database='ORCL',
                expected_creation_binding_sha256=digest, hosts={'cluster': self.host})
            self.assertEqual(result['plan_id'], 'p1')
            create.assert_called_once()

    def test_create_rejects_unresolved_host_pipeline_before_native_creation(self):
        with patch.object(pipeline_runner, 'active_run_id', return_value='old-run'), \
             patch.object(planctl, '_create_from_host_evidence') as create:
            with self.assertRaisesRegex(planctl.PlanError, 'unresolved pipeline'):
                planctl.create('p1', 'requester', 'cluster', 'start', 'end')
            create.assert_not_called()

    def test_multinode_compatibility_requires_all_per_node_snapshots_before_ssh(self):
        for name in ('snapshot', 'artifact', 'procedure'):
            evidence.write_evidence('cluster', name, {'host': {'name': 'n1'}})
        with patch.object(pipeline_steps.tools_sync, 'ensure_host_tools') as sync, \
             patch.object(pipeline_steps.remote, 'push_file') as push, \
             patch.object(pipeline_steps.remote, 'run_remote_json') as native:
            with self.assertRaises(pipeline_steps.localtools.LocalToolError):
                pipeline_steps.step_compatibility_collect('cluster', self.host, {'artifact_dir': '/stage/patch'})
            sync.assert_not_called(); push.assert_not_called(); native.assert_not_called()

    def test_failed_index_publication_does_not_leave_primary_authority(self):
        write = evidence.write_evidence
        def fail_index(host_id, name, payload):
            if name == 'snapshot_nodes': raise OSError('simulated full disk')
            return write(host_id, name, payload)
        with patch.object(pipeline_steps.tools_sync, 'ensure_host_tools'), \
             patch.object(pipeline_steps.remote, 'run_remote_json', return_value={'host': {'name': 'n1'}}), \
             patch.object(evidence, 'write_evidence', side_effect=fail_index):
            with self.assertRaises(OSError): pipeline_steps.step_discovery('cluster', self.host, {})
        self.assertIsNone(evidence.read_evidence('cluster', 'snapshot'))
        self.assertEqual(evidence.list_snapshot_paths('cluster'), [])

    def test_queued_agent_work_blocks_http_before_plan_read_or_remote_sync(self):
        agent_queue.publish_task(plan_id='plan1', task_id='task1', node='n1',
            adapter='database_single_instance_opatch', payload={'task': {'retry_count': 0}})
        with patch.object(planctl, 'status') as status, patch.object(planctl, '_execute_live') as execute:
            with self.assertRaises(planctl.PlanError): planctl.execute_next_task('plan1', 'operator')
            status.assert_not_called(); execute.assert_not_called()

    def test_queue_publication_cannot_cross_an_http_execution_lock(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            with planctl.transport_lock('plan1'):
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with patch.object(planctl, 'status') as status, patch.object(agent_queue, 'publish_plan_tasks') as publish:
                with self.assertRaises(planctl.PlanError): planctl.publish_agent_queue('plan1')
                status.assert_not_called(); publish.assert_not_called()
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_unknown_http_execution_blocks_queue_after_process_lock_is_released(self):
        run_id = 'a' * 12
        directory = pipeline_runner.RUNS_DIR / run_id
        directory.mkdir(parents=True)
        (directory / 'run.json').write_text(json.dumps({'run_id': run_id, 'key': 'plan:plan1:execute', 'status': 'running'}))
        with patch.object(agent_queue, 'publish_plan_tasks') as publish:
            with self.assertRaises(planctl.PlanError): planctl.publish_agent_queue('plan1')
            publish.assert_not_called()

    def test_invalid_persisted_status_is_unknown_and_cannot_release_execution(self):
        run_id = 'b' * 12
        directory = pipeline_runner.RUNS_DIR / run_id
        directory.mkdir(parents=True)
        for status in (None, 'complete', [], False):
            (directory / 'run.json').write_text(json.dumps({'run_id': run_id, 'key': 'plan:plan1:execute', 'status': status}))
            with self.subTest(status=status):
                self.assertEqual(pipeline_runner._load_persisted(run_id).status, 'unknown')
                with self.assertRaises(pipeline_runner.RunConflict): pipeline_runner.active_run_id('plan:plan1:execute')

    def test_invalid_rbac_flag_does_not_fall_back_to_shared_lab_auth(self):
        for flag in ('tru', 'enabled', '2'):
            with self.subTest(flag=flag), patch.dict(os.environ, {'OPU_WEBAPP_RBAC': flag}), self.assertRaises(auth.AuthError):
                auth.rbac_enabled()

    def test_label_is_optional_for_estate_as_it_is_for_host_inventory(self):
        result = server.summarize_discovery({'id': 'node'}, {'host': {'name': 'node'}}, None)
        self.assertEqual(result['label'], 'node')

    def test_executor_stdout_cannot_claim_success_for_another_task_or_scalar(self):
        for payload in ([], 1, True, {'status': 'succeeded'}, {'task_id': 'other', 'status': 'succeeded'},
                        {'task_id': 'task1', 'status': 'failed'}):
            with self.subTest(payload=payload), self.assertRaises(planctl.PlanError):
                planctl._parse_executor_result('task1', 0, json.dumps(payload), '')
        valid = {'task_id': 'task1', 'status': 'succeeded'}
        self.assertEqual(planctl._parse_executor_result('task1', 0, json.dumps(valid), ''), valid)

    def test_transport_lock_spans_the_complete_controller_execution(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def execute(*_args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test release timed out')
            return {'task_id': 'task1', 'status': 'succeeded'}
        def run():
            try: results.append(planctl.execute_next_task('plan1', 'operator'))
            except Exception as exc: errors.append(exc)
        with patch.object(planctl, 'status', return_value={'state': 'running'}), \
             patch.object(planctl, 'next_task', return_value={'task_id': 'task1'}), \
             patch.object(planctl, '_fixture_dir_for_plan', return_value=None), \
             patch.object(planctl, '_execute_live', side_effect=execute):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(planctl.PlanError): planctl.publish_agent_queue('plan1')
                with self.assertRaises(planctl.PlanError): planctl.execute_next_task('plan1', 'other')
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{'task_id': 'task1', 'status': 'succeeded'}])


if __name__ == '__main__': unittest.main()
