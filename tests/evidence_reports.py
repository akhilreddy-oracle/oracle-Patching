#!/usr/bin/env python3
"""Verified evidence comparison, absent data and safe export fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'webapp'))
import evidence_reports as reports


def write(path, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value).encode() if not isinstance(value, str) else value.encode())
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.object(reports.planctl, 'PLAN_STATE_DIR', self.root))
        self.target = {'database_unique_name': 'ORCL', 'oracle_home': '/u01/dbhome_1'}
        self.plan = {'plan_id': 'p', 'plan_sha256': 'a' * 64, 'state': 'paused', 'patch_id': '39034528', 'target': self.target}
        self.enterContext(patch.object(reports.planctl, 'status', return_value=self.plan))
        self.tasks = []; self.enterContext(patch.object(reports.execution_console, 'tasks', side_effect=lambda _: self.tasks))

    def snapshot(self, document=None):
        document = document if document is not None else {
            'oracle_homes': [{'path': '/u01/dbhome_1', 'patches': ['123']}],
            'databases': [{'db_unique_name': 'ORCL', 'oracle_home': '/u01/dbhome_1', 'runtime': {'status': 'complete', 'invalid_objects': 0,
                           'database_role': 'PRIMARY', 'open_mode': 'READ WRITE', 'instance_state': 'OPEN'}}]}
        path = self.root / 'snapshot.json'
        self.plan['snapshot_evidence'] = [{'path': str(path), 'sha256': write(path, document)}]
        return path

    def task(self, task_id='004-datapatch-local', stage='datapatch', status='succeeded', files=None):
        directory = self.root / 'plans/p/evidence' / task_id
        native = {'plan_id': 'p', 'plan_sha256': self.plan['plan_sha256'], 'task_id': task_id, 'stage': stage,
                  'status': status, 'target': {**self.target, 'oracle_sid': 'ORCL'}, 'record_sha256': 'c' * 64,
                  'finished_at': '2026-09-15T10:00:00Z'}
        entries = []
        for name, content in (files or {}).items():
            role = name.split('.')[0] if name in {'stdout.log', 'stderr.log'} else 'artifact'
            entries.append({'custody_path': str(directory / name), 'sha256': write(directory / name, content), 'role': role})
        evidence_sha = write(directory / 'evidence.json', native)
        manifest_sha = write(directory / 'custody.json', {'files': entries})
        row = {'task_id': task_id, 'stage': stage, 'status': status, 'evidence_verified': True,
               'evidence_sha256': evidence_sha, 'evidence_custody': {'manifest_sha256': manifest_sha}}
        self.tasks.append(row)
        return directory

    def comparison(self, report, metric):
        return next(row for row in report['comparison'] if row['metric'] == metric)

    def test_paused_history_compares_verified_sources_without_claiming_completion(self):
        self.snapshot()
        self.task(files={'datapatch-lspatches.log': '39034528;Database release update\n',
                         'datapatch-sqlpatch.log': 'SQLPATCH_LATEST_ACTION=APPLY\nSQLPATCH_LATEST_STATUS=SUCCESS\n',
                         'datapatch-dictionary.log': 'INVALID_OBJECTS=0\nCOMPONENT=CATALOG|VALID\nCOMPONENT=RAC|OPTION OFF\n'})
        report = reports.build('p')
        inventory = self.comparison(report, 'binary_inventory')
        self.assertEqual(inventory['before']['value'], ['123']); self.assertEqual(inventory['after']['value'], ['39034528'])
        self.assertTrue(inventory['changed']); self.assertFalse(report['completion_verified'])
        self.assertEqual(self.comparison(report, 'sql_patch_status')['after']['value'], 'APPLY/SUCCESS')
        self.assertEqual(self.comparison(report, 'listener_ready')['after']['status'], 'unknown')
        self.assertEqual(report['rollback']['status'], 'not_available'); self.assertFalse(report['rollback']['approved'])

    def test_snapshot_hash_change_and_wrong_target_remain_unknown(self):
        path = self.snapshot(); path.write_text('{}')
        report = reports.build('p')
        self.assertEqual(self.comparison(report, 'binary_inventory')['before']['status'], 'unknown')
        self.assertTrue(report['gaps'])
        self.snapshot({'oracle_homes': [{'path': '/other/home', 'patches': ['999']}], 'databases': []})
        self.assertEqual(self.comparison(reports.build('p'), 'binary_inventory')['before']['status'], 'unknown')

    def test_malformed_and_absent_historical_observations_are_structured_unknowns(self):
        for document in [[], {'oracle_homes': None, 'databases': [None]}, {'databases': [{'db_unique_name': 'ORCL', 'oracle_home': '/u01/dbhome_1', 'runtime': []}]}]:
            self.snapshot(document)
            report = reports.build('p')
            self.assertFalse(report['completion_verified'])
            self.assertTrue(all(row['after']['status'] == 'unknown' for row in report['comparison']))

    def test_failed_stage_does_not_replace_last_verified_observation(self):
        self.task(files={'datapatch-dictionary.log': 'INVALID_OBJECTS=0\nCOMPONENT=CATALOG|VALID\n'})
        self.task('005-final-validate-local', 'final_validate', 'failed', {'final-dictionary.log': 'INVALID_OBJECTS=999\n'})
        report = reports.build('p')
        self.assertEqual(self.comparison(report, 'invalid_objects')['after']['value'], 0)
        self.assertEqual(len(report['evidence']['tasks']), 2)

    def test_custody_change_rolls_back_all_facts_from_that_task(self):
        directory = self.task(files={'a-dictionary.log': 'INVALID_OBJECTS=0\n', 'z-lspatches.log': '39034528;RU\n'})
        (directory / 'z-lspatches.log').write_text('corrupted')
        report = reports.build('p')
        self.assertEqual(self.comparison(report, 'invalid_objects')['after']['status'], 'unknown')
        self.assertEqual(report['verified_sources'], []); self.assertTrue(report['gaps'])

    def test_failed_diagnostics_are_bound_redacted_and_never_health_facts(self):
        self.task('005-final-validate-local', 'final_validate', 'failed', {
            'stderr.log': 'extjob ownership/mode differs from README: uid=54321 mode=0755; expected uid=0 mode=4750\npassword=do-not-export\n<script>evil()</script>',
            'stdout.log': 'INVALID_OBJECTS=0\n',
        })
        report = reports.build('p')
        failure = report['evidence']['tasks'][0]
        self.assertEqual(len(failure['failure_diagnostics']), 2)
        self.assertIn('provenance', failure['next_action'])
        self.assertIn('mode=0755', failure['failure_diagnostics'][0]['text'])
        self.assertNotIn('do-not-export', json.dumps(report))
        self.assertEqual(self.comparison(report, 'invalid_objects')['after']['status'], 'unknown')
        exported, _, _ = reports.export(report, 'html')
        self.assertIn('Saved failure diagnostics', exported)
        self.assertIn('mode=0755', exported)
        self.assertNotIn('<script>', exported)
        self.assertNotIn('do-not-export', exported)

    def test_tampered_failure_log_cannot_supply_diagnostic_or_repair_guidance(self):
        directory = self.task('005-final-validate-local', 'final_validate', 'failed', {'stderr.log': 'failure'})
        (directory / 'stderr.log').write_text('extjob ownership/mode differs from README: forged')
        report = reports.build('p')
        self.assertEqual(report['evidence']['tasks'], [])
        self.assertTrue(report['gaps'])
        self.assertNotIn('forged', json.dumps(report))

    def test_verified_final_is_only_a_prerequisite_for_native_rollback_validation(self):
        self.plan['state'] = 'succeeded'
        self.task('005-final-validate-local', 'final_validate', files={'report-listener.log': 'Instance "ORCL", status READY, has 1 handler\n'})
        report = reports.build('p')
        self.assertTrue(report['completion_verified'])
        self.assertTrue(self.comparison(report, 'listener_ready')['after']['value'])
        self.assertEqual(report['rollback']['status'], 'requires_native_validation')
        self.assertEqual(report['rollback']['native_validation'], 'not_run')
        self.assertFalse(report['rollback']['approved'])

    def test_corrupt_final_cannot_enable_rollback_prerequisite(self):
        self.plan['state'] = 'succeeded'
        directory = self.task('005-final-validate-local', 'final_validate', files={'final-health.log': 'DATABASE_UNIQUE_NAME=ORCL\n'})
        (directory / 'final-health.log').write_text('changed')
        report = reports.build('p')
        self.assertFalse(report['completion_verified']); self.assertEqual(report['rollback']['status'], 'not_available')

    def test_safe_source_read_rejects_symlinks_and_hardlinks(self):
        path = self.root / 'file'; digest = write(path, 'data')
        link = self.root / 'symlink'; link.symlink_to(path)
        with self.assertRaises(OSError): reports._checked(link, digest)
        hard = self.root / 'hardlink'; hard.hardlink_to(path)
        with self.assertRaises(ValueError): reports._checked(path, digest)

    def test_export_escapes_dynamic_html_formulas_and_secrets(self):
        report = reports.build('p')
        report['plan_id'] = '<script>alert(1)</script>'
        row = report['comparison'][0]; row['label'] = '=SUM(1,2)'
        row['after'] = {'status': 'verified', 'value': '<img src=x onerror=evil()>', 'source': {'label': 'token=hidden-token'}}
        report['extra_log'] = 'connect sys/hidden-password@ORCL'
        body, content_type, name = reports.export(report, 'html')
        self.assertNotIn('<script>', body); self.assertNotIn('<img ', body)
        self.assertIn('&lt;img', body); self.assertNotIn('hidden-token', body)
        self.assertEqual(content_type, 'text/html; charset=utf-8'); self.assertNotIn('/', name)
        csv, _, _ = reports.export(report, 'csv'); self.assertIn("'=SUM", csv)
        exported, _, _ = reports.export(report, 'json')
        self.assertNotIn('hidden-password', exported); self.assertNotIn('hidden-token', exported)


if __name__ == '__main__': unittest.main()
