"""Filesystem recovery binding tests; all Oracle/SSH boundaries are mocked."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from runtime_fixture import host_runtime_receipts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'webapp'))
import evidence
import pipeline_steps as pipeline
import recoveryctl
import remote


class RecoveryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='opu recovery pipeline ')
        self.addCleanup(self.tmp.cleanup)
        self.var = patch.object(evidence, 'VAR_DIR', Path(self.tmp.name) / 'hosts')
        self.var.start(); self.addCleanup(self.var.stop)
        self.host = {'id':'source', 'ssh_alias':'source-alias', 'remote_root':'/opt/opu', 'sudo':True}
        self.target = {'database_unique_name':'ORCL', 'oracle_home':'/u01/db', 'owner':'oracle'}
        self.snapshot = {'host':{'name':'source'}, 'cluster':{'status':'unavailable'}, 'databases':[{'db_unique_name':'ORCL','oracle_home':'/u01/db'}]}
        self.path = evidence.write_evidence('source','snapshot',self.snapshot)
        self.native = {'status':'passed','target':self.target,'source_snapshot':{'path':str(self.path),'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}}
        self.policy = {'schema_version':'1.0','maximum_snapshot_age_seconds':1800,'require_xml_inventory':True,'recovery':{'require_backup':True,'storage_mode':'filesystem','minimum_filesystem_free_bytes':0},'database':{}}
        self.request = {'preparation_policy':self.policy,'mode':'live','host_id':'source','state':'completed','target':self.target,'result':{'backup_root':'/backup/request'}}
        self.mocks = []
        for target, kwargs in [
            ('recoveryctl.selection_status', {'return_value':self.request}),
            ('pipeline_steps.tools_sync.ensure_host_tools', {'side_effect':host_runtime_receipts}),
            ('pipeline_steps.remote.push_file', {}),
            ('pipeline_steps.remote.run_remote_raw', {'side_effect':self.native_response}),
        ]:
            p = patch(target, **kwargs); mock = p.start(); self.addCleanup(p.stop); self.mocks.append(mock)

    def native_response(self, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.native), stderr='')

    def collect(self):
        return pipeline.step_recovery_collect('source', self.host, {'request_id':'request'})

    def test_exact_mirrored_snapshot_and_unchanged_native_document(self):
        self.assertEqual(self.collect(), self.native)
        self.mocks[2].assert_called_once_with('source-alias', str(self.path), self.path.read_bytes(), sudo=True)
        argv = self.mocks[3].call_args.args[1]
        self.assertEqual(argv[argv.index('--snapshot')+1], str(self.path))
        self.assertIn('opu recovery pipeline ', argv[argv.index('--snapshot')+1])
        self.assertEqual(evidence.read_evidence('source','recovery'), self.native)
        self.assertEqual(evidence.read_evidence('source','recovery_selection')['request_id'], 'request')
        self.assertNotIn('TEST_MODE', repr(self.mocks[3].call_args))

    def test_reject_demo_other_host_or_incomplete_request_before_ssh(self):
        for field, value in [('mode','test_mode'),('host_id','target'),('state','running')]:
            with self.subTest(field=field):
                old = self.request[field]; self.request[field]=value
                with self.assertRaises(remote.RemoteError): self.collect()
                self.request[field]=old
        self.mocks[2].assert_not_called(); self.mocks[3].assert_not_called()

    def test_wrong_requested_database_rejected(self):
        self.request['target'] = {**self.target, 'database_unique_name':'OTHER'}
        with self.assertRaises(remote.RemoteError): self.collect()
        self.mocks[3].assert_not_called()

    def test_native_failure_invalidates_cached_authority(self):
        for name in ('recovery','readiness','recovery_selection'): evidence.write_evidence('source', name, {'old':True})
        self.mocks[3].side_effect=None
        self.mocks[3].return_value=SimpleNamespace(returncode=65,stdout='',stderr='backup validation failed')
        with self.assertRaises(remote.RemoteError): self.collect()
        for name in ('recovery','readiness','recovery_selection'): self.assertIsNone(evidence.read_evidence('source',name))

    def test_wrong_native_snapshot_rejected_without_rebinding(self):
        self.native['source_snapshot']['path']='/remote/other-snapshot.json'
        with self.assertRaises(remote.RemoteError): self.collect()
        self.assertIsNone(evidence.read_evidence('source','recovery'))

    def test_wrong_native_target_rejected(self):
        self.native['target']={**self.target,'owner':'different'}
        with self.assertRaises(remote.RemoteError): self.collect()
        self.assertIsNone(evidence.read_evidence('source','recovery'))

    def test_snapshot_drift_during_validation_rejected(self):
        def response(*args, **kwargs):
            self.path.write_text(json.dumps({**self.snapshot,'new':True}))
            return self.native_response()
        self.mocks[3].side_effect=response
        with self.assertRaises(remote.RemoteError): self.collect()
        self.assertIsNone(evidence.read_evidence('source','recovery'))

    def test_failed_selection_invalidates_old_readiness_authority(self):
        for name in ('recovery', 'readiness', 'recovery_selection'):
            evidence.write_evidence('source', name, {'old':True})
        self.mocks[0].side_effect = recoveryctl.RecoveryError('Recovery needs a verified successful execution')
        with self.assertRaises(recoveryctl.RecoveryError): self.collect()
        for name in ('recovery', 'readiness', 'recovery_selection'):
            self.assertIsNone(evidence.read_evidence('source', name))
        self.mocks[3].assert_not_called()

    def test_selected_request_policy_is_explicit_and_does_not_replace_saved_policy(self):
        saved = {**self.policy, 'maximum_snapshot_age_seconds':900}
        evidence.write_evidence('source', 'policy', saved)
        self.collect()
        self.assertEqual(evidence.read_evidence('source', 'policy'), saved)
        self.assertEqual(evidence.read_evidence('source', 'recovery_selection')['policy'], self.policy)
        self.mocks[0].assert_called_once_with('request', host_id='source', host=self.host)

    def test_multinode_rejected(self):
        self.host['nodes']=[{'name':'one','ssh_alias':'one'},{'name':'two','ssh_alias':'two'}]
        with self.assertRaises(remote.RemoteError): self.collect()
        self.mocks[3].assert_not_called()

    def test_saved_policy_available_without_successful_readiness(self):
        policy={'schema_version':'1.0','recovery':{'require_backup':True,'storage_mode':'filesystem'}}
        evidence.write_evidence('source','policy',policy)
        row=next(r for r in pipeline.pipeline_state('source') if r['step']=='readiness-evaluate')
        self.assertEqual(row['input'],policy)
        self.assertFalse(row['done'])

    def test_chain_revalidates_selected_backup_before_readiness(self):
        evidence.write_evidence('source','recovery_selection',{'request_id':'request','host_id':'source'})
        procedure = {'artifact_sha256': 'a' * 64,
                     'oracle_references': [{'kind': 'patch_readme', 'identifier': 'README.html', 'sha256': 'b' * 64}]}
        evidence.write_evidence('source', 'artifact', {'artifact': {'sha256': 'a' * 64,
            'readme_files': [{'path': 'README.html', 'sha256': 'b' * 64}]}})
        policy={'recovery':{'require_backup':True,'storage_mode':'filesystem'}}
        order=[]
        functions=[('discovery',{}),('reconcile',{'status':'consistent'}),('artifact_inspect',{'artifact':{'status':'ready_for_catalog'}}),('procedure_validate',{'status':'ready_for_planning'}),('compatibility_collect',{'status':'passed'}),('compatibility_reconcile',{'status':'passed'}),('recovery_collect',{'status':'passed'}),('readiness_evaluate',{'status':'ready_for_approval'})]
        handles=[]
        try:
            for name,result in functions:
                def fake(*args,_name=name,_result=result,**kwargs): order.append(_name); return _result
                h=patch.object(pipeline,'step_'+name,side_effect=fake);h.start();handles.append(h)
            result=pipeline.step_readiness_chain('source',self.host,{'policy':policy,'procedure':procedure,'artifact_dir':'/stage/patch'})
        finally:
            for h in handles: h.stop()
        self.assertEqual(result['status'],'ready_for_approval')
        self.assertEqual(order[-2:],['recovery_collect','readiness_evaluate'])


if __name__ == '__main__': unittest.main()
