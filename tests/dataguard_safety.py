#!/usr/bin/env python3
"""Data Guard control boundaries with a local fake broker; never Oracle/SSH."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sealed(path, data):
    data = {key: value for key, value in data.items() if key != 'record_sha256'}
    data['record_sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    path.write_text(json.dumps(data))
    return data


class DataGuardSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'oracle home'; (self.home / 'bin').mkdir(parents=True)
        self.calls = self.root / 'calls'
        broker = self.home / 'bin/dgmgrl'
        broker.write_text('''#!/bin/sh
printf '%s|%s|%s\n' "$ORACLE_HOME" "$ORACLE_SID" "$3" >>"$DG_TEST_CALLS"
case "$3" in
  SWITCHOVER*)
    [ "$DG_TEST_BEHAVIOR" != empty ] || exit 0
    [ "$DG_TEST_BEHAVIOR" != diagnostic ] || { echo 'DGM-17016: failed'; exit 0; }
    echo 'Switchover succeeded, new primary is "STBY"';;
  REINSTATE*) echo 'Reinstatement of database "STBY" succeeded';;
  'SHOW CONFIGURATION;')
    [ "$DG_TEST_BEHAVIOR" != configuration_failure ] || { echo 'configuration query disconnected'; exit 1; }
    printf 'Configuration - fixture\nConfiguration Status:\nSUCCESS\n';;
  'SHOW DATABASE'* )
    [ "$DG_TEST_BEHAVIOR" != database_failure ] || { echo 'database query disconnected'; exit 1; }
    [ "$DG_TEST_BEHAVIOR" != wrong_role ] || { printf 'Role: PHYSICAL STANDBY\nSUCCESS\n'; exit 0; }
    printf 'Role: %s\nDatabase Status:\nSUCCESS\n' "$DG_TEST_ROLE";;
  *) exit 64;;
esac
''')
        broker.chmod(0o700)
        self.now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.observe = self.root / 'observe.json'; self.gate = self.root / 'gate.json'
        self.document = {'schema_version': '1.0', 'collector': {'name': 'oracle.dataguard.observe', 'version': '1'},
            'collected_at': self.now, 'target': {'oracle_home': str(self.home), 'oracle_sid': 'PRIMARY'},
            'primary': {'database_role': 'PRIMARY'}, 'members': [{'db_unique_name': 'STBY', 'apply_lag_seconds': 0}]}
        sealed(self.observe, self.document)
        self.bind_gate()

    def bind_gate(self, operation='switchover', timestamp=None):
        return sealed(self.gate, {'schema_version': '1.0', 'status': 'ready_for_' + operation,
            'evaluated_at': timestamp or self.now, 'validation_mode': 'operator_override',
            'evidence': {'observe_sha256': hashlib.sha256(self.observe.read_bytes()).hexdigest()}})

    def execute(self, operation='switchover', behavior='normal', output=None):
        target = '--target-standby' if operation == 'switchover' else '--database'
        args = [str(ROOT / ('bin/opu-dataguard-' + operation)), 'execute', '--gate', str(self.gate),
                '--observe', str(self.observe), target, 'STBY', '--actor', 'fixture']
        if output is not None: args += ['--output', str(output)]
        env = {**os.environ, 'OPU_DATAGUARD_TEST_MODE': '0', 'OPU_PRODUCTION_MODE': '0',
               'OPU_DATAGUARD_MAX_EXECUTION_AGE_SECONDS': '300', 'ORACLE_HOME': '/wrong/home', 'ORACLE_SID': 'WRONG',
               'DG_TEST_CALLS': str(self.calls), 'DG_TEST_BEHAVIOR': behavior,
               'DG_TEST_ROLE': 'PRIMARY' if operation == 'switchover' else 'PHYSICAL STANDBY'}
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)

    def test_success_requires_observed_home_identity_and_verified_broker_role(self):
        for operation in ('switchover', 'reinstate'):
            with self.subTest(operation=operation):
                self.bind_gate(operation)
                result = self.execute(operation)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], 'succeeded')
                calls = self.calls.read_text().splitlines()
                self.assertTrue(all(line.startswith(str(self.home) + '|PRIMARY|') for line in calls))
                self.assertIn('SHOW DATABASE', calls[-1])

    def test_empty_diagnostic_or_wrong_role_is_unknown_even_with_exit_zero(self):
        for behavior in ('empty', 'diagnostic', 'wrong_role'):
            with self.subTest(behavior=behavior):
                result = self.execute(behavior=behavior)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], 'unknown')

    def test_stale_correctly_sealed_gate_never_starts_broker(self):
        self.bind_gate(timestamp='2000-01-01T00:00:00Z')
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_failed_postchecks_keep_diagnostics_for_unknown_outcome_recovery(self):
        for behavior, message in (('configuration_failure', 'configuration query disconnected'),
                                  ('database_failure', 'database query disconnected')):
            with self.subTest(behavior=behavior):
                output = self.root / (behavior + '.json')
                result = self.execute(behavior=behavior, output=output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], 'unknown')
                logs = list(self.root.glob(output.name + '.dgmgrl.*'))
                self.assertEqual(len(logs), 1)
                self.assertIn(message, logs[0].read_text())

    def test_simulated_and_legacy_gates_never_authorize_native_broker(self):
        for mode in ('simulated', None):
            data = self.bind_gate()
            if mode is None: data.pop('validation_mode')
            else: data['validation_mode'] = mode
            sealed(self.gate, data)
            self.assertNotEqual(self.execute().returncode, 0)
            self.assertFalse(self.calls.exists())

    def test_changed_observation_cannot_be_laundered_through_new_gate(self):
        changed = json.loads(self.observe.read_text()); changed['target']['oracle_sid'] = 'OTHER'
        self.observe.write_text(json.dumps(changed)); self.bind_gate()
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_unsafe_output_rejected_before_broker_and_victim_is_preserved(self):
        victim = self.root / 'victim'; victim.write_text('keep')
        for kind in ('symlink', 'fifo', 'directory'):
            destination = self.root / kind
            if kind == 'symlink': destination.symlink_to(victim)
            elif kind == 'fifo': os.mkfifo(destination)
            else: destination.mkdir()
            self.assertNotEqual(self.execute(output=destination).returncode, 0)
            self.assertFalse(self.calls.exists())
        self.assertEqual(victim.read_text(), 'keep')

    def test_standby_order_reaches_orchestration_and_seals_cannot_be_ignored(self):
        self.document['primary']['database_role'] = 'PHYSICAL STANDBY'
        sealed(self.observe, self.document)
        evaluation = self.root / 'eval.json'
        sealed(evaluation, {'status': 'ready_for_standby_first', 'evaluated_at': self.now,
            'evidence': {'observe_sha256': hashlib.sha256(self.observe.read_bytes()).hexdigest()}})
        order = self.root / 'order.json'
        args = [str(ROOT / 'bin/opu-dataguard-plan-order'), '--observe', str(self.observe), '--evaluation', str(evaluation), '--output', str(order)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(ROOT / 'bin/opu-dataguard-orchestrate'), '--observe', str(self.observe),
            '--evaluation', str(evaluation), '--order', str(order)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        step = next(step for step in json.loads(result.stdout)['steps'] if step['step'] == 'patch_standbys')
        self.assertEqual(step['targets'][0]['role'], 'STANDBY')
        data = json.loads(evaluation.read_text()); data['evaluated_at'] = 'changed'
        evaluation.write_text(json.dumps(data))
        self.assertNotEqual(subprocess.run(args, capture_output=True, text=True, timeout=20).returncode, 0)


if __name__ == '__main__': unittest.main()
