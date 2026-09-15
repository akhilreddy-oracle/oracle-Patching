#!/usr/bin/env python3
"""Fleet metadata configuration remains separate from connection and evidence data."""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import evidence
import fleet
import fleet_metadata as metadata


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "fleet-metadata.json"
        self.enterContext(patch.object(metadata, "STORE_FILE", self.store))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "evidence"))
        self.hosts = {"source": {"id": "source", "label": "Source", "environment": "legacy",
                                "desired_patch_baseline": "12345", "address": "fixture.invalid",
                                "ssh_user": "fixture", "ssh_key": "fixture-key"},
                      "target": {"id": "target"}}
        self.original = copy.deepcopy(self.hosts)

    def edit(self, host="source", environment="test", baseline="39034528", version=None):
        return metadata.update(self.hosts, host, {"expected_version": version or metadata.snapshot(self.hosts)[host]["metadata_version"],
                               "environment": environment, "desired_patch_baseline": baseline}, actor="fixture-admin")

    def test_metadata_only_changes_display_values_without_source_or_evidence_changes(self):
        self.assertFalse(self.store.exists())
        result = self.edit(environment=" QA / Stage ")
        self.assertEqual(result["environment"], "QA / Stage")
        self.assertEqual(result["desired_patch_baseline"], "39034528")
        self.assertEqual(self.hosts, self.original)
        data = json.loads(self.store.read_text())
        self.assertEqual(set(data["hosts"]["source"]), metadata.FIELDS | {"revision", "updated_at", "updated_by"})
        self.assertNotIn("fixture-key", self.store.read_text())
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "evidence").exists())

    def test_explicit_clearing_overrides_hosts_json_and_missing_baseline_stays_unknown(self):
        self.edit(environment="", baseline=None)
        configured = metadata.snapshot(self.hosts)["source"]
        self.assertIsNone(configured["environment"])
        self.assertIsNone(configured["desired_patch_baseline"])
        row = next(row for row in fleet.build(self.hosts, can_manage_metadata=True)["databases"] if row["host_id"] == "source")
        self.assertEqual(row["configuration_missing"], ["environment", "desired_patch_baseline"])
        self.assertEqual(row["baseline_status"], "unknown")
        self.assertEqual(row["evidence_status"], "unknown")

    def test_invalid_fields_and_values_cannot_write(self):
        for bad in [True, -1, 0, "0", "00123", "12.3", "1e7", "1;shutdown", "1" * 21, {}, []]:
            with self.subTest(baseline=bad), self.assertRaises(metadata.MetadataError):
                self.edit(baseline=bad)
        for bad in [True, ["prod"], "<script>", "test\nprod", "a" * 65]:
            with self.subTest(environment=bad), self.assertRaises(metadata.MetadataError):
                self.edit(environment=bad)
        with self.assertRaises(metadata.MetadataError):
            metadata.update(self.hosts, "source", {"environment": "test", "desired_patch_baseline": "123", "expected_version": "a" * 64, "ssh_user": "root"}, actor="admin")
        self.assertFalse(self.store.exists())

    def test_a_stale_version_and_changed_host_source_are_conflicts(self):
        version = metadata.snapshot(self.hosts)["source"]["metadata_version"]
        self.edit(version=version)
        with self.assertRaises(metadata.MetadataError) as caught:
            self.edit(version=version)
        self.assertEqual(caught.exception.status, 409)
        current = metadata.snapshot(self.hosts)["source"]["metadata_version"]
        self.hosts["source"]["environment"] = "changed-outside-app"
        with self.assertRaises(metadata.MetadataError) as caught:
            self.edit(version=current)
        self.assertEqual(caught.exception.status, 409)

    def test_concurrent_edits_preserve_hosts_and_reject_one_stale_writer(self):
        version = metadata.snapshot(self.hosts)["source"]["metadata_version"]
        ready = threading.Barrier(2)
        def edit(index):
            ready.wait(timeout=3)
            try:
                return self.edit(environment=f"team-{index}", version=version)
            except metadata.MetadataError as exc:
                return exc.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn(409, results)
        self.edit(host="target", environment="QA", baseline="23456")
        self.assertEqual(set(json.loads(self.store.read_text())["hosts"]), {"source", "target"})

    def test_corrupt_or_unsafe_sidecar_does_not_invent_compliance(self):
        self.store.write_text("{broken")
        result = fleet.build(self.hosts, can_manage_metadata=True)
        self.assertFalse(result["can_manage_metadata"])
        self.assertIn("unreadable", result["metadata_error"])
        self.assertTrue(all(row["baseline_status"] == "unknown" and row["desired_patch_baseline"] is None for row in result["databases"]))
        self.store.unlink()
        target = self.root / "other.json"
        target.write_text('{"schema_version":1,"hosts":{}}')
        self.store.symlink_to(target)
        with self.assertRaises(metadata.MetadataError) as caught:
            self.edit()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(target.read_text(), '{"schema_version":1,"hosts":{}}')

    def test_lock_link_and_fifo_are_rejected_without_writing(self):
        version = metadata.snapshot(self.hosts)["source"]["metadata_version"]
        victim = self.root / "victim"
        victim.write_text("unchanged")
        lock = self.store.with_suffix(".lock")
        lock.symlink_to(victim)
        with self.assertRaises(metadata.MetadataError):
            self.edit(version=version)
        self.assertEqual(victim.read_text(), "unchanged")
        lock.unlink()
        os.mkfifo(self.store)
        with self.assertRaises(metadata.MetadataError):
            metadata.snapshot(self.hosts)


if __name__ == "__main__":
    unittest.main()
