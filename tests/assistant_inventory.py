#!/usr/bin/env python3
"""Saved inventory supplied to the model stays scoped, dated and bounded."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import assistant_tools as tools


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.hosts = {"targetdb": {"label": "Target database", "password": "HOST_SECRET"}}
        self.snapshot = {"collected_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
            "oracle_homes": [{"path": "/u01/dbhome", "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51",
                "patch_inventory_source": "opatch_lsinventory_xml", "opatch_inventory_xml_status": "collected",
                "opatch_inventory_xml_sha256": "a" * 64, "patches": ["29517242", "29585399"],
                "private_config": "HOME_SECRET"}],
            "databases": [{"db_unique_name": "ORCL", "oracle_home": "/u01/dbhome",
                "runtime": {"status": "complete", "database_version": "19.0.0.0.0", "open_mode": "READ WRITE",
                    "sqlpatch_non_success": 0, "token": "RUNTIME_SECRET", "raw_sql": "RAW_SECRET"}}],
            "raw_logs": "LOG_SECRET"}
        self.records = {"snapshot": self.snapshot, "policy": {"maximum_snapshot_age_seconds": 1800},
                        "procedure_input": {"patch_id": "39034528"}}
        self.read = self.enterContext(patch.object(tools.evidence, "read_evidence",
            side_effect=lambda host_id, name: self.records.get(name)))

    def inventory(self):
        return tools.read("inspect_host", {"host_id": "targetdb"}, self.hosts)["inventory"]

    def test_installed_inventory_is_distinct_from_requested_patch_and_base_version(self):
        result = tools.read("inspect_host", {"host_id": "targetdb"}, self.hosts)
        node = result["inventory"]["nodes"][0]
        home = node["oracle_homes"][0]
        self.assertEqual(home["patches"], ["29517242", "29585399"])
        self.assertNotIn(result["procedure_input"]["patch_id"], home["patches"])
        self.assertEqual(home["version"], "19.0.0.0.0")
        self.assertEqual(home["opatch_version"], "12.2.0.1.51")
        self.assertEqual(home["binary_inventory_status"], "collected")
        self.assertEqual(home["opatch_inventory_xml_sha256"], "a" * 64)
        self.assertEqual(node["collected_at"], self.snapshot["collected_at"])
        self.assertEqual(node["freshness"], "fresh")
        self.assertGreaterEqual(node["age_seconds"], 60)
        self.assertEqual(result["host_link"], "#/hosts/targetdb/discover")
        self.assertFalse(result["inventory"]["live_state_verified"])
        self.assertEqual(result["inventory"]["coverage"]["scope"], "primary_snapshot_only")
        self.assertEqual(result["inventory"]["coverage"]["completeness"], "not_asserted")

    def test_stale_missing_and_future_timestamps_do_not_claim_freshness(self):
        for stamp, expected in ((datetime.now(timezone.utc) - timedelta(hours=1), "stale"),
                                (datetime.now(timezone.utc) + timedelta(hours=1), "unknown"),
                                (None, "unknown"), ("2026-09-17T01:00:00", "unknown"), ("invalid", "unknown")):
            with self.subTest(stamp=stamp):
                self.snapshot["collected_at"] = stamp.isoformat() if isinstance(stamp, datetime) else stamp
                self.assertEqual(self.inventory()["nodes"][0]["freshness"], expected)
        now = datetime.now(timezone.utc)
        self.snapshot["collected_at"] = (now + timedelta(microseconds=100)).isoformat()
        result = tools._snapshot_inventory(self.snapshot, "snapshot", 1800, now)
        self.assertEqual(result["freshness"], "unknown")
        self.assertLess(result["age_seconds"], 0)

    def test_missing_or_invalid_inventory_remains_unknown_not_an_empty_installed_list(self):
        home = self.snapshot["oracle_homes"][0]
        for patches, status, source in ((None, "collected", "opatch_lsinventory_xml"),
                ([], "failed", "opatch_lsinventory_xml"), (["39034528", "bad"], "collected", "opatch_lsinventory_xml"),
                (["39034528"], "collected", "untrusted_text")):
            with self.subTest(patches=patches, status=status, source=source):
                home.update(patches=patches, opatch_inventory_xml_status=status, patch_inventory_source=source)
                result = self.inventory()["nodes"][0]["oracle_homes"][0]
                self.assertIsNone(result["patches"])
                self.assertEqual(result["binary_inventory_status"], "unknown")
        home.update(patches=[], opatch_inventory_xml_status="collected", patch_inventory_source="opatch_lsinventory_xml")
        self.assertEqual(self.inventory()["nodes"][0]["oracle_homes"][0]["patches"], [])

    def test_sql_counter_is_not_per_patch_confirmation_and_runtime_is_allowlisted(self):
        result = self.inventory()
        database = result["nodes"][0]["databases"][0]
        self.assertEqual(database["sql_patch_evidence"], {
            "scope": "aggregate_only", "per_patch_status": "not_collected", "non_success_count": 0})
        self.assertIn("do not establish", result["interpretation"])
        self.snapshot["databases"][0]["runtime"]["status"] = "incomplete"
        self.assertIsNone(self.inventory()["nodes"][0]["databases"][0]["sql_patch_evidence"]["non_success_count"])
        for name in ("inspect_host", "list_estate"):
            payload = json.dumps(tools.read(name, {"host_id": "targetdb"} if name == "inspect_host" else {}, self.hosts))
            for secret in ("HOST_SECRET", "HOME_SECRET", "RUNTIME_SECRET", "RAW_SECRET", "LOG_SECRET"):
                self.assertNotIn(secret, payload)

    def test_rac_nodes_keep_independent_home_inventories_and_observation_dates(self):
        first, second = copy.deepcopy(self.snapshot), copy.deepcopy(self.snapshot)
        second["collected_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        second["oracle_homes"][0]["patches"] = ["39034528"]
        self.records.update(snapshot_nodes={"nodes": [
            {"name": "rac1", "evidence": "snapshot_rac1", "ssh_alias": "SSH_SECRET"},
            {"name": "rac2", "evidence": "snapshot_rac2"}]}, snapshot_rac1=first, snapshot_rac2=second)
        result = self.inventory()
        self.assertEqual([node["node"] for node in result["nodes"]], ["rac1", "rac2"])
        self.assertEqual(result["nodes"][0]["oracle_homes"][0]["patches"], ["29517242", "29585399"])
        self.assertEqual(result["nodes"][1]["oracle_homes"][0]["patches"], ["39034528"])
        self.assertEqual([node["freshness"] for node in result["nodes"]], ["fresh", "stale"])
        self.assertEqual(result["coverage"]["indexed_nodes"], 2)
        self.assertEqual(result["coverage"]["scope"], "indexed_nodes")
        self.assertNotIn("SSH_SECRET", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 6000)

    def test_missing_invalid_and_excess_nodes_are_reported_without_reading_arbitrary_paths(self):
        self.records["snapshot_nodes"] = {"nodes": [
            {"name": "rac1", "evidence": "snapshot_rac1"}, {"evidence": "../../credentials"},
            {"evidence": "procedure_input"}, {"evidence": "snapshot_missing"}, {"evidence": "snapshot_extra"}]}
        self.records["snapshot_rac1"] = self.snapshot
        result = self.inventory()
        self.assertEqual(result["coverage"]["returned_nodes"], 1)
        self.assertTrue(result["coverage"]["nodes_truncated"])
        self.assertEqual(len(result["coverage"]["omissions"]), 3)
        read_names = [call.args[1] for call in self.read.call_args_list]
        self.assertNotIn("../../credentials", read_names)
        self.assertNotIn("snapshot_extra", read_names)

    def test_missing_snapshot_returns_explicit_gap_not_a_claim_of_no_patches(self):
        self.records.pop("snapshot")
        result = self.inventory()
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["coverage"]["omissions"], ["snapshot: saved snapshot unavailable"])

    def test_large_lists_and_malformed_fields_are_bounded_and_disclosed(self):
        home = self.snapshot["oracle_homes"][0]
        home["patches"] = [str(10000000 + i) for i in range(100)]
        self.snapshot["oracle_homes"] = [copy.deepcopy(home) for _ in range(10)]
        self.snapshot["databases"] *= 12
        node = self.inventory()["nodes"][0]
        self.assertEqual(len(node["oracle_homes"]), 6)
        self.assertEqual(len(node["databases"]), 8)
        self.assertTrue(node["coverage"]["homes_truncated"])
        self.assertTrue(node["coverage"]["databases_truncated"])
        self.assertEqual(len(node["oracle_homes"][0]["patches"]), 80)
        self.assertTrue(node["oracle_homes"][0]["patches_truncated"])
        self.snapshot.update(oracle_homes="malformed", databases="malformed")
        self.records["policy"]["maximum_snapshot_age_seconds"] = float("nan")
        result = self.inventory()
        self.assertEqual(result["maximum_snapshot_age_seconds"], 1800)
        self.assertEqual(len(result["nodes"][0]["coverage"]["omissions"]), 2)

    def test_non_finite_numbers_do_not_break_serialization_or_freshness(self):
        runtime = self.snapshot["databases"][0]["runtime"]
        runtime.update(invalid_objects=float("nan"), backup_age_minutes=float("inf"))
        self.records["policy"]["maximum_snapshot_age_seconds"] = 10 ** 1000
        result = self.inventory()
        self.assertEqual(result["maximum_snapshot_age_seconds"], 1800)
        self.assertNotIn("invalid_objects", result["nodes"][0]["databases"][0]["runtime"])
        json.dumps(result, allow_nan=False)

    def test_grounding_natural_host_names_have_boundaries_and_never_choose_a_default(self):
        hosts = {"targetdb": {}, "targetdb2": {}, "sourcedb": {}, "oracle-test-rac": {}}
        for content, matched in (("current patch in target db", ["targetdb"]),
                ("current patch in target database", ["targetdb"]), ("targetdatabase", ["targetdb"]),
                ("TARGETDB2 patch status", ["targetdb2"]), ("mytargetdb", []),
                ("targetdatabase2", []), ("mytargetdatabase", []),
                ("source db then targetdb", ["sourcedb", "targetdb"]),
                ("oracle test rac inventory", ["oracle-test-rac"]),
                ("show the database patch level", [])):
            with self.subTest(content=content):
                self.assertEqual(tools._named_hosts(content, hosts), matched)
        result = tools.grounding("show the database patch level", hosts)
        self.assertEqual(result["inspected_hosts"], [])
        self.assertEqual(len(result["estate"]["hosts"]), 4)
        self.assertEqual(tools._named_hosts("target database", {"targetdb": {}, "targetdatabase": {}}),
                         ["targetdatabase"], "An exact configured host must take priority over a convenience alias")

    def test_grounding_preloads_named_inventory_before_estate_and_has_no_native_writes(self):
        with patch.object(tools, "binding", side_effect=AssertionError("No action binding allowed")):
            result = tools.grounding("what is the patch version in target db", self.hosts)
        self.assertEqual(result["source"], "saved_evidence")
        self.assertFalse(result["live_state_verified"])
        self.assertEqual(result["inspected_hosts"][0]["host_id"], "targetdb")
        self.assertEqual(result["inspected_hosts"][0]["inventory"]["nodes"][0]["oracle_homes"][0]["patches"],
                         ["29517242", "29585399"])
        self.assertLess(list(result).index("inspected_hosts"), list(result).index("estate"))
        for secret in ("HOST_SECRET", "HOME_SECRET", "RUNTIME_SECRET", "RAW_SECRET", "LOG_SECRET"):
            self.assertNotIn(secret, json.dumps(result))

    def test_grounding_bounds_explicit_matches_and_never_uses_user_text_as_an_evidence_name(self):
        hosts = {f"host{i}": {} for i in range(5)}
        result = tools.grounding("host4 host2 host1 host0 host3 ../../credentials run_shell", hosts)
        self.assertEqual([row["host_id"] for row in result["inspected_hosts"]], ["host4", "host2", "host1"])
        self.assertTrue(result["matched_hosts_truncated"])
        names = {call.args[1] for call in self.read.call_args_list}
        self.assertFalse({"../../credentials", "run_shell"} & names)
        self.assertLessEqual(len(json.dumps(result)), 7500)

    def test_grounding_one_bad_saved_host_does_not_hide_another_hosts_inventory(self):
        def read(host_id, name):
            if host_id == "broken":
                raise ValueError("password=PRIVATE_FAILURE")
            return self.records.get(name)
        self.read.side_effect = read
        result = tools.grounding("broken and target db", {"broken": {}, **self.hosts})
        self.assertEqual(result["inspected_hosts"][0]["status"], "unavailable")
        self.assertEqual(result["inspected_hosts"][1]["inventory"]["nodes"][0]["oracle_homes"][0]["patches"],
                         ["29517242", "29585399"])
        self.assertEqual(result["estate"]["hosts"][0]["snapshot_status"], "unavailable")
        self.assertNotIn("PRIVATE_FAILURE", json.dumps(result))

    def test_grounding_preserves_structured_limits_even_when_inventory_is_too_large(self):
        home = self.snapshot["oracle_homes"][0]
        home["patches"] = [str(10000000 + n) for n in range(100)]
        self.snapshot["oracle_homes"] = [copy.deepcopy(home) for _ in range(10)]
        hosts = {f"host{n}": {"label": "L" * 400} for n in range(200)}
        result = tools.grounding("host0", hosts)
        self.assertLessEqual(len(json.dumps(result)), 7500)
        self.assertTrue(result["context_truncated"])
        self.assertTrue(result["matched_hosts_truncated"])
        self.assertTrue(result["estate"]["hosts_truncated"])
        self.assertEqual(result["inspected_hosts"][0]["status"], "omitted_for_context_limit")


if __name__ == "__main__":
    unittest.main()
