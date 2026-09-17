#!/usr/bin/env python3
"""Exact-run discovery receipts; every Oracle and SSH boundary is mocked."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import evidence
import live_inventory as inventory
import pipeline_runner as runner
import pipeline_steps as pipeline
import remote


class LiveInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="opu-live-inventory-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "hosts"))
        self.enterContext(patch.object(runner, "RUNS_DIR", self.root / "runs"))
        self.enterContext(patch.object(runner, "RUNS", {}))
        self.enterContext(patch.object(runner, "_ACTIVE_KEYS", {}))
        self.enterContext(patch.object(runner.notifications, "emit"))
        self.sync = self.enterContext(patch.object(pipeline.tools_sync, "ensure_host_tools"))
        self.ssh = self.enterContext(patch.object(pipeline.remote, "run_remote_json",
            side_effect=lambda *_args, **_kwargs: self.snapshot()))
        self.host = {"id": "targetdb", "ssh_alias": "never-connect", "remote_root": "/fixture",
                     "password": "HOST_SECRET"}

    def snapshot(self, patches=None):
        return {"schema_version": "1.0", "collector": {"name": "oracle.topology.discover", "version": "1"},
            "collected_at": datetime.now(timezone.utc).isoformat(), "host": {"name": "target"},
            "oracle_homes": [{"path": "/u01/dbhome", "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51",
                "patch_inventory_source": "opatch_lsinventory_xml", "opatch_inventory_xml_status": "collected",
                "opatch_inventory_xml_sha256": "a" * 64,
                "patches": ["29517242", "29585399"] if patches is None else patches,
                "private_config": "HOME_SECRET"}],
            "databases": [{"db_unique_name": "ORCL", "oracle_home": "/u01/dbhome",
                "runtime": {"status": "complete", "database_version": "19.0.0.0.0", "open_mode": "READ WRITE",
                    "sqlpatch_non_success": 0, "token": "RUNTIME_SECRET", "raw_sql": "SQL_SECRET"}}],
            "raw_logs": "LOG_SECRET"}

    def build(self, payload=None, **overrides):
        now = datetime.now(timezone.utc)
        args = dict(host_id="targetdb", host=self.host, run_id="a" * 12,
            started_at=(now - timedelta(seconds=1)).isoformat(), completed_at=now.isoformat(),
            node_snapshots=[("targetdb", self.snapshot() if payload is None else payload)])
        args.update(overrides)
        return inventory.build_receipt(**args)

    def run_discovery(self):
        record = runner.start_run("pipeline", "host:targetdb:pipeline", lambda _record:
            pipeline.step_discovery("targetdb", self.host, {"inventory_receipt": True}))
        deadline = time.monotonic() + 5
        while record.status in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertNotIn(record.status, {"queued", "running"})
        return record

    def test_discovery_receipt_is_bound_to_its_native_run_and_configuration(self):
        record = self.run_discovery()
        self.assertEqual(record.status, "succeeded", record.error)
        result = inventory.verify_receipt(record.result, host_id="targetdb", run_id=record.run_id,
            configuration_sha256=inventory.configuration_digest(self.host), not_before=record.started_at)
        self.assertEqual(result["source"], "live_discovery")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["nodes"][0]["oracle_homes"][0]["patches"], ["29517242", "29585399"])
        self.ssh.assert_called_once_with("never-connect", ["/fixture/bin/opu-topology-discover", "--pretty"],
            timeout=pipeline.DISCOVERY_TIMEOUT_SECONDS, sudo=False)
        for secret in ("HOST_SECRET", "HOME_SECRET", "RUNTIME_SECRET", "SQL_SECRET", "LOG_SECRET"):
            self.assertNotIn(secret, json.dumps(result))

    def test_plain_discovery_keeps_original_primary_payload_contract(self):
        expected = self.snapshot()
        self.ssh.side_effect = None
        self.ssh.return_value = expected
        result = pipeline.step_discovery("targetdb", self.host, {})
        self.assertIs(result, expected)
        self.assertEqual(evidence.read_evidence("targetdb", "snapshot"), expected)

    def test_receipt_cannot_be_requested_without_native_run_tracking(self):
        with self.assertRaises(inventory.InventoryError):
            pipeline.step_discovery("targetdb", self.host, {"inventory_receipt": True})
        self.ssh.assert_not_called()
        self.sync.assert_not_called()

    def test_rac_receipt_captures_each_node_and_survives_later_cache_overwrite(self):
        self.host["nodes"] = [{"name": "rac1", "ssh_alias": "node-one"},
                              {"name": "rac2", "ssh_alias": "node-two"}]
        self.ssh.side_effect = lambda alias, *_args, **_kwargs: self.snapshot(
            ["39034528"] if alias == "node-two" else ["29517242"])
        record = self.run_discovery()
        self.assertEqual(record.status, "succeeded", record.error)
        original = copy.deepcopy(record.result)
        for name in ("snapshot", "snapshot_rac1", "snapshot_rac2"):
            evidence.write_evidence("targetdb", name, self.snapshot(["99999999"]))
        self.assertEqual(record.result, original)
        result = inventory.verify_receipt(record.result, host_id="targetdb", run_id=record.run_id)
        self.assertEqual([node["node"] for node in result["nodes"]], ["rac1", "rac2"])
        self.assertEqual([node["oracle_homes"][0]["patches"] for node in result["nodes"]],
                         [["29517242"], ["39034528"]])
        self.assertEqual(result["coverage"]["configured_nodes"], 2)
        self.assertNotIn("99999999", inventory.format_receipt(result))

    def test_failed_rac_node_never_returns_old_or_partially_written_cache_as_receipt(self):
        self.host["nodes"] = [{"name": "rac1", "ssh_alias": "node-one"},
                              {"name": "rac2", "ssh_alias": "node-two"}]
        evidence.write_evidence("targetdb", "snapshot", self.snapshot(["99999999"]))
        self.ssh.side_effect = [self.snapshot(["29517242"]), remote.RemoteError("ssh_failed", "fixture unavailable")]
        record = self.run_discovery()
        self.assertEqual(record.status, "failed")
        self.assertIsNone(record.result)
        self.assertIn("rac2", record.error["message"])
        self.assertTrue(evidence.read_evidence("targetdb", "snapshot_rac1"))
        with self.assertRaises(inventory.InventoryError):
            inventory.verify_receipt(record.result, host_id="targetdb", run_id=record.run_id)

    def test_receipt_rejects_missing_duplicate_or_mismatched_configured_nodes(self):
        self.host["nodes"] = [{"name": "rac1", "ssh_alias": "one"}, {"name": "rac2", "ssh_alias": "two"}]
        for entries in ([("rac1", self.snapshot())], [("rac1", self.snapshot()), ("rac1", self.snapshot())],
                        [("rac1", self.snapshot()), ("other", self.snapshot())]):
            with self.subTest(entries=[name for name, _ in entries]), self.assertRaises(inventory.InventoryError):
                self.build(node_snapshots=entries)

    def test_wrong_run_host_configuration_and_old_operation_are_rejected(self):
        receipt = self.build()
        cases = ({"host_id": "sourcedb"}, {"run_id": "b" * 12}, {"configuration_sha256": "b" * 64},
                 {"not_before": datetime.now(timezone.utc).isoformat()})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(inventory.InventoryError):
                inventory.verify_receipt(receipt, **{"host_id": "targetdb", "run_id": "a" * 12, **changes})
        receipt["nodes"][0]["oracle_homes"][0]["patches"] = ["99999999"]
        with self.assertRaisesRegex(inventory.InventoryError, "integrity"):
            inventory.verify_receipt(receipt, host_id="targetdb", run_id="a" * 12)

    def test_old_future_missing_or_untrusted_collection_does_not_supply_current_values(self):
        now = datetime.now(timezone.utc)
        for value in ((now - timedelta(minutes=10)).isoformat(), (now + timedelta(minutes=10)).isoformat(),
                      None, "invalid", "2026-09-17T01:00:00"):
            with self.subTest(value=value):
                payload = self.snapshot(["39034528"])
                payload["collected_at"] = value
                receipt = self.build(payload)
                node = receipt["nodes"][0]
                self.assertEqual(receipt["status"], "partial")
                self.assertEqual(node["collection_status"], "unknown")
                self.assertIsNone(node["oracle_homes"][0]["patches"])
                self.assertIsNone(node["oracle_homes"][0]["version"])
                self.assertEqual(node["databases"][0]["runtime"], {})
                self.assertNotIn("39034528", inventory.format_receipt(receipt))
        payload = self.snapshot()
        payload.pop("collector")
        self.assertEqual(self.build(payload)["nodes"][0]["collection_status"], "unknown")

    def test_partial_xml_inventory_is_unknown_and_empty_verified_inventory_is_distinct(self):
        for changes in ({"opatch_inventory_xml_status": "failed"}, {"patch_inventory_source": "text"},
                        {"opatch_inventory_xml_sha256": None}, {"patches": ["39034528", "bogus"]},
                        {"patches": None}):
            with self.subTest(changes=changes):
                payload = self.snapshot()
                payload["oracle_homes"][0].update(changes)
                receipt = self.build(payload)
                self.assertEqual(receipt["status"], "partial")
                self.assertIsNone(receipt["nodes"][0]["oracle_homes"][0]["patches"])
                self.assertIn("unavailable", inventory.format_receipt(receipt))
        receipt = self.build(self.snapshot([]))
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["nodes"][0]["oracle_homes"][0]["patches"], [])

    def test_sql_counter_is_not_per_patch_status_and_fields_are_bounded(self):
        payload = self.snapshot([str(10000000 + index) for index in range(120)])
        payload["oracle_homes"] *= 10
        payload["databases"][0]["runtime"].update(invalid_objects=float("nan"))
        receipt = self.build(payload)
        node = receipt["nodes"][0]
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(len(node["oracle_homes"]), inventory.LIMITS["homes_per_node"])
        self.assertEqual(len(node["oracle_homes"][0]["patches"]), inventory.LIMITS["patches_per_home"])
        self.assertTrue(node["oracle_homes"][0]["patches_truncated"])
        self.assertEqual(node["databases"][0]["sql_patch_evidence"],
            {"scope": "aggregate_only", "per_patch_status": "not_collected", "non_success_count": 0})
        self.assertNotIn("invalid_objects", node["databases"][0]["runtime"])
        text = inventory.format_receipt(receipt)
        self.assertIn("does not identify an RU", text)
        self.assertIn("Per-patch SQL application status was not collected", text)
        json.dumps(receipt, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
