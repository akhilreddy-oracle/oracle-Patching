"""Fixture claims must not survive source/evidence drift or imply live approval."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "release_validation.py"
spec = importlib.util.spec_from_file_location("release_validation", MODULE)
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)


class ValidationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "opu-fixture").write_text("fixture runtime v1")
        self.bundle = self.root / "release-validation"
        self.bundle.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def record(self, exit_code=0):
        log = b"Fixture command output\n"
        (self.bundle / "browser.log").write_bytes(log)
        snapshot = rv.source_snapshot(self.root)
        record = {"schema_version": "1.0", "classification": "fixture-only", "scopes": ["browser"], "source_before": snapshot, "source_after_sha256": snapshot["sha256"], "receipts": [{"scope": "browser", "command": rv.COMMANDS["browser"], "exit_code": exit_code, "log_path": "browser.log", "log_sha256": rv.digest(log)}]}
        self.save(record)
        return record

    def save(self, record):
        record = {key: value for key, value in record.items() if key != "record_sha256"}
        record["record_sha256"] = rv.digest(rv.canonical(record))
        (self.bundle / "manifest.json").write_text(json.dumps(record))

    def test_matching_receipts_are_scoped_and_never_grant_live_or_production(self):
        self.record()
        status = rv.validation_status(self.root)
        self.assertEqual(status["fixture_tested"]["status"], "passed")
        self.assertEqual(status["fixture_tested"]["scopes"], ["browser"])
        self.assertEqual(status["live_lab_verified"]["status"], "unverified")
        self.assertEqual(status["production_approved"]["status"], "unverified")

    def test_runtime_change_invalidates_previous_receipts(self):
        self.record()
        (self.root / "bin" / "opu-fixture").write_text("runtime v2")
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")

    def test_shellcheck_policy_change_invalidates_previous_receipts(self):
        policy = self.root / ".shellcheckrc"
        policy.write_text("disable=SC1091\n")
        record = self.record()
        self.assertIn(".shellcheckrc", [item["path"] for item in record["source_before"]["files"]])
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "passed")
        policy.write_text("disable=SC1091,SC2086\n")
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")
        self.record()
        policy.unlink()
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")

    def test_failed_command_cannot_be_reported_as_passed(self):
        self.record(exit_code=1)
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "failed")

    def test_changed_or_missing_logs_invalidate_status(self):
        self.record()
        (self.bundle / "browser.log").write_text("Different evidence")
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")
        (self.bundle / "browser.log").unlink()
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")

    def test_arbitrary_commands_paths_and_forged_production_flags_are_not_authority(self):
        record = self.record()
        record["production_approved"] = True
        record["live_lab_verified"] = True
        self.save(record)
        status = rv.validation_status(self.root)
        self.assertEqual(status["production_approved"]["status"], "unverified")
        record["receipts"][0]["command"] = ["true"]
        self.save(record)
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")
        record = self.record()
        record["receipts"][0]["log_path"] = "../outside"
        self.save(record)
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")

    def test_symlinked_artifact_cannot_supply_validation_evidence(self):
        self.record()
        (self.bundle / "browser.log").unlink()
        (self.bundle / "browser.log").symlink_to(self.root / "bin" / "opu-fixture")
        self.assertEqual(rv.validation_status(self.root)["fixture_tested"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
