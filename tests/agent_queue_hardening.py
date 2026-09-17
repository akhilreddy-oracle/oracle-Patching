"""Fencing, crash recovery, concurrency, and worker-heartbeat regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'webapp'))
import agent_queue as queue
import agent_enroll
import agent_worker
from adapters import EXECUTOR_PATHS


class QueueSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            'OPU_AGENT_QUEUE_DIR': str(self.base / 'queue'),
            'OPU_AGENT_REGISTRY_FILE': str(self.base / 'registry.json'),
            'OPU_AGENT_ENROLLMENT_REQUIRED': '0',
            'OPU_AGENT_TEST_MODE': '1',
            'PYTHONPATH': str(ROOT / 'webapp'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def publish(self, attempt=0, task='task1'):
        return queue.publish_task(plan_id='plan1', task_id=task, node='node1',
                                  adapter='database_single_instance_opatch',
                                  payload={'task': {'retry_count': attempt}})

    def test_concurrent_claims_are_exclusive_across_processes(self):
        self.publish()
        code = "import agent_queue,json,sys; print(json.dumps(agent_queue.claim('node1',sys.argv[1])))"
        procs = [subprocess.Popen([sys.executable, '-B', '-c', code, f'agent{i}'], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for i in range(8)]
        claims = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, stderr)
            claims.append(json.loads(stdout))
        self.assertEqual(sum(c is not None for c in claims), 1)
        claim = next(c for c in claims if c)
        self.assertNotIn(claim['claim_token'], json.dumps(queue.list_jobs()))
        self.assertNotIn(claim['claim_token'], queue._job_path(claim['job_id']).read_text())

    def test_retry_fences_old_claim_and_terminal_replay(self):
        self.publish()
        old = queue.claim('node1', 'agent1')
        queue.complete(old['job_id'], 'agent1', claim_token=old['claim_token'])
        self.assertEqual(self.publish()['status'], 'completed')
        with self.assertRaises(queue.QueueError):
            queue.complete(old['job_id'], 'agent1', claim_token=old['claim_token'])
        self.publish(attempt=1)
        new = queue.claim('node1', 'agent1')
        self.assertGreater(new['claim_generation'], old['claim_generation'])
        with self.assertRaises(queue.QueueError):
            queue.extend_lease(new['job_id'], 'agent1', 60, claim_token=old['claim_token'])
        queue.complete(new['job_id'], 'agent1', claim_token=new['claim_token'])
        self.assertTrue((queue.queue_dir() / 'history' / old['job_id'] / 'attempt-0.json').is_file())

    def test_expired_unknown_blocks_all_node_work(self):
        self.publish()
        self.publish(task='task2')
        with patch.object(queue.time, 'time', return_value=1000):
            old = queue.claim('node1', 'agent1', 30)
        with patch.object(queue.time, 'time', return_value=1031):
            self.assertIsNone(queue.claim('node1', 'agent2'))
            with self.assertRaises(queue.QueueError):
                queue.complete(old['job_id'], 'agent1', claim_token=old['claim_token'])
            with self.assertRaises(queue.QueueError):
                queue.extend_lease(old['job_id'], 'agent1', 60, claim_token=old['claim_token'])
        self.assertEqual(queue.list_jobs()[0]['status'], 'reconciliation_required')

    def test_enrollment_is_bound_to_node_and_revocation(self):
        os.environ['OPU_AGENT_ENROLLMENT_REQUIRED'] = '1'
        enrolled = agent_enroll.enroll(node='node1', agent_id='agent1')
        token = enrolled['agent_token']
        self.publish()
        with self.assertRaises(queue.QueueError):
            queue.claim('node2', 'agent1', agent_token=token)
        owned = queue.claim('node1', 'agent1', agent_token=token)
        agent_enroll.revoke('agent1')
        with self.assertRaises(queue.QueueError):
            queue.complete(owned['job_id'], 'agent1', agent_token=token, claim_token=owned['claim_token'])

    def test_malformed_or_unsafe_enrollment_registry_fails_without_authentication(self):
        enrolled = agent_enroll.enroll(node='node1', agent_id='agent1')
        registry = agent_enroll.registry_path()
        original = json.loads(registry.read_text())
        for entry in ([], None, {**original['agents']['agent1'], 'revoked': 'false'},
                      {**original['agents']['agent1'], 'token_sha256': 'invalid'}):
            registry.write_text(json.dumps({'agents': {'agent1': entry}}))
            self.assertFalse(agent_enroll.verify('agent1', enrolled['agent_token']))
            with self.assertRaises(agent_enroll.EnrollError):
                agent_enroll.list_agents()
        registry.write_bytes(b'\xff')
        self.assertFalse(agent_enroll.verify('agent1', enrolled['agent_token']))
        registry.unlink(); os.mkfifo(registry)
        self.assertFalse(agent_enroll.verify('agent1', enrolled['agent_token']))
        registry.unlink(); registry.write_text(json.dumps(original)); registry.chmod(0o666)
        self.assertFalse(agent_enroll.verify('agent1', enrolled['agent_token']))

    def test_worker_renews_during_long_executor(self):
        self.publish()
        class Process:
            returncode = 0
            calls = 0
            def communicate(self, timeout):
                self.calls += 1
                if self.calls < 3:
                    raise subprocess.TimeoutExpired('fake-executor', timeout)
                return 'completed', ''
        with patch.object(agent_worker.subprocess, 'Popen', return_value=Process()), \
             patch.object(queue, 'extend_lease', wraps=queue.extend_lease) as renew:
            job, code = agent_worker.run_once('node1', 'agent1', 30)
        self.assertEqual(code, 0)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(renew.call_count, 3)
        self.assertTrue(all(c.kwargs['claim_token'] for c in renew.call_args_list))

    def test_worker_never_reports_success_after_lost_ownership(self):
        self.publish()
        class Process:
            returncode = 0
            calls = 0
            def communicate(self, timeout):
                self.calls += 1
                if self.calls < 2:
                    raise subprocess.TimeoutExpired('fake-executor', timeout)
                return 'completed', ''
        renew_real = queue.extend_lease
        calls = []
        def renew(*args, **kwargs):
            calls.append(args)
            if len(calls) > 1:
                raise queue.QueueError('lost lease')
            return renew_real(*args, **kwargs)
        with patch.object(agent_worker.subprocess, 'Popen', return_value=Process()), \
             patch.object(queue, 'extend_lease', side_effect=renew):
            with self.assertRaises(queue.QueueError):
                agent_worker.run_once('node1', 'agent1', 30)
        self.assertNotEqual(queue.list_jobs()[0]['status'], 'completed')

    def test_all_adapters_exist_and_unknown_adapter_rejected(self):
        self.assertEqual(len(EXECUTOR_PATHS), 12)
        for executor in EXECUTOR_PATHS.values():
            self.assertTrue(os.access(ROOT / executor, os.X_OK), executor)
        with self.assertRaises(queue.QueueError):
            queue.publish_task(plan_id='p', task_id='t', node='n', adapter='unknown')

    def test_cli_input_cannot_be_python_source(self):
        marker = self.base / 'injected'
        attack = f"node'); __import__('pathlib').Path('{marker}').touch(); #"
        proc = subprocess.run([str(ROOT / 'bin/opu-agent-work-pull'), '--node', attack, '--agent-id', 'agent1'],
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(marker.exists())

    def test_plan_order_spans_nodes_and_failed_predecessor_blocks(self):
        adapter = 'database_rolling_opatch'
        for task, node in [('001-precheck-node1', 'node1'), ('002-precheck-node2', 'node2'),
                           ('003-validate-node1', 'node1')]:
            queue.publish_task(plan_id='rac', task_id=task, node=node, adapter=adapter)
        self.assertIsNone(queue.claim('node2', 'agent2'))
        first = queue.claim('node1', 'agent1')
        self.assertIsNone(queue.claim('node2', 'agent2'))
        queue.complete(first['job_id'], 'agent1', result={'status': 'success'}, claim_token=first['claim_token'])
        second = queue.claim('node2', 'agent2')
        self.assertEqual(second['task_id'], '002-precheck-node2')
        self.assertIsNone(queue.claim('node1', 'agent1'))
        queue.complete(second['job_id'], 'agent2', result={'status': 'failed'}, claim_token=second['claim_token'])
        self.assertIsNone(queue.claim('node1', 'agent1'))

    def test_local_stages_route_to_actual_host_without_rewriting_sealed_task(self):
        adapter = 'database_single_instance_opatch'
        tasks = [{'task_id': '001-precheck-node1', 'node': 'node1', 'adapter': adapter, 'status': 'pending'},
                 {'task_id': '002-datapatch-local', 'node': 'local', 'adapter': adapter, 'status': 'pending'}]
        jobs = queue.publish_plan_tasks({'plan_id': 'single', 'nodes': ['node1']}, tasks)
        self.assertEqual([j['node'] for j in jobs], ['node1', 'node1'])
        self.assertEqual(jobs[1]['payload']['task']['node'], 'local')
        first = queue.claim('node1', 'agent1')
        queue.complete(first['job_id'], 'agent1', result={'status': 'success'}, claim_token=first['claim_token'])
        self.assertEqual(queue.claim('node1', 'agent1')['task_id'], '002-datapatch-local')
        routed = queue.publish_plan_tasks({'plan_id': 'coordinator', 'nodes': ['node1', 'node2'],
                                           'target': {'coordinator_node': 'node2'}}, tasks[1:])
        self.assertEqual(routed[0]['node'], 'node2')

    def native_fixture(self, status='pending', generation=0):
        return {'plan_id': 'plan1', 'task_id': 'task1', 'adapter': 'database_single_instance_opatch',
                'status': status, 'retry_count': generation}

    def test_unready_native_task_defers_without_launch_and_invalidates_claim(self):
        self.publish()
        captured = []
        claim_real = queue.claim
        def claim(*args, **kwargs):
            job = claim_real(*args, **kwargs); captured.append(job); return job
        def native(command, _job):
            task = self.native_fixture()
            if command == 'next': task['task_id'] = 'earlier-task'
            return task
        with patch.dict(os.environ, {'OPU_AGENT_TEST_MODE': '0', 'OPU_PLAN_STATE_DIR': str(self.base / 'plans')}), \
             patch.object(queue, 'claim', side_effect=claim), patch.object(queue, '_native_json', side_effect=native), \
             patch.object(agent_worker.subprocess, 'Popen') as execute:
            job, code = agent_worker.run_once('node1', 'agent1')
        self.assertEqual((job['status'], code), ('queued', 75))
        execute.assert_not_called()
        with self.assertRaises(queue.QueueError):
            queue.extend_lease(job['job_id'], 'agent1', 60, claim_token=captured[0]['claim_token'])
        new = queue.claim('node1', 'agent1')
        self.assertGreater(new['claim_generation'], captured[0]['claim_generation'])

    def test_native_claim_refusal_defers_and_same_attempt_remains_usable(self):
        self.publish()
        class Process:
            returncode = 65
            def communicate(self, timeout):
                return '', 'a rolling task is already running'
        with patch.dict(os.environ, {'OPU_AGENT_TEST_MODE': '0', 'OPU_PLAN_STATE_DIR': str(self.base / 'plans')}), \
             patch.object(queue, '_native_json', return_value=self.native_fixture()), \
             patch.object(agent_worker.subprocess, 'Popen', return_value=Process()) as execute:
            job, code = agent_worker.run_once('node1', 'agent1')
        self.assertEqual((job['status'], code), ('queued', 75))
        execute.assert_called_once()
        self.assertEqual(self.publish()['status'], 'queued')
        self.assertIsNotNone(queue.claim('node1', 'agent1'))

    def test_uncertain_or_other_generation_native_state_never_defers_or_executes(self):
        for status, generation in [('running', 0), ('unknown', 0), ('failed', 0), ('pending', 1)]:
            with self.subTest(status=status, generation=generation):
                with patch.dict(os.environ, {'OPU_AGENT_QUEUE_DIR': str(self.base / f'queue-{status}-{generation}'),
                                             'OPU_AGENT_TEST_MODE': '0', 'OPU_PLAN_STATE_DIR': str(self.base / 'plans')}), \
                     patch.object(queue, '_native_json', return_value=self.native_fixture(status, generation)), \
                     patch.object(agent_worker.subprocess, 'Popen') as execute:
                    self.publish()
                    with self.assertRaises(queue.QueueError):
                        agent_worker.run_once('node1', 'agent1')
                    self.assertEqual(queue.list_jobs()[0]['status'], 'reconciliation_required')
                    execute.assert_not_called()
                    self.assertIsNone(queue.claim('node1', 'agent1'))

    def test_real_completion_requires_verified_terminal_attempt_and_worker_requires_state(self):
        self.publish()
        with patch.dict(os.environ, {'OPU_AGENT_TEST_MODE': '0', 'OPU_PLAN_STATE_DIR': ''}):
            with self.assertRaises(queue.QueueError): agent_worker.run_once('node1', 'agent1')
            self.assertEqual(queue.list_jobs()[0]['status'], 'queued')
        claim = queue.claim('node1', 'agent1')
        with patch.dict(os.environ, {'OPU_AGENT_TEST_MODE': '0'}), \
             patch.object(queue, '_native_json', return_value=self.native_fixture()):
            with self.assertRaises(queue.QueueError):
                queue.complete(claim['job_id'], 'agent1', result={'status': 'success'}, claim_token=claim['claim_token'])
        self.assertEqual(queue.list_jobs()[0]['status'], 'claimed')
        with patch.dict(os.environ, {'OPU_AGENT_TEST_MODE': '0'}), \
             patch.object(queue, '_native_json', return_value=self.native_fixture('failed')):
            done = queue.complete(claim['job_id'], 'agent1', result={'status': 'success'}, claim_token=claim['claim_token'])
        self.assertEqual(done['result']['status'], 'failed')
        self.assertEqual(done['result']['source'], 'sealed_plan_task')

    def test_post_launch_uncertainty_blocks_instead_of_reporting_success(self):
        self.publish()
        native = self.native_fixture()
        class Process:
            returncode = 0
            def communicate(self, timeout):
                native['status'] = 'unknown'
                return 'child exited', ''
        with patch.dict(os.environ, {'OPU_AGENT_TEST_MODE': '0', 'OPU_PLAN_STATE_DIR': str(self.base / 'plans')}), \
             patch.object(queue, '_native_json', side_effect=lambda *_: dict(native)), \
             patch.object(agent_worker.subprocess, 'Popen', return_value=Process()):
            with self.assertRaises(queue.QueueError):
                agent_worker.run_once('node1', 'agent1')
        self.assertEqual(queue.list_jobs()[0]['status'], 'reconciliation_required')

    def test_reconciliation_native_wait_does_not_block_other_node_renewal(self):
        self.publish()
        first = queue.claim('node1', 'agent1')
        queue.require_reconciliation(first['job_id'], 'agent1', 'lost native result', claim_token=first['claim_token'])
        queue.publish_task(plan_id='other', task_id='task2', node='node2', adapter='database_rolling_opatch')
        other = queue.claim('node2', 'agent2')
        verifying, release = threading.Event(), threading.Event()
        def native(*_args):
            verifying.set()
            if not release.wait(5): raise AssertionError('native verifier was not released')
            return self.native_fixture('succeeded')
        with patch.object(queue, '_native_json', side_effect=native), ThreadPoolExecutor(max_workers=2) as pool:
            reconcile = pool.submit(queue.reconcile, first['job_id'], 'operator')
            try:
                self.assertTrue(verifying.wait(2))
                renew = pool.submit(queue.extend_lease, other['job_id'], 'agent2', 60, claim_token=other['claim_token'])
                self.assertEqual(renew.result(timeout=2)['status'], 'running')
                self.assertFalse(reconcile.done())
            finally:
                release.set()
            self.assertEqual(reconcile.result(timeout=2)['status'], 'completed')

    def test_reconciliation_rechecks_attempt_fence_after_native_proof(self):
        self.publish()
        first = queue.claim('node1', 'agent1')
        queue.require_reconciliation(first['job_id'], 'agent1', 'lost native result', claim_token=first['claim_token'])
        for field in ('claim_generation', 'attempt'):
            with self.subTest(field=field):
                def native(*_args):
                    with queue.file_lock(queue.queue_dir() / '.queue.lock'):
                        path = queue._job_path(first['job_id'])
                        job = queue._read_job(path)
                        snapshot = self.native_fixture('succeeded', job['attempt'])
                        job[field] = job.get(field, 0) + 1
                        queue._write_job(path, job)
                    return snapshot
                with patch.object(queue, '_native_json', side_effect=native):
                    with self.assertRaisesRegex(queue.QueueError, 'attempt changed'):
                        queue.reconcile(first['job_id'], 'operator')
                self.assertEqual(queue.list_jobs()[0]['status'], 'reconciliation_required')


if __name__ == '__main__':
    unittest.main()
