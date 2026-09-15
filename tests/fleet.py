#!/usr/bin/env python3
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'webapp'))
import evidence
import fleet


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(evidence, 'VAR_DIR', Path(self.temp.name)))
        self.now = 1800000000
        self.host = {'id':'h','label':'Host', 'environment':'test', 'desired_patch_baseline':'12345'}
        self.snapshot = {'collected_at': self.iso(self.now-60), 'oracle_homes': [
            {'path':'/oracle','version':'19','patches':['12345'],'opatch_inventory_xml_status':'collected'}],
            'databases':[{'db_unique_name':'ORCL','oracle_home':'/oracle','runtime':{
                'status':'complete','database_version':'19.31','backup_age_minutes':10,
                'latest_backup_completed_at':self.iso(self.now-660)}}]}
        self.policy={'maximum_snapshot_age_seconds':1800,'recovery':{'max_backup_age_minutes':60}}
        self.write()

    def iso(self, when): return dt.datetime.fromtimestamp(when,dt.timezone.utc).isoformat()
    def write(self):
        snapshot_path=evidence.write_evidence('h','snapshot',self.snapshot)
        policy_path=evidence.write_evidence('h','policy',self.policy)
        evidence.write_evidence('h','procedure_input',{'database_unique_name':'ORCL'})
        self.ready={'status':'ready_for_approval','valid_until':self.iso(self.now+900),
            'snapshot_evidence':[{'path':str(snapshot_path),'sha256':hashlib.sha256(snapshot_path.read_bytes()).hexdigest()}],
            'evidence':{'policy_sha256':hashlib.sha256(policy_path.read_bytes()).hexdigest()},'gates':[]}
        evidence.write_evidence('h','readiness',self.ready)
    def row(self): return fleet.build({'h':self.host},now=self.now)['databases'][0]

    def test_fresh_observations_are_not_restore_validation(self):
        row=self.row()
        self.assertEqual((row['baseline_status'],row['backup_status'],row['readiness']),('compliant','fresh','ready_for_approval'))
        self.assertIsNone(row['backup_restore_validated'])
    def test_stale_snapshot_never_green(self):
        self.snapshot['collected_at']=self.iso(self.now-4000); self.write()
        row=self.row()
        self.assertEqual(row['evidence_status'],'stale')
        self.assertEqual((row['readiness'],row['baseline_status'],row['backup_status']),('unknown',)*3)
    def test_changed_snapshot_or_policy_invalidates_readiness(self):
        self.snapshot['databases'][0]['runtime']['invalid_objects']=9
        evidence.write_evidence('h','snapshot',self.snapshot)
        self.assertEqual(self.row()['readiness'],'unknown')
        self.write()
        evidence.write_evidence('h','policy',{**self.policy,'require_xml_inventory':False})
        self.assertEqual(self.row()['readiness'],'unknown')
    def test_no_cross_database_readiness(self):
        self.snapshot['databases'][0]['db_unique_name']='OTHER'; self.write()
        self.assertEqual(self.row()['readiness'],'unknown')
    def test_missing_backups_and_expired_readiness(self):
        self.snapshot['databases'][0]['runtime']['latest_backup_completed_at']=None; self.write()
        self.assertEqual(self.row()['backup_status'],'missing')
        self.ready['valid_until']=self.iso(self.now-1); evidence.write_evidence('h','readiness',self.ready)
        self.assertEqual(self.row()['readiness'],'unknown')
    def test_missing_and_corrupt_evidence_remain_visible(self):
        evidence.evidence_path('h','snapshot').write_text('{bad')
        row=self.row()
        self.assertIsNone(row['database']); self.assertEqual(row['evidence_status'],'unknown')
    def test_future_timestamp_and_invalid_inventory_are_unknown(self):
        self.snapshot['collected_at']=self.iso(self.now+10); self.write()
        self.assertEqual(self.row()['evidence_status'],'unknown')
        self.snapshot['collected_at']=self.iso(self.now-10)
        self.snapshot['oracle_homes'][0]['opatch_inventory_xml_status']='failed'; self.write()
        self.assertEqual(self.row()['baseline_status'],'unknown')

    def test_malformed_nested_evidence_does_not_hide_the_fleet(self):
        self.ready['evidence']=[]; evidence.write_evidence('h','readiness',self.ready)
        self.assertEqual(self.row()['readiness'],'unknown')
        self.policy['maximum_snapshot_age_seconds']=10**400; self.write()
        self.assertEqual(self.row()['evidence_status'],'fresh')
        self.snapshot['databases'].append({'db_unique_name':{}}); self.write()
        rows=fleet.build({'h':self.host},now=self.now)['databases']
        self.assertEqual(len(rows),2)
        self.assertIn(None,[row['database'] for row in rows])

    def test_blocked_without_gate_detail_still_needs_attention(self):
        self.ready['status']='blocked'; evidence.write_evidence('h','readiness',self.ready)
        self.assertEqual(self.row()['attention'],'blocked')
        self.ready['status']=[]; self.ready['gates']=[{'status':[]}]
        evidence.write_evidence('h','readiness',self.ready)
        self.assertEqual(self.row()['readiness'],'unknown')

if __name__=='__main__': unittest.main()
