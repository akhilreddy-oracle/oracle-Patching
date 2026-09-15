#!/usr/bin/env python3
"""Live recovery transport regressions, with no SSH, sockets, or Oracle."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import evidence
import recoveryctl
from durable import write_json


class LiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opu-recovery-live-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = {"id": "sourcedb", "ssh_alias": "source", "remote_root": "/opt/opu", "sudo": True}
        self.hostfile = self.root / "hosts.json"
        self.hostfile.write_text(json.dumps({"hosts": [self.host]}))
        self.enterContext(patch.object(recoveryctl, "LIVE_DIR", self.root / "live"))
        self.enterContext(patch.object(recoveryctl, "RECOVERY_DIR", self.root / "fixtures"))
        self.enterContext(patch.object(recoveryctl, "HOSTS_FILE", self.hostfile))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "hosts"))
        self.policy = {"schema_version": "1.0", "maximum_snapshot_age_seconds": 1800,
                       "require_xml_inventory": True, "database": {},
                       "recovery": {"require_backup": True, "max_backup_age_minutes": 1440}}
        self.snapshot = {"schema_version": "1.0", "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "collector": {"name": "oracle.topology.discover"},
                         "cluster": {"status": "unavailable", "nodes": []},
                         "oracle_homes": [{"path": "/u01/db", "owner": "oracle"}],
                         "databases": [{"db_unique_name": "ORCL", "oracle_home": "/u01/db", "runtime": {"instance": "ORCL",
                             "status": "complete", "database_role": "PRIMARY", "open_mode": "READ WRITE", "instance_state": "OPEN", "log_mode": "NOARCHIVELOG"}}]}
        evidence.write_evidence("sourcedb", "snapshot", self.snapshot)
        evidence.write_evidence("sourcedb", "policy", self.policy)
        self.sync = self.enterContext(patch.object(recoveryctl.tools_sync, "ensure_tools"))
        self.shell = self.enterContext(patch.object(recoveryctl.remote, "run_remote_shell", return_value=self.response("")))
        self.raw = self.enterContext(patch.object(recoveryctl.remote, "run_remote_raw", return_value=self.response({"request_id": "r1", "state": "awaiting_approval"})))
        self.push = self.enterContext(patch.object(recoveryctl.remote, "push_file"))
        self.pull = self.enterContext(patch.object(recoveryctl.remote, "pull_file"))
        self.context = self.enterContext(patch.object(recoveryctl.pipeline_runner, "set_execution_context"))
        self.production = self.enterContext(patch.object(recoveryctl.production, "require_live_mutation_allowed"))

    def response(self, value, rc=0, stderr=""):
        if isinstance(value, dict) and "state" in value:
            input_dir = recoveryctl.REMOTE_STATE_DIR + "/webapp-inputs/r1"
            try:
                metadata = recoveryctl._metadata("r1")
                inputs, target = metadata["inputs"], metadata["target"]
            except recoveryctl.RecoveryError:
                inputs = {"snapshot": {"path": input_dir + "/snapshot.json", "sha256": hashlib.sha256(evidence.evidence_path("sourcedb", "snapshot").read_bytes()).hexdigest()},
                          "policy": {"path": input_dir + "/policy.json", "sha256": hashlib.sha256((json.dumps(self.policy, indent=2, sort_keys=True) + "\n").encode()).hexdigest()}}
                target = {"database_unique_name": "ORCL", "oracle_home": "/u01/db", "owner": "oracle", "oracle_sid": "ORCL"}
            value = {"source_snapshot": inputs["snapshot"], "policy": inputs["policy"], "target": target, **value}
        return subprocess.CompletedProcess([], rc, json.dumps(value) if not isinstance(value, str) else value, stderr)

    def create(self, **changes):
        kwargs = dict(host=self.host, host_id="sourcedb", database="ORCL", backup_parent="/u02/backups",
                      window_start=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), window_end=(datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        kwargs.update(changes)
        if self.raw.side_effect is None:
            self.raw.side_effect = lambda *a, **k: self.response({"request_id": "r1", "state": "awaiting_approval"})
        return recoveryctl.create_live("r1", "requester", **kwargs)

    def prepared(self):
        result = self.create()
        self.shell.reset_mock()
        self.raw.reset_mock()
        self.raw.side_effect = None
        return result

    def execution(self):
        execution = {"request_id": "r1", "remote_run_dir": recoveryctl.REMOTE_STATE_DIR + "/webapp-executions/r1/" + "a" * 32,
                     "actor": "operator", "terminal": False}
        recoveryctl._update_metadata("r1", execution=execution)
        return execution

    def test_create_seals_snapshot_and_policy_without_fixture_environment(self):
        result = self.create()
        self.assertEqual((result["host_id"], result["mode"]), ("sourcedb", "live"))
        meta = recoveryctl._metadata("r1")
        self.assertEqual(meta["create_state"], "created")
        local = recoveryctl.LIVE_DIR / "r1" / "snapshot.json"
        before = local.read_bytes()
        self.assertEqual(hashlib.sha256(before).hexdigest(), meta["inputs"]["snapshot"]["sha256"])
        evidence.write_evidence("sourcedb", "snapshot", {"changed": True})
        self.assertEqual(local.read_bytes(), before)
        argv = self.raw.call_args.args[1]
        self.assertEqual(argv[:2], ["env", "-i"])
        self.assertFalse(any("TEST_MODE" in arg or "TEST_ALLOW" in arg for arg in argv))
        self.assertIn("/opt/opu/bin/opu-database-recovery-prepare", argv)
        self.assertEqual(self.raw.call_args.kwargs["sudo"], True)
        self.assertNotIn("execute", argv)
        self.assertEqual(self.push.call_count, 2)
        self.assertIn("chmod 400", self.shell.call_args.args[1])

    def test_create_rejects_missing_policy_or_invalid_target_before_remote_calls(self):
        evidence.clear_evidence("sourcedb", "policy")
        with self.assertRaises(recoveryctl.RecoveryError):
            self.create()
        evidence.write_evidence("sourcedb", "policy", self.policy)
        with self.assertRaises(recoveryctl.RecoveryError):
            self.create(database="UNCONFIGURED")
        with self.assertRaises(recoveryctl.RecoveryError):
            self.create(backup_parent="/u02/backups/../oracle")
        with self.assertRaises(recoveryctl.RecoveryError):
            self.create(policy=[])
        self.sync.assert_not_called()
        self.raw.assert_not_called()

    def test_explicit_policy_is_request_scoped_and_existing_id_is_not_replaced(self):
        explicit = {**self.policy, "maximum_snapshot_age_seconds": 3600}
        self.create(policy=explicit)
        self.assertEqual(evidence.read_evidence("sourcedb", "policy"), self.policy)
        self.assertEqual(json.loads((recoveryctl.LIVE_DIR / "r1" / "policy.json").read_text()), explicit)
        count = self.raw.call_count
        with self.assertRaises(recoveryctl.RecoveryError):
            self.create()
        self.assertEqual(self.raw.call_count, count)

    def test_create_unknown_is_durable_and_not_retried(self):
        self.raw.side_effect = recoveryctl.remote.RemoteError("ssh_timeout", "lost create response")
        with self.assertRaises(recoveryctl.remote.RemoteError):
            self.create()
        self.assertEqual(recoveryctl._metadata("r1")["create_state"], "remote_create_unknown")
        with self.assertRaises(recoveryctl.RecoveryError):
            self.create()
        self.assertEqual(self.raw.call_count, 1)

    def test_analysis_blockers_are_preserved_and_visible_in_status(self):
        self.prepared()
        analysis = {"request_id": "r1", "status": "blocked", "reason": "capacity", "capacity": {"required_bytes": 200, "available_bytes": 100}}
        self.raw.return_value = self.response(analysis, 2)
        result = recoveryctl.analyze("r1")
        self.assertEqual(result["status"], "blocked")
        self.raw.return_value = self.response({"request_id": "r1", "state": "awaiting_approval"})
        self.assertEqual(recoveryctl.status("r1")["analysis"], analysis)
        self.assertEqual(recoveryctl.status("r1")["mode"], "live")

    def test_host_config_drift_and_actor_injection_fail_before_ssh(self):
        self.prepared()
        with self.assertRaises(recoveryctl.RecoveryError):
            recoveryctl.approve("r1", "operator; touch /tmp/pwn", "CHG-1")
        self.raw.assert_not_called()
        self.hostfile.write_text(json.dumps({"hosts": [{**self.host, "ssh_alias": "target"}]}))
        with self.assertRaises(recoveryctl.RecoveryError):
            recoveryctl.status("r1")
        self.raw.assert_not_called()

    def test_native_approval_rejection_is_not_reclassified_as_success(self):
        self.prepared()
        self.raw.return_value = self.response("", 65, "requester cannot approve its own recovery preparation")
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "exited 65"):
            recoveryctl.approve("r1", "requester", "CHG-1")
        argv = self.raw.call_args.args[1]
        self.assertEqual(argv[-4:], ["--actor", "requester", "--approval-ticket", "CHG-1"])

    def test_execute_persists_unknown_launch_before_ssh_and_never_relaunches(self):
        self.prepared()
        self.raw.return_value = self.response({"request_id": "r1", "state": "authorized", "authorization": {"actor": "operator"}})
        def interrupted(alias, script, **kwargs):
            metadata = recoveryctl._metadata("r1")
            self.assertFalse(metadata["execution"]["terminal"])
            self.assertIn(metadata["execution"]["remote_run_dir"], script)
            self.assertTrue(self.context.called)
            raise recoveryctl.remote.RemoteError("ssh_timeout", "launch response lost")
        self.shell.side_effect = interrupted
        with self.assertRaises(recoveryctl.remote.RemoteError):
            recoveryctl.execute("r1", "operator")
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "already submitted"):
            recoveryctl.execute("r1", "operator")
        self.assertEqual(self.shell.call_count, 1)
        self.production.assert_called()

    def test_execute_refuses_actor_differing_from_sealed_authorizer(self):
        self.prepared()
        self.raw.return_value = self.response({"request_id": "r1", "state": "authorized", "authorization": {"actor": "operator"}})
        with self.assertRaises(recoveryctl.RecoveryError):
            recoveryctl.execute("r1", "requester")
        self.shell.assert_not_called()
        self.assertIsNone(recoveryctl._metadata("r1").get("execution"))

    def completed_native(self):
        payloads = {}
        result = {"backup_root": "/u02/backups/r1"}
        for kind, filename in (("recovery_evidence", "recovery-evidence.json"), ("post_snapshot", "post-backup-topology.json"), ("reconciliation", "post-backup-reconciliation.json")):
            path = recoveryctl.REMOTE_STATE_DIR + "/r1/evidence/" + filename
            content = (json.dumps({"kind": kind, "native_path": path}, indent=3) + "\n").encode()
            payloads[path] = content
            result[kind] = {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
        return {"request_id": "r1", "state": "completed", "result": result}, payloads

    def test_completed_evidence_is_imported_byte_for_byte_without_rebinding(self):
        self.prepared()
        self.execution()
        native, payloads = self.completed_native()
        self.raw.return_value = self.response(native)
        self.shell.return_value = self.response("RC\n0\n")
        self.pull.side_effect = lambda alias, path, **kwargs: payloads[path]
        result = recoveryctl.status("r1")
        self.assertEqual(result["result"], native["result"])
        for binding in result["webapp_evidence"].values():
            self.assertEqual(Path(binding["path"]).read_bytes(), payloads[binding["remote_path"]])
        self.assertTrue(result["webapp_execution"]["terminal"])
        self.assertTrue(all(call.kwargs["max_bytes"] == recoveryctl.MAX_EVIDENCE_BYTES for call in self.pull.call_args_list))
        self.assertNotEqual(recoveryctl.recovery_evidence_path("r1"), native["result"]["recovery_evidence"]["path"])
        self.assertFalse(evidence.evidence_path("sourcedb", "recovery").exists())

    def test_execute_completion_requires_exit_and_native_state_and_imported_evidence(self):
        self.prepared()
        native, payloads = self.completed_native()
        self.raw.side_effect = [self.response({"request_id": "r1", "state": "authorized", "authorization": {"actor": "operator"}}), self.response(native)]
        self.shell.side_effect = [self.response("12345\n"), self.response("RC\n0\n")]
        self.pull.side_effect = lambda alias, path, **kwargs: payloads[path]
        with patch.object(recoveryctl.time, "sleep"):
            result = recoveryctl.execute("r1", "operator")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["webapp_execution"]["exit_code"], 0)
        self.assertTrue(result["webapp_execution"]["terminal"])
        self.assertIn("nohup setsid", self.shell.call_args_list[0].args[1])
        self.assertTrue(any(call.kwargs.get("detached_terminal") is True for call in self.context.call_args_list))

    def test_failed_terminal_execution_remains_failed_without_retry(self):
        self.prepared()
        self.raw.side_effect = [self.response({"request_id": "r1", "state": "authorized", "authorization": {"actor": "operator"}}), self.response({"request_id": "r1", "state": "failed_services_restored"})]
        self.shell.side_effect = [self.response("12345\n"), self.response("RC\n1\n")]
        with patch.object(recoveryctl.time, "sleep"):
            with self.assertRaisesRegex(recoveryctl.RecoveryError, "exited 1"):
                recoveryctl.execute("r1", "operator")
        meta = recoveryctl._metadata("r1")
        self.assertTrue(meta["execution"]["terminal"])
        self.assertEqual(meta["last_status"]["state"], "failed_services_restored")
        self.pull.assert_not_called()
        with self.assertRaises(recoveryctl.RecoveryError):
            recoveryctl.execute("r1", "operator")
        self.assertEqual(self.shell.call_count, 2)

    def test_reconciliation_context_cannot_select_other_host_or_launch(self):
        self.prepared()
        execution = self.execution()
        context = {"recovery_request_id": "r1", "host_id": "targetdb", "ssh_alias": "source", "remote_root": "/opt/opu", "remote_run_dir": execution["remote_run_dir"]}
        result = recoveryctl.reconcile_detached_run({"context": context})
        self.assertEqual(result["status"], "unknown")
        self.shell.assert_not_called()
        self.raw.assert_not_called()

    def test_mismatched_evidence_hash_or_path_cannot_publish_completion(self):
        self.prepared()
        self.execution()
        native, payloads = self.completed_native()
        self.raw.return_value = self.response(native)
        self.shell.return_value = self.response("RC\n0\n")
        self.pull.return_value = b'{}'
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "digest"):
            recoveryctl.status("r1")
        self.assertFalse(recoveryctl._metadata("r1").get("evidence"))
        native["result"]["recovery_evidence"]["path"] = "/etc/shadow"
        self.raw.return_value = self.response(native)
        self.pull.reset_mock()
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "unexpected"):
            recoveryctl.status("r1")
        self.pull.assert_not_called()

    def test_reconcile_active_execution_does_not_launch_or_restore_services(self):
        self.prepared()
        execution = self.execution()
        self.shell.return_value = self.response("RUNNING\n")
        result = recoveryctl.reconcile("r1", "operator")
        self.assertEqual(result["status"], "in_progress")
        self.raw.assert_not_called()
        context = {"recovery_request_id": "r1", "host_id": "sourcedb", "ssh_alias": "source", "remote_root": "/opt/opu", "remote_run_dir": execution["remote_run_dir"]}
        self.assertEqual(recoveryctl.reconcile_detached_run({"context": context})["status"], "unknown")
        self.assertTrue(all("nohup" not in call.args[1] for call in self.shell.call_args_list))

    def test_scope_filters_unrelated_unreadable_requests_without_ssh(self):
        self.prepared()
        self.raw.side_effect = recoveryctl.remote.RemoteError("unreachable", "host unavailable")
        self.assertEqual(recoveryctl.list_requests(host_id="targetdb"), [])
        self.raw.assert_not_called()
        rows = recoveryctl.list_requests(host_id="sourcedb")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["host_id"], rows[0]["state"]), ("sourcedb", "unreadable"))

    def test_full_backup_approval_restore_validation_and_selection_transport(self):
        self.prepared()
        calls = []
        completed, payloads = self.completed_native()
        current = {"request_id": "r1", "state": "awaiting_approval"}

        def native(alias, argv, **kwargs):
            action = argv[7]
            calls.append(action)
            if action == "analyze":
                return self.response({"request_id": "r1", "status": "passed", "capacity": {"admitted": True}})
            if action == "approve":
                self.assertEqual(argv[-4:], ["--actor", "approver", "--approval-ticket", "CHG-42"])
                current.update(state="approved", approval={"actor": "approver"})
            elif action == "authorize":
                self.assertEqual(argv[-2:], ["--actor", "operator"])
                current.update(state="authorized", authorization={"actor": "operator"})
            return self.response(current)

        self.raw.side_effect = native
        self.assertEqual(recoveryctl.analyze("r1")["status"], "passed")
        self.assertEqual(recoveryctl.approve("r1", "approver", "CHG-42")["state"], "approved")
        self.assertEqual(recoveryctl.authorize("r1", "operator")["state"], "authorized")

        def transport(alias, script, **kwargs):
            if "nohup" in script:
                current.update(completed)
                return self.response("12345\n")
            return self.response("RC\n0\n")

        self.shell.side_effect = transport
        self.pull.side_effect = lambda alias, path, **kwargs: payloads[path]
        with patch.object(recoveryctl.time, "sleep"):
            self.assertEqual(recoveryctl.execute("r1", "operator")["state"], "completed")
        selected = recoveryctl.selection_status("r1", host_id="sourcedb", host=self.host)
        self.assertEqual(selected["webapp_execution"]["exit_code"], 0)
        self.assertEqual(set(selected["webapp_evidence"]), {"recovery_evidence", "post_snapshot", "reconciliation"})
        self.assertEqual(calls[:3], ["analyze", "approve", "authorize"])
        self.assertEqual(sum("nohup" in call.args[1] for call in self.shell.call_args_list), 1)

    def test_complete_policy_and_native_option_ranges_before_remote_calls(self):
        invalid = [
            {"schema_version": "1.0"},
            {**self.policy, "require_xml_inventory": "true"},
            {**self.policy, "maximum_snapshot_age_seconds": True},
            {**self.policy, "maximum_snapshot_age_seconds": 59},
            {**self.policy, "maximum_snapshot_age_seconds": float("nan")},
            {**self.policy, "recovery": {"require_backup": False}},
            {**self.policy, "recovery": {"require_backup": True, "storage_mode": "filesystem"}},
            {**self.policy, "recovery": {"require_backup": True, "minimum_filesystem_free_bytes": -1}},
            {**self.policy, "database": {"listener_registration_timeout_seconds": True}},
        ]
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(recoveryctl.RecoveryError):
                self.create(policy=policy)
        self.sync.assert_not_called()
        self.raw.assert_not_called()

    def test_expired_future_and_missing_discovery_reject_before_remote_calls(self):
        for timestamp in [(datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), None]:
            snapshot = {**self.snapshot, "collected_at": timestamp}
            evidence.write_evidence("sourcedb", "snapshot", snapshot)
            with self.assertRaisesRegex(recoveryctl.RecoveryError, "Refresh discovery"):
                self.create()
        self.raw.assert_not_called()
        self.sync.assert_not_called()

    def test_changed_configured_host_is_rejected_at_create(self):
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "retarget"):
            self.create(host={**self.host, "ssh_alias": "other"})
        self.raw.assert_not_called()

    def test_native_response_cannot_change_bound_input_or_target(self):
        self.prepared()
        for field, value in [("source_snapshot", {"path": "/other", "sha256": "a" * 64}),
                             ("policy", {}), ("target", {"database_unique_name": "OTHER"})]:
            self.raw.return_value = self.response({"request_id": "r1", "state": "approved", field: value})
            with self.assertRaisesRegex(recoveryctl.RecoveryError, "sealed host inputs"):
                recoveryctl.status("r1")

    def test_unknown_missing_or_invalid_pid_never_launches_service_reconciliation(self):
        self.prepared()
        self.execution()
        for state in ("UNKNOWN", "MISSING"):
            self.shell.return_value = self.response(state + "\n")
            with self.assertRaisesRegex(recoveryctl.RecoveryError, "no verified dead wrapper"):
                recoveryctl.reconcile("r1", "operator")
        self.raw.assert_not_called()
        self.assertTrue(all("nohup" not in call.args[1] for call in self.shell.call_args_list))
        self.assertIn('[[ "$pid" =~ ^[1-9][0-9]*$ ]]', self.shell.call_args.args[1])

    def test_dead_execution_uses_detached_service_reconciliation_and_closes_original(self):
        self.prepared()
        original = self.execution()
        self.raw.side_effect = [
            self.response({"request_id": "r1", "state": "running", "authorization": {"actor": "operator"}}),
            self.response({"request_id": "r1", "state": "failed_services_restored"}),
        ]
        self.shell.side_effect = [self.response("DEAD\n"), self.response("DEAD\n"), self.response("12345\n"), self.response("RC\n0\n")]
        with patch.object(recoveryctl.time, "sleep"):
            result = recoveryctl.reconcile("r1", "operator")
        self.assertEqual(result["state"], "failed_services_restored")
        metadata = recoveryctl._metadata("r1")
        self.assertTrue(metadata["execution"]["reconciled"])
        self.assertEqual(metadata["execution"]["remote_run_dir"], original["remote_run_dir"])
        self.assertEqual(metadata["reconciliation_execution"]["action"], "reconcile")
        self.assertTrue(metadata["reconciliation_execution"]["terminal"])
        self.assertTrue(any(call.kwargs.get("recovery_action") == "reconcile" for call in self.context.call_args_list))
        self.pull.assert_not_called()

    def test_worker_exit_with_running_native_state_can_restore_services(self):
        self.prepared()
        self.execution()
        running = self.response({"request_id": "r1", "state": "running", "authorization": {"actor": "operator"}})
        restored = self.response({"request_id": "r1", "state": "failed_services_restored"})
        self.raw.side_effect = [running, running, restored]
        self.shell.side_effect = [self.response("RC\n137\n"), self.response("RC\n137\n"), self.response("12345\n"), self.response("RC\n0\n")]
        with patch.object(recoveryctl.time, "sleep"):
            result = recoveryctl.reconcile("r1", "operator")
        self.assertEqual(result["state"], "failed_services_restored")
        self.assertTrue(recoveryctl._metadata("r1")["execution"]["reconciled"])

    def test_service_restoration_failure_never_becomes_success(self):
        self.prepared()
        self.execution()
        self.raw.side_effect = [
            self.response({"request_id": "r1", "state": "running", "authorization": {"actor": "operator"}}),
            self.response({"request_id": "r1", "state": "recovery_required"}),
        ]
        self.shell.side_effect = [self.response("DEAD\n"), self.response("DEAD\n"), self.response("12345\n"), self.response("RC\n0\n")]
        with patch.object(recoveryctl.time, "sleep"), self.assertRaisesRegex(recoveryctl.RecoveryError, "health remains unverified"):
            recoveryctl.reconcile("r1", "operator")
        self.assertTrue(recoveryctl._metadata("r1")["reconciliation_execution"]["terminal"])
        self.pull.assert_not_called()

    def test_poll_script_distinguishes_missing_invalid_live_and_mismatched_pid(self):
        # Execute only the fixed read-only poll locally in a temporary fixture.
        state_root = self.root / "remote-state"
        run_dir = state_root / "webapp-executions" / "r1" / ("b" * 32)
        run_dir.mkdir(parents=True)
        execution = {"request_id":"r1", "remote_run_dir":str(run_dir)}
        self.shell.side_effect = lambda alias, script, **kwargs: subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
        with patch.object(recoveryctl, "REMOTE_STATE_DIR", str(state_root)):
            self.assertEqual(recoveryctl._poll_launch(self.host, execution), ("UNKNOWN", None))
            (run_dir / "pid").write_text("invalid\n")
            self.assertEqual(recoveryctl._poll_launch(self.host, execution), ("UNKNOWN", None))
            (run_dir / "pid").write_text(str(os.getpid()) + "\n")
            self.assertEqual(recoveryctl._poll_launch(self.host, execution), ("RUNNING", None))
            self.assertEqual(recoveryctl._poll_launch(self.host, {**execution, "pid":os.getpid()+1}), ("UNKNOWN", None))
            (run_dir / "rc").write_text("137\n")
            self.assertEqual(recoveryctl._poll_launch(self.host, execution), ("RC", 137))

    def test_service_reconciliation_disconnect_never_relaunches(self):
        self.prepared()
        self.execution()
        self.raw.return_value = self.response({"request_id": "r1", "state": "running", "authorization": {"actor": "operator"}})
        self.shell.side_effect = [self.response("DEAD\n"), self.response("DEAD\n"), recoveryctl.remote.RemoteError("ssh_timeout", "disconnected")]
        with self.assertRaises(recoveryctl.remote.RemoteError):
            recoveryctl.reconcile("r1", "operator")
        persisted = recoveryctl._metadata("r1")["reconciliation_execution"]
        self.assertFalse(persisted["terminal"])
        self.shell.side_effect = None
        self.shell.return_value = self.response("DEAD\n")
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "already has an unknown outcome"):
            recoveryctl.reconcile("r1", "operator")
        self.assertEqual(sum("nohup" in call.args[1] for call in self.shell.call_args_list), 1)

    def test_selection_rejects_cross_host_without_contact_and_changed_imports(self):
        self.prepared()
        self.execution()
        native, payloads = self.completed_native()
        self.raw.return_value = self.response(native)
        self.shell.return_value = self.response("RC\n0\n")
        self.pull.side_effect = lambda alias, path, **kwargs: payloads[path]
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "different configured host"):
            recoveryctl.selection_status("r1", host_id="targetdb", host=self.host)
        self.raw.assert_not_called()
        selected = recoveryctl.selection_status("r1", host_id="sourcedb", host=self.host)
        path = Path(selected["webapp_evidence"]["recovery_evidence"]["path"])
        path.chmod(0o600)
        path.write_text('{}')
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "missing or changed"):
            recoveryctl.selection_status("r1", host_id="sourcedb", host=self.host)

    def test_completed_native_without_successful_wrapper_is_not_selectable(self):
        self.prepared()
        native, payloads = self.completed_native()
        self.raw.return_value = self.response(native)
        with self.assertRaisesRegex(recoveryctl.RecoveryError, "verified successful execution"):
            recoveryctl.selection_status("r1", host_id="sourcedb", host=self.host)
        self.pull.assert_not_called()

    def test_fixture_requests_continue_to_use_only_fixture_runner(self):
        with patch.object(recoveryctl, "_run", return_value={"request_id": "fixture", "status": "passed"}) as fixture:
            self.assertEqual(recoveryctl.analyze("fixture")["status"], "passed")
            fixture.assert_called_once_with("fixture", ["analyze", "--request-id", "fixture"])
        self.raw.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
