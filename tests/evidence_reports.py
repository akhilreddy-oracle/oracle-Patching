#!/usr/bin/env python3
"""Verified evidence comparison, absent data and safe export fixtures."""
import hashlib
import json
import os
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

    def test_sorted_mutation_artifacts_cannot_replace_after_inventory_with_before(self):
        for stage, prefix in (('rac_opatch_apply', 'apply'), ('grid_opatch_apply', 'apply'),
                              ('rac_opatch_rollback', 'rollback'), ('grid_opatch_rollback', 'rollback')):
            with self.subTest(stage=stage):
                self.tasks.clear()
                self.task('mutation-' + stage, stage, files={
                    prefix + '-after-lspatches.log': '123;resulting inventory\n',
                    prefix + '-before-lspatches.log': '999;previous inventory\n',
                })
                fact = self.comparison(reports.build('p'), 'binary_inventory')['after']
                self.assertEqual(fact['value'], ['123'])
                self.assertTrue(fact['source']['label'].endswith(prefix + '-after-lspatches.log'))

    def test_standalone_datapatch_pre_health_cannot_overwrite_post_dictionary(self):
        self.task(files={
            'datapatch-dictionary.log': 'INVALID_OBJECTS=0\nCOMPONENT=CATALOG|VALID\n',
            'datapatch-pre-health.log': 'DATABASE_UNIQUE_NAME=ORCL\nINVALID_OBJECTS=7\n',
        })
        fact = self.comparison(reports.build('p'), 'invalid_objects')['after']
        self.assertEqual(fact['value'], 0)
        self.assertTrue(fact['source']['label'].endswith('datapatch-dictionary.log'))

    def test_out_of_place_inventory_uses_the_stage_subject_not_the_comparison_home(self):
        cases = (
            ('oop_patch_clone', 'patch-clone-after-lspatches.log', 'patch-clone-active-lspatches.log', 'after', ['39034528']),
            ('oop_validate_clone', 'validate-clone-lspatches.log', 'validate-clone-active-lspatches.log', 'after', ['39034528']),
            ('oop_final_validate', 'final-lspatches.log', 'final-original-lspatches.log', 'after', ['39034528']),
            ('oop_switchback_precheck', 'switchback-precheck-clone-lspatches.log', 'switchback-precheck-original-lspatches.log', 'before', ['39034528']),
            ('oop_switch_back', 'switch-back-original-lspatches.log', 'switch-back-clone-lspatches.log', 'after', ['123']),
        )
        for stage, subject, comparison, side, expected in cases:
            # The custody order must never decide which Oracle home a metric describes.
            for reverse in (False, True):
                with self.subTest(stage=stage, reverse=reverse):
                    self.tasks.clear()
                    other = ['123'] if expected == ['39034528'] else ['39034528']
                    files = [(subject, expected[0] + ';workflow subject\n'), (comparison, other[0] + ';comparison home\n')]
                    self.task('oop-' + stage, stage, files=dict(reversed(files) if reverse else files))
                    fact = self.comparison(reports.build('p'), 'binary_inventory')[side]
                    self.assertEqual(fact['value'], expected)
                    self.assertTrue(fact['source']['label'].endswith(subject))

    def test_out_of_place_comparison_home_cannot_replace_a_missing_or_invalid_subject(self):
        for subject in (None, 'Inventory unavailable\n'):
            with self.subTest(subject=subject):
                self.tasks.clear()
                files = {'final-original-lspatches.log': '123;original home\n'}
                if subject is not None:
                    files['final-lspatches.log'] = subject
                self.task('final', 'oop_final_validate', files=files)
                fact = self.comparison(reports.build('p'), 'binary_inventory')['after']
                self.assertEqual((fact['status'], fact['value']), ('unknown', None))

    def test_out_of_place_evidence_for_another_clone_cannot_establish_completion(self):
        self.plan.update(state='succeeded', target={**self.target, 'clone_home': '/u02/reviewed-clone'})
        self.target['clone_home'] = '/u03/different-clone'
        self.task('final', 'oop_final_validate', files={'final-lspatches.log': '39034528;RU\n'})
        report = reports.build('p')
        self.assertFalse(report['completion_verified'])
        self.assertEqual(report['rollback']['status'], 'not_available')
        self.assertEqual(self.comparison(report, 'binary_inventory')['after']['status'], 'unknown')

    def test_successful_rollback_inventory_can_be_empty_without_retaining_applied_patches(self):
        for output in ('', 'OPatch succeeded.\n', 'There are no Interim patches installed in this Oracle Home.\nOPatch succeeded.\n'):
            with self.subTest(output=output):
                self.tasks.clear()
                self.plan.update(state='succeeded', intent='patch_rollback')
                self.task('004-rollback-rac1', 'rac_opatch_rollback', files={
                    'rollback-after-lspatches.log': output,
                    'rollback-before-lspatches.log': '39034528;applied RU\n',
                })
                self.task('009-final-local', 'rac_rollback_final_validate', files={
                    'rollback-cluster-final-lspatches.log': output,
                })
                report = reports.build('p')
                fact = self.comparison(report, 'binary_inventory')['after']
                self.assertTrue(report['completion_verified'])
                self.assertEqual((fact['status'], fact['value']), ('verified', []))
                self.assertEqual(fact['source']['task_id'], '009-final-local')

    def test_missing_or_unrecognizable_after_inventory_never_becomes_empty_success(self):
        self.task('004-apply-rac1', 'rac_opatch_apply', files={
            'apply-before-lspatches.log': '123;baseline\n',
        })
        self.assertEqual(self.comparison(reports.build('p'), 'binary_inventory')['after']['status'], 'unknown')
        self.task('005-validate-rac1', 'rac_node_validate', files={'node-validate-lspatches.log': '39034528;applied RU\n'})
        self.task('009-final-local', 'cluster_final_validate', files={'cluster-final-lspatches.log': 'Inventory unavailable\n'})
        fact = self.comparison(reports.build('p'), 'binary_inventory')['after']
        self.assertEqual((fact['status'], fact['value']), ('unknown', None))
        self.assertEqual(fact['source']['task_id'], '009-final-local')

    def test_json_observation_side_must_match_the_stage(self):
        self.task('005-final-local', 'final_validate', files={
            'report-after.json': {'target': self.target, 'observations': {'invalid_objects': 0}},
            'report-before.json': {'target': self.target, 'observations': {'invalid_objects': 7}},
        })
        fact = self.comparison(reports.build('p'), 'invalid_objects')['after']
        self.assertEqual(fact['value'], 0)
        self.assertTrue(fact['source']['label'].endswith('report-after.json'))

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

    def test_native_prechecks_populate_only_the_before_column(self):
        for stage in ('rac_precheck', 'rac_rollback_precheck', 'grid_precheck', 'grid_rollback_precheck',
                      'opatchauto_precheck', 'opatchauto_rollback_precheck', 'ojvm_precheck',
                      'ojvm_rollback_precheck', 'oop_precheck', 'oop_switchback_precheck'):
            with self.subTest(stage=stage):
                self.tasks.clear()
                name = 'switchback-precheck-clone-lspatches.log' if stage == 'oop_switchback_precheck' else 'precheck-lspatches.log'
                self.task('before-' + stage, stage, files={name: '123;baseline\n'})
                row = self.comparison(reports.build('p'), 'binary_inventory')
                self.assertEqual(row['before']['value'], ['123'])
                self.assertEqual(row['after']['status'], 'unknown')

    def test_stage_and_grid_home_mismatch_cannot_establish_completion(self):
        self.plan['state'] = 'succeeded'
        self.task('final', 'grid_cluster_final_validate')
        self.tasks[0]['stage'] = 'final_validate'
        self.assertFalse(reports.build('p')['completion_verified'])
        self.tasks[0]['stage'] = 'grid_cluster_final_validate'
        self.plan['target'] = {'grid_home': '/other-grid'}
        self.assertFalse(reports.build('p')['completion_verified'])

    def test_all_native_adapter_final_stages_report_completion_without_inventing_metrics(self):
        self.plan['state'] = 'succeeded'
        for stage in ('cluster_final_validate', 'rac_rollback_final_validate',
                      'grid_cluster_final_validate', 'grid_rollback_cluster_final_validate',
                      'opatchauto_cluster_final_validate', 'opatchauto_rollback_cluster_final_validate',
                      'ojvm_final_validate', 'ojvm_rollback_final_validate',
                      'oop_final_validate', 'oop_switchback_final_validate'):
            with self.subTest(stage=stage):
                self.tasks.clear()
                self.plan['intent'] = 'patch_rollback' if 'rollback' in stage or 'switchback' in stage else 'patch_apply'
                self.task('final-' + stage, stage)
                report = reports.build('p')
                self.assertTrue(report['completion_verified'])
                self.assertTrue(all(row['after']['status'] == 'unknown' for row in report['comparison']))
                self.assertFalse(report['rollback']['approved'])
                self.assertEqual(report['rollback']['status'], 'requires_native_validation'
                                 if self.plan['intent'] == 'patch_apply' else 'not_available')
                self.tasks[0]['evidence_verified'] = False
                self.assertFalse(reports.build('p')['completion_verified'])

    def test_safe_source_read_rejects_symlinks_and_hardlinks(self):
        path = self.root / 'file'; digest = write(path, 'data')
        link = self.root / 'symlink'; link.symlink_to(path)
        with self.assertRaises(OSError): reports._checked(link, digest)
        hard = self.root / 'hardlink'; hard.hardlink_to(path)
        with self.assertRaises(ValueError): reports._checked(path, digest)

    def test_safe_source_read_rejects_fifo_oversize_and_path_replacement(self):
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaises(ValueError): reports.evidence.read_regular_bytes(fifo)
        path = self.root / 'file'; path.write_bytes(b'data')
        with self.assertRaises(ValueError): reports.evidence.read_regular_bytes(path, maximum=3)
        original = reports.evidence.os.fstat
        calls = 0
        def replace_after_capture(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = self.root / 'replacement'; replacement.write_bytes(b'data')
                replacement.replace(path)
            return original(fd)
        with patch.object(reports.evidence.os, 'fstat', side_effect=replace_after_capture):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                reports.evidence.read_regular_bytes(path)

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
