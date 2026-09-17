#!/usr/bin/env python3
"""Managed lock recovery boundary tests; no network or Oracle processes."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import lockctl
import pipeline_runner


def seal(value):
    body = {k: v for k, v in value.items() if k != "record_sha256"}
    return {**body, "record_sha256": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()}


class LockBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(pipeline_runner, "RUNS_DIR", Path(self.temp.name)))
        self.enterContext(patch.object(pipeline_runner, "RUNS", {}))
        self.enterContext(patch.object(pipeline_runner, "_ACTIVE_KEYS", {}))
        self.host = {"id": "source", "node_name": "source", "ssh_alias": "source", "remote_root": "/opt/opu", "sudo": True}
        self.original = pipeline_runner.RunRecord("a" * 12, "execute", "plan:p:execute")
        self.original.status = "unknown"
        self.original.context = {"detached_execution": True, "plan_id": "p", "task_id": "003-validate-source",
                                 "node": "source", "host_id": "source", "ssh_alias": "source", "remote_root": "/opt/opu",
                                 "remote_run_dir": "/opt/opu/var/webapp-runs/p/003-validate-source/" + "b" * 32}
        pipeline_runner.RUNS[self.original.run_id] = self.original
        self.plan = {"state": "running", "plan_sha256": "c" * 64, "procedure": {"adapter": "database_single_instance_opatch"}}
        self.enterContext(patch.object(lockctl.planctl, "_resolve_node_host", return_value=self.host))
        self.enterContext(patch.object(lockctl.planctl, "status", return_value=self.plan))
        self.enterContext(patch.object(lockctl.planctl, "_read_sealed_actor", return_value="operator"))
        self.native = self.enterContext(patch.object(lockctl.planctl, "_run", return_value={"status": "pending", "stage": "validate"}))
        self.sync = self.enterContext(patch.object(lockctl.tools_sync, "ensure_host_tools"))
        self.ssh = self.enterContext(patch.object(lockctl.remote, "run_remote_raw"))
        self.pull = self.enterContext(patch.object(lockctl.remote, "pull_file"))
        self.launch = self.enterContext(patch.object(lockctl.planctl, "_run_detached_remote"))
        self.report = seal({"status": "eligible", "recovery_eligible": True, "blockers": [],
                            "plan_id": "p", "task_id": "003-validate-source", "run_id": "b" * 32,
                            "actor": "operator", "plan_sha256": "c" * 64, "wrapper": {"exit_code": 75}})

    def scope(self):
        return lockctl._scope("p", "operator", self.original.run_id)

    def completed(self):
        return seal({**self.report, "status": "completed", "original_task_status": "pending", "plan_and_task_unchanged": True,
                     "lock_inode_preserved": True, "lock_released": True,
                     "lock": {"safe": True, "identity": [1, 2], "identity_after": [1, 2], "inode_preserved": True,
                              "held_after": False, "inherited_holders_after": []}, "health_before": {"dbid": "123"},
                     "service_health": {"dbid": "123", "instance_status": "OPEN", "database_role": "PRIMARY", "open_mode": "READ WRITE", "listener_ready": True}})

    def maintenance(self):
        record = pipeline_runner.RunRecord("d" * 12, "lock_recovery", "plan:p:lock-recovery")
        record.status = "running"
        record.context = {**self.original.context, "lock_recovery": True, "actor": "operator", "original_run_id": self.original.run_id,
                          "original_task_id": "003-validate-source", "original_remote_run_id": "b" * 32, "original_plan_sha256": "c" * 64,
                          "task_id": "lock-recover-003-validate-source",
                          "remote_run_dir": "/opt/opu/var/webapp-runs/p/lock-recover-003-validate-source/" + "e" * 32}
        pipeline_runner.RUNS[record.run_id] = record
        self.original.context["lock_recovery_run_id"] = record.run_id
        return record

    def ready_recovery(self):
        self.original.status = "unknown"
        self.original.result = None
        self.original.error = None
        maintenance = self.maintenance()
        del self.original.context["lock_recovery_run_id"]
        self.enterContext(patch.object(pipeline_runner._CURRENT, "record", maintenance, create=True))
        self.launch.return_value = (0, "", "")
        self.ssh.return_value = SimpleNamespace(returncode=0, stdout="0", stderr="")
        self.pull.return_value = json.dumps(self.completed()).encode()
        return maintenance

    def store_closure(self, proof="both"):
        self.original.status = "failed"
        self.original.result = {"no_task_claim": True}
        self.original.error = {}
        if proof in ("both", "report"):
            self.original.result["lock_recovery"] = self.completed()
        if proof in ("both", "diagnostic"):
            self.original.error = {"result": {"lock_recovery_sha256": self.completed()["record_sha256"]}}
        self.original._persist()

    def test_inspect_uses_only_bound_native_argv(self):
        self.ssh.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(self.report), stderr="")
        result = lockctl.inspect("p", "operator", self.original.run_id)
        self.assertTrue(result["recovery_eligible"])
        self.assertEqual(result["execution_run_id"], self.original.run_id)
        self.assertEqual(self.ssh.call_args.args[1], ["/usr/bin/env", "OPU_PLAN_STATE_DIR=/opt/opu/var/webapp-plans", "/opt/opu/bin/opu-database-lock-recover",
                                                    "inspect", "--plan-id", "p", "--task-id", "003-validate-source", "--run-id", "b" * 32, "--actor", "operator"])
        self.launch.assert_not_called()

    def test_blocked_report_is_a_completed_inspection(self):
        report = seal({**self.report, "status": "blocked", "recovery_eligible": False, "blockers": ["unknown process"]})
        self.ssh.return_value = SimpleNamespace(returncode=65, stdout=json.dumps(report), stderr="")
        self.assertEqual(lockctl.inspect("p", "operator", self.original.run_id)["blockers"], ["unknown process"])

    def test_host_path_actor_and_task_changes_stop_before_network(self):
        for field, value in (("remote_root", "/changed"), ("remote_run_dir", "/opt/opu/../../bad"), ("host_id", "other")):
            with self.subTest(field=field), patch.dict(self.original.context, {field: value}):
                with self.assertRaises(lockctl.LockError): self.scope()
        with self.assertRaises(lockctl.LockError): lockctl.inspect("p", "wrong-actor", self.original.run_id)
        self.native.return_value = {"status": "running", "stage": "validate"}
        with self.assertRaises(lockctl.LockError): self.scope()
        self.ssh.assert_not_called()
        self.sync.assert_not_called()

    def test_audit_tamper_and_cross_run_rejected(self):
        for report in ({**self.report, "actor": "other"}, seal({**self.report, "run_id": "f" * 32}), seal({**self.report, "plan_sha256": "0" * 64})):
            with self.assertRaises(lockctl.LockError): lockctl._verify_report(report, self.scope())

    def test_completed_requires_health_inode_and_pending_proof(self):
        report = self.completed()
        lockctl._verify_report(report, self.scope(), completed=True)
        for key, value in (("lock_released", False), ("original_task_status", "succeeded"), ("lock_inode_preserved", False), ("service_health", {})):
            with self.subTest(key=key), self.assertRaises(lockctl.LockError):
                lockctl._verify_report(seal({**report, key: value}), self.scope(), completed=True)

    def test_unknown_maintenance_never_relaunches_or_releases_original(self):
        maintenance = self.maintenance()
        self.ssh.return_value = SimpleNamespace(returncode=1, stdout="", stderr="missing")
        outcome = lockctl.reconcile_execution_run({**self.original.to_json(), "reconciliation_actor": "operator"})
        self.assertEqual(outcome["status"], "unknown")
        self.assertEqual(lockctl.reconcile_detached_run({**maintenance.to_json(), "reconciliation_actor": "operator"})["status"], "unknown")
        self.launch.assert_not_called()
        self.pull.assert_not_called()

    def test_verified_recovery_closes_only_rejected_launch(self):
        self.maintenance()
        self.ssh.return_value = SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        self.pull.return_value = json.dumps(self.completed()).encode()
        outcome = lockctl.reconcile_execution_run({**self.original.to_json(), "reconciliation_actor": "operator"})
        self.assertEqual(outcome["status"], "failed")
        self.assertTrue(outcome["result"]["no_task_claim"])
        self.assertEqual(outcome["error"]["result"]["task_status"], "pending")
        self.launch.assert_not_called()
        self.assertTrue(all(call.args[0][0] == "task-status" for call in self.native.call_args_list))

    def test_recovery_submission_is_durable_and_cannot_retry(self):
        maintenance = self.maintenance()
        with self.assertRaises(lockctl.LockError):
            lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        self.launch.assert_not_called()
        del self.original.context["lock_recovery_run_id"]
        self.launch.side_effect = lockctl.LockError("connection lost")
        with self.assertRaises(lockctl.LockError):
            lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        persisted = json.loads((Path(self.temp.name) / self.original.run_id / "run.json").read_text())
        self.assertEqual(persisted["context"]["lock_recovery_run_id"], maintenance.run_id)
        self.assertEqual(self.original.status, "unknown")

    def test_tool_sync_failure_leaves_original_available_for_safe_recovery(self):
        maintenance = self.ready_recovery()
        self.sync.side_effect = lockctl.LockError("installation connection failed")
        with self.assertRaises(lockctl.LockError):
            lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        self.assertNotIn("lock_recovery_run_id", self.original.context)
        self.launch.assert_not_called()
        self.sync.side_effect = None
        outcome = lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        self.assertEqual(outcome["status"], "completed")
        self.launch.assert_called_once()

    def test_tool_sync_cannot_continue_after_scope_or_maintenance_owner_changes(self):
        for changed in ("task", "maintenance"):
            with self.subTest(changed=changed):
                maintenance = self.ready_recovery()
                self.native.return_value = {"status": "pending", "stage": "validate"}
                def alter(_host):
                    if changed == "task":
                        self.native.return_value = {"status": "running", "stage": "validate"}
                    else:
                        maintenance.status = "failed"
                self.sync.side_effect = alter
                with self.assertRaises(lockctl.LockError):
                    lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
                self.assertNotIn("lock_recovery_run_id", self.original.context)
                self.launch.assert_not_called()

    def test_maintenance_reconcile_after_original_closed_survives_controller_restart(self):
        maintenance = self.maintenance()
        self.original.status = "failed"
        self.original.result = {"no_task_claim": True}
        self.plan["state"] = "succeeded"
        self.native.return_value = {"status": "succeeded", "stage": "validate"}
        self.ssh.return_value = SimpleNamespace(returncode=0, stdout="0", stderr="")
        self.pull.return_value = json.dumps(self.completed()).encode()
        self.assertEqual(lockctl.reconcile_detached_run({**maintenance.to_json(), "reconciliation_actor": "operator"})["status"], "succeeded")

    def test_successful_recovery_reconciles_original_without_claiming_task(self):
        maintenance = self.maintenance()
        del self.original.context["lock_recovery_run_id"]
        self.enterContext(patch.object(pipeline_runner._CURRENT, "record", maintenance, create=True))
        self.launch.return_value = (0, "", "")
        self.ssh.return_value = SimpleNamespace(returncode=0, stdout="0", stderr="")
        self.pull.return_value = json.dumps(self.completed()).encode()
        outcome = lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(self.original.status, "failed")
        self.assertTrue(self.original.result["no_task_claim"])
        self.assertTrue(maintenance.context["detached_terminal"])
        self.assertTrue(all(call.args[0][0] == "task-status" for call in self.native.call_args_list))

    def test_completed_recovery_does_not_hide_unknown_original_reconciliation(self):
        maintenance = self.maintenance()
        del self.original.context["lock_recovery_run_id"]
        self.enterContext(patch.object(pipeline_runner._CURRENT, "record", maintenance, create=True))
        self.launch.return_value = (0, "", "")
        self.pull.return_value = json.dumps(self.completed()).encode()
        with patch.object(pipeline_runner, "reconcile_run", return_value={"status": "unknown"}):
            with self.assertRaisesRegex(lockctl.LockError, "original launch still needs reconciliation"):
                lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        self.assertEqual(self.original.status, "unknown")

    def test_recovery_accepts_ui_closure_before_its_reconciliation_check(self):
        maintenance = self.ready_recovery()
        actual_reconcile = pipeline_runner.reconcile_run

        def ui_finishes_first(*_args, **_kwargs):
            actual_reconcile(self.original.run_id, actor="operator", inspect=lockctl.reconcile_execution_run)
            return (0, "", "")

        self.launch.side_effect = ui_finishes_first
        with patch.object(pipeline_runner, "reconcile_run") as redundant:
            outcome = lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        redundant.assert_not_called()
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(self.original.status, "failed")
        self.assertEqual(self.original.result["lock_recovery"]["record_sha256"], outcome["record_sha256"])
        self.launch.assert_called_once()

    def test_recovery_accepts_exact_ui_closure_after_its_read_before_registry_lock(self):
        maintenance = self.ready_recovery()
        actual_reconcile = pipeline_runner.reconcile_run

        def ui_wins_registry_lock(*args, **kwargs):
            actual_reconcile(*args, **kwargs)
            raise pipeline_runner.RunConflict("UI already reconciled this run", self.original.run_id)

        with patch.object(pipeline_runner, "reconcile_run", side_effect=ui_wins_registry_lock) as reconciler:
            outcome = lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        reconciler.assert_called_once()
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(self.original.status, "failed")
        self.assertEqual(self.original.context["lock_recovery_run_id"], maintenance.run_id)
        self.launch.assert_called_once()

    def test_existing_closure_accepts_either_persisted_recovery_digest(self):
        for proof in ("report", "diagnostic"):
            with self.subTest(proof=proof):
                maintenance = self.ready_recovery()

                def close_before_check(*_args, **_kwargs):
                    self.store_closure(proof)
                    return (0, "", "")

                self.launch.side_effect = close_before_check
                with patch.object(pipeline_runner, "reconcile_run") as redundant:
                    outcome = lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
                redundant.assert_not_called()
                self.assertEqual(outcome["status"], "completed")

    def test_conflict_never_accepts_unrelated_terminal_or_unknown_result(self):
        for mismatch in ("succeeded", "unknown", "missing-proof", "wrong-proof", "wrong-maintenance",
                         "claimed", "numeric-no-claim", "contradicting-proof", "wrong-run", "wrong-key"):
            with self.subTest(mismatch=mismatch):
                maintenance = self.ready_recovery()

                def unrelated_closure(*_args, **_kwargs):
                    self.store_closure()
                    if mismatch in ("succeeded", "unknown"):
                        self.original.status = mismatch
                    elif mismatch == "missing-proof":
                        self.original.result = {"no_task_claim": True}
                        self.original.error = None
                    elif mismatch == "wrong-proof":
                        self.original.result["lock_recovery"]["record_sha256"] = "0" * 64
                        self.original.error["result"]["lock_recovery_sha256"] = "0" * 64
                    elif mismatch == "wrong-maintenance":
                        self.original.context["lock_recovery_run_id"] = "f" * 12
                    elif mismatch == "claimed":
                        self.original.result["no_task_claim"] = False
                    elif mismatch == "numeric-no-claim":
                        self.original.result["no_task_claim"] = 1
                    elif mismatch == "contradicting-proof":
                        self.original.error["result"]["lock_recovery_sha256"] = "0" * 64
                    elif mismatch == "wrong-run":
                        self.original.run_id = "f" * 12
                    elif mismatch == "wrong-key":
                        self.original.key = "plan:other:execute"
                    self.original._persist()
                    raise pipeline_runner.RunConflict("Another request changed this run")

                try:
                    with patch.object(pipeline_runner, "reconcile_run", side_effect=unrelated_closure):
                        with self.assertRaisesRegex(lockctl.LockError, "original launch still needs reconciliation") as caught:
                            lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
                    self.assertEqual(caught.exception.result["lock_recovery"]["record_sha256"], self.completed()["record_sha256"])
                finally:
                    self.original.run_id = "a" * 12
                    self.original.key = "plan:p:execute"

    def test_native_recovery_failure_retains_verified_stdout_diagnosis(self):
        maintenance = self.maintenance()
        del self.original.context["lock_recovery_run_id"]
        report = seal({**self.report, "status": "recovery_required", "blockers": ["unknown holder appeared"]})
        self.launch.return_value = (65, json.dumps(report), "")
        with self.assertRaises(lockctl.LockError) as caught:
            lockctl.recover("p", "operator", self.original.run_id, maintenance_run_id=maintenance.run_id)
        self.assertIn("unknown holder appeared", caught.exception.stderr)
        self.assertEqual(caught.exception.result["record_sha256"], report["record_sha256"])
        self.assertEqual(self.original.status, "unknown")

    def test_ordinary_reconcile_stays_on_existing_path(self):
        with patch.object(lockctl.planctl, "reconcile_detached_run", return_value={"status": "unknown"}) as original:
            self.assertEqual(lockctl.reconcile_execution_run(self.original.to_json()), {"status": "unknown"})
            original.assert_called_once()


if __name__ == "__main__":
    unittest.main()
