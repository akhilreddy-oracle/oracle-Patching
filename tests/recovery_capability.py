#!/usr/bin/env python3
"""Recovery admission regressions: cached discovery never authorizes downtime."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from runtime_fixture import runtime_receipt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import evidence
import recoveryctl


class RecoveryCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opu-recovery-capability-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "hosts"))
        self.enterContext(patch.object(recoveryctl, "LIVE_DIR", self.root / "live"))
        self.enterContext(patch.object(recoveryctl, "RECOVERY_DIR", self.root / "fixtures"))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.snapshot = {
            "schema_version": "1.0", "collector": {"name": "oracle.topology.discover"},
            "collected_at": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cluster": {"status": "unavailable", "nodes": [], "grid_home": None},
            "oracle_homes": [{"path": "/u01/db", "owner": "oracle"}],
            "databases": [{"db_unique_name": "ORCL", "oracle_home": "/u01/db", "runtime": {
                "status": "complete", "instance": "ORCL", "database_role": "PRIMARY",
                "open_mode": "READ WRITE", "instance_state": "OPEN", "log_mode": "NOARCHIVELOG", "cdb": "NO"}}],
        }
        self.policy = {"schema_version": "1.0", "maximum_snapshot_age_seconds": 1800,
                       "require_xml_inventory": True, "database": {}, "recovery": {"require_backup": True}}

    def evaluate(self, snapshot=None, **kwargs):
        return recoveryctl._target_capability(self.snapshot if snapshot is None else snapshot, "ORCL", 1800, now=self.now, **kwargs)

    def test_supported_discovery_only_permits_creation_for_native_analysis(self):
        result = self.evaluate()
        self.assertTrue(result["can_create"])
        self.assertEqual(result["status"], "needs_native_analysis")
        spfile = next(row for row in result["requirements"] if row["id"] == "spfile")
        self.assertEqual((spfile["status"], spfile["stage"]), ("unknown", "native_analysis"))
        self.assertIn("approval remains blocked", spfile["next_action"])
        self.snapshot["databases"][0]["runtime"]["spfile"] = "/unverified/claim"
        self.assertEqual(next(row for row in self.evaluate()["requirements"] if row["id"] == "spfile")["status"], "unknown")

    def test_archivelog_standby_and_unknown_modes_fail_closed(self):
        for field, value in (("log_mode", "ARCHIVELOG"), ("log_mode", None), ("database_role", "PHYSICAL STANDBY"),
                             ("open_mode", "MOUNTED"), ("status", "partial"), ("cdb", "YES"), ("cdb", None)):
            with self.subTest(field=field, value=value):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["databases"][0]["runtime"][field] = value
                result = self.evaluate(snapshot)
                self.assertFalse(result["can_create"])
                finding = next(row for row in result["blockers"] if row["id"] == field)
                self.assertEqual(finding["observed"], value or "Unknown")
                self.assertTrue(finding["required"])
                self.assertTrue(finding["next_action"])

    def test_cluster_unknown_nodes_and_grid_home_cannot_be_called_standalone(self):
        for cluster in ({"status": "detected", "nodes": ["n1", "n2"]}, {"status": "unavailable"},
                        {"status": "unavailable", "nodes": [], "grid_home": "/u01/grid"}, {"status": "unknown", "nodes": []},
                        {"status": [], "nodes": []}, {"status": "unavailable", "nodes": [], "grid_home": []}):
            with self.subTest(cluster=cluster):
                self.assertFalse(self.evaluate({**self.snapshot, "cluster": cluster})["can_create"])

    def test_expired_future_window_and_subsecond_boundary_are_blocked(self):
        for delta in (-1801, 1):
            snapshot = {**self.snapshot, "collected_at": (self.now + timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            self.assertFalse(self.evaluate(snapshot)["can_create"])
        self.assertFalse(self.evaluate(window_start=(self.now + timedelta(seconds=1801)).strftime("%Y-%m-%dT%H:%M:%SZ"))["can_create"])
        result = recoveryctl._target_capability(self.snapshot, "ORCL", 1800, now=self.now + timedelta(seconds=1800, microseconds=1))
        self.assertFalse(result["can_create"])

    def test_ambiguous_database_or_owner_and_malformed_nested_evidence_are_unknown(self):
        duplicate = copy.deepcopy(self.snapshot)
        duplicate["databases"] *= 2
        self.assertFalse(self.evaluate(duplicate)["can_create"])
        duplicate = copy.deepcopy(self.snapshot)
        duplicate["oracle_homes"] *= 2
        self.assertFalse(self.evaluate(duplicate)["can_create"])
        for field in ("cluster", "databases", "oracle_homes", "collector"):
            for malformed in (None, "invalid", 42, {"unexpected": []}):
                with self.subTest(field=field, malformed=malformed):
                    self.assertFalse(self.evaluate({**self.snapshot, field: malformed})["can_create"])

    def test_host_scoped_read_does_not_create_directories_or_follow_snapshot_symlink(self):
        result = recoveryctl.target_capabilities("source")
        self.assertEqual(result["target_capabilities"][0]["status"], "unknown")
        self.assertFalse((self.root / "hosts").exists())
        evidence.write_evidence("source", "snapshot", self.snapshot)
        evidence.write_evidence("source", "policy", self.policy)
        self.assertTrue(recoveryctl.target_capabilities("source")["target_capabilities"][0]["can_create"])
        path = evidence.evidence_path("source", "snapshot")
        other = self.root / "other.json"
        path.rename(other)
        path.symlink_to(other)
        self.assertFalse(recoveryctl.target_capabilities("source")["target_capabilities"][0]["can_create"])
        self.assertFalse(recoveryctl.target_capabilities("target")["target_capabilities"][0]["can_create"])

    def test_create_rechecks_exact_saved_snapshot_after_ui_capability_was_eligible(self):
        evidence.write_evidence("source", "snapshot", self.snapshot)
        evidence.write_evidence("source", "policy", self.policy)
        self.assertTrue(recoveryctl.target_capabilities("source")["target_capabilities"][0]["can_create"])
        host = {"id": "source", "ssh_alias": "source", "remote_root": "/opt/opu", "sudo": True}
        with patch.object(recoveryctl, "_configured_host", return_value=host), \
             patch.object(recoveryctl.tools_sync, "ensure_tools", side_effect=runtime_receipt) as sync, \
             patch.object(recoveryctl.remote, "run_remote_raw") as remote:
            for log_mode in ("ARCHIVELOG", None):
                self.snapshot["databases"][0]["runtime"]["log_mode"] = log_mode
                evidence.write_evidence("source", "snapshot", self.snapshot)
                with self.assertRaisesRegex(recoveryctl.RecoveryError, "Log mode: observed .*; required NOARCHIVELOG"):
                    recoveryctl.create_live("request1", "requester", host=host, host_id="source", database="ORCL", backup_parent="/backups",
                                            window_start=self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                            window_end=(self.now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            sync.assert_not_called()
            remote.assert_not_called()
            self.assertFalse((self.root / "live").exists())

    def test_unavailable_installation_still_discloses_support_and_restore_scope(self):
        with patch.object(recoveryctl, "TOOL", self.root / "missing-tool"), \
             patch.object(recoveryctl.production, "require_live_mutation_allowed"):
            result = recoveryctl.capability()
        self.assertFalse(result["live_available"])
        self.assertEqual(result["supported_adapter"], recoveryctl.SUPPORTED_ADAPTER)
        self.assertIn("does not restore a separate database", result["restore_validation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
