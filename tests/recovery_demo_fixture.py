#!/usr/bin/env python3
"""Real native recovery commands against isolated app demo files; no Oracle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import recoveryctl


class RecoveryDemoFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opu-app-recovery-demo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.object(recoveryctl, "RECOVERY_DIR", self.root / "requests"))
        self.enterContext(patch.object(recoveryctl, "LIVE_DIR", self.root / "live"))
        self.request_id = "demo-regression"
        self.fixture = recoveryctl.RECOVERY_DIR / self.request_id

    def test_app_demo_passes_native_analysis_and_execution_with_persistent_analysis(self):
        created = recoveryctl.create_testmode_demo(self.request_id, "requester")
        self.assertEqual(created["state"], "awaiting_approval")
        request_path = self.fixture / "state" / self.request_id / "request.json"
        before = hashlib.sha256(request_path.read_bytes()).hexdigest()
        analysis = recoveryctl.analyze(self.request_id)
        self.assertEqual(analysis["status"], "passed", analysis)
        capacity = analysis["capacity"]
        self.assertTrue(capacity["admitted"])
        self.assertTrue(capacity["complete_datafile_coverage"])
        measured = capacity["datafiles"][0]
        datafile = self.fixture / "runtime" / "oradata" / "system01.dbf"
        self.assertEqual(Path(measured["path"]).resolve(), datafile.resolve())
        self.assertEqual(measured["allocated_bytes"], datafile.stat().st_size)
        self.assertGreater(measured["filesystem_allocated_bytes"], 0)
        self.assertEqual(capacity["spfile_bytes"], (self.fixture / "runtime" / "spfileORCL.ora").stat().st_size)
        self.assertEqual(hashlib.sha256(request_path.read_bytes()).hexdigest(), before, "Analysis must not rewrite the sealed native request")
        self.assertEqual(recoveryctl.status(self.request_id)["analysis"], analysis, "Analysis must survive a fresh status request and identity switch")
        approved = recoveryctl.approve(self.request_id, "approver", "CHG-DEMO")
        self.assertEqual(approved["state"], "approved")
        approval_analysis = approved["approval"]["analysis"]
        self.assertEqual(approval_analysis["status"], "passed")
        self.assertTrue(approval_analysis["capacity"]["admitted"])
        for binding in ("source_request", "source_snapshot", "policy", "target"):
            self.assertEqual(approval_analysis[binding], approved[binding])
        self.assertTrue(approval_analysis["analyzed_at"])
        self.assertEqual(recoveryctl.authorize(self.request_id, "operator")["state"], "authorized")
        result = recoveryctl.execute(self.request_id, "operator")
        self.assertEqual(result["state"], "completed")
        self.assertEqual((self.fixture / "runtime" / "database.state").read_text().strip(), "OPEN")
        self.assertIsNotNone(result["result"]["recovery_evidence"])
        order = (self.fixture / "runtime" / "order.log").read_text()
        self.assertIn("request-lock:sqlplus:closed", order)
        self.assertIn("request-lock:listener:closed", order)
        self.assertNotIn("request-lock:sqlplus:open", order)
        self.assertNotIn("request-lock:listener:open", order)

    def test_missing_fixture_spfile_remains_blocked_and_replaces_prior_analysis(self):
        recoveryctl.create_testmode_demo(self.request_id, "requester")
        self.assertEqual(recoveryctl.analyze(self.request_id)["status"], "passed")
        (self.fixture / "runtime" / "spfileORCL.ora").unlink()
        # The browser may still show a previously passed analysis. Approval
        # must perform a new native probe instead of trusting that cached JSON.
        with self.assertRaises(recoveryctl.RecoveryError) as rejection:
            recoveryctl.approve(self.request_id, "approver", "CHG-STALE-ANALYSIS")
        self.assertIn("analysis must pass before approval", rejection.exception.stderr)
        self.assertEqual(recoveryctl.status(self.request_id)["state"], "awaiting_approval")
        blocked = recoveryctl.analyze(self.request_id)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("capacity cannot be proven", blocked["reason"])
        self.assertEqual(recoveryctl.status(self.request_id)["analysis"], blocked)
        self.assertEqual((self.fixture / "runtime" / "database.state").read_text().strip(), "OPEN")

    def test_direct_cli_approval_without_ui_analysis_runs_and_seals_native_preflight(self):
        recoveryctl.create_testmode_demo(self.request_id, "requester")
        self.assertIsNone(recoveryctl.status(self.request_id)["analysis"])
        approved = recoveryctl._run(self.request_id, ["approve", "--request-id", self.request_id,
                                    "--actor", "approver", "--approval-ticket", "CHG-DIRECT-CLI"])
        self.assertEqual(approved["approval"]["analysis"]["status"], "passed")
        self.assertEqual(approved["approval"]["analysis"]["source_request"], approved["source_request"])
        self.assertEqual(recoveryctl.status(self.request_id)["analysis"], approved["approval"]["analysis"])
        self.assertEqual((self.fixture / "runtime" / "database.state").read_text().strip(), "OPEN")

    def test_failed_analysis_refresh_cannot_reuse_saved_pass(self):
        recoveryctl.create_testmode_demo(self.request_id, "requester")
        self.assertEqual(recoveryctl.analyze(self.request_id)["status"], "passed")
        with patch.object(recoveryctl, "_run", side_effect=recoveryctl.RecoveryError("Probe disconnected")):
            with self.assertRaises(recoveryctl.RecoveryError):
                recoveryctl.analyze(self.request_id)
        self.assertIsNone(recoveryctl.status(self.request_id)["analysis"])

    def test_concurrent_cli_mutations_cannot_overtake_approval_analysis(self):
        recoveryctl.create_testmode_demo(self.request_id, "requester")
        # Hold the first approver inside its SQL identity probe. The second
        # mutation must fail before reading/changing request state, rather than
        # wait and overwrite the first actor's successful sealed approval.
        shim = self.fixture / "oracle" / "dbhome_1" / "bin" / "sqlplus"
        original = shim.read_text()
        shim.write_text(original.replace("input=$(cat)", "input=$(cat)\n"
            "if [[ $input == *OPU_RECOVERY_PREP_PROBE* ]]; then\n"
            "  touch \"$OPU_TEST_RUNTIME/probe-started\"\n"
            "  while [ ! -e \"$OPU_TEST_RUNTIME/probe-release\" ]; do sleep 0.05; done\nfi", 1))
        first = subprocess.Popen([str(recoveryctl.TOOL), "approve", "--request-id", self.request_id,
                                  "--actor", "approver-one", "--approval-ticket", "CHG-CONCURRENT"],
                                 env=recoveryctl._env_for(self.request_id), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        marker = self.fixture / "runtime" / "probe-started"
        release = self.fixture / "runtime" / "probe-release"
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "First approver did not reach its native probe")
            lock_path = self.fixture / "state" / self.request_id / "operation.lock"
            held_inode = lock_path.stat().st_ino
            for action, extra in (("approve", ["--approval-ticket", "CHG-SECOND"]), ("authorize", []), ("execute", []), ("reconcile", [])):
                with self.subTest(action=action), self.assertRaises(recoveryctl.RecoveryError) as rejection:
                    recoveryctl._run(self.request_id, [action, "--request-id", self.request_id, "--actor", "approver-two", *extra], timeout=5)
                self.assertIn("another operation owns this recovery request", rejection.exception.stderr)
            self.assertEqual(recoveryctl.status(self.request_id)["state"], "awaiting_approval")
            self.assertEqual(lock_path.stat().st_ino, held_inode)
        finally:
            release.touch()
            try:
                stdout, stderr = first.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                first.kill()
                first.communicate()
                raise
        self.assertEqual(first.returncode, 0, stderr)
        approved = json.loads(stdout)
        self.assertEqual(approved["approval"]["actor"], "approver-one")
        self.assertEqual(recoveryctl.status(self.request_id)["approval"]["actor"], "approver-one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
