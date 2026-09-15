#!/usr/bin/env python3
"""Complete shared TEST_MODE demo and explicit native command fixtures."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'webapp'))
import evidence_reports
import planctl
import testmode_fixtures


class SharedStandaloneTests(unittest.TestCase):
    def test_complete_demo_retains_native_inventory_recompile_and_report_custody(self):
        with tempfile.TemporaryDirectory(prefix='opu-shared-demo-') as temporary:
            base = Path(temporary)
            env = {'OPU_TEST_MODE': '1', 'OPU_TEST_KERNEL_NAME': 'Linux', 'OPU_TEST_KERNEL_RELEASE': '5.15.0-test',
                   'OPU_TEST_ARCHITECTURE': 'x86_64', 'OPU_FS_ROOT': str(ROOT / 'tests/fixtures/oracle-linux-8'),
                   'OPU_STATE_DIR': str(base / 'agent'), 'OPU_PRODUCTION_MODE': '0', 'OPU_TARGET_LOCK_DIR': str(base / 'target-locks')}
            with patch.object(planctl, 'PLAN_STATE_DIR', base / 'plans'), patch.object(planctl, 'TESTMODE_DIR', base / 'fixtures'), \
                 patch.object(planctl, '_execute_live', side_effect=AssertionError('fixture attempted live execution')), \
                 patch.object(planctl.remote, 'run_remote_raw', side_effect=AssertionError('fixture attempted SSH')), patch.dict(os.environ, env):
                now = datetime.now(timezone.utc); fmt = '%Y-%m-%dT%H:%M:%SZ'
                planctl.create_testmode_demo('shared-demo', 'requester', (now - timedelta(minutes=1)).strftime(fmt), (now + timedelta(hours=1)).strftime(fmt))
                fixture = base / 'fixtures/shared-demo'
                fixture_env = {**os.environ, **testmode_fixtures.env_for(fixture), 'ORACLE_HOME': str(fixture / 'oracle/dbhome_1')}
                datapatch = fixture / 'oracle/dbhome_1/OPatch/datapatch'
                self.assertEqual(subprocess.run([str(datapatch), '-help'], env=fixture_env, capture_output=True).returncode, 0)
                self.assertFalse((fixture / 'datapatch.state').exists()); self.assertFalse((fixture / 'sqlpatch-action.state').exists())
                for flags in [('-noqi',), ('-verbose', '-force'), ('-verbose', '-apply', '39034528')]:
                    self.assertNotEqual(subprocess.run([str(datapatch), *flags], env=fixture_env, capture_output=True).returncode, 0)
                sqlplus = fixture / 'oracle/dbhome_1/bin/sqlplus'
                rejected = subprocess.run([str(sqlplus)], input='select arbitrary_fixture_query from dual;\n', text=True, capture_output=True, env=fixture_env)
                self.assertEqual(rejected.returncode, 64)
                planctl.approve('shared-demo', 'approver', 'FIXTURE-REPORT')
                planctl.authorize('shared-demo', 'operator'); planctl.dispatch('shared-demo', 'operator')
                outputs = []
                for stage in ['precheck', 'apply', 'validate', 'datapatch', 'final_validate']:
                    output = planctl.execute_next_task('shared-demo', 'operator')
                    self.assertEqual(output['stage'], stage); self.assertEqual(output['status'], 'succeeded')
                    outputs.append(output)
                self.assertEqual(planctl.status('shared-demo')['state'], 'succeeded')
                sql_names = {Path(item['path']).name for item in outputs[3]['artifacts']}
                self.assertTrue({'local-inventory-before.xml', 'datapatch-arguments.log', 'utlrp0.log', 'datapatch-dictionary.log'} <= sql_names)
                report = evidence_reports.build('shared-demo')
                self.assertTrue(report['completion_verified'])
                self.assertEqual(report['rollback']['status'], 'requires_native_validation')
                self.assertFalse(report['rollback']['approved'])
                by_metric = {row['metric']: row for row in report['comparison']}
                self.assertEqual(by_metric['sql_patch_status']['after']['value'], 'APPLY/SUCCESS')
                self.assertTrue(by_metric['listener_ready']['after']['value'])
                self.assertEqual(by_metric['invalid_objects']['after']['value'], 0)


if __name__ == '__main__': unittest.main()
