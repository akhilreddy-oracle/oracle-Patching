#!/usr/bin/env python3
"""Read-only provenance bridge boundaries; no SSH, sockets, or Oracle calls."""
from copy import deepcopy
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from runtime_fixture import runtime_receipt, host_runtime_receipts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import auth
import extjobctl
import pipeline_runner
import server


def seal(value):
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    return {**body, "record_sha256": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                              ensure_ascii=True).encode()).hexdigest()}


class ExtjobBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-extjob-bridge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(pipeline_runner, "RUNS_DIR", self.root / "runs"))
        self.enterContext(patch.object(pipeline_runner, "RUNS", {}))
        self.enterContext(patch.object(pipeline_runner, "_ACTIVE_KEYS", {}))
        self.enterContext(patch.object(pipeline_runner.notifications, "emit"))
        self.run = pipeline_runner.RunRecord("a" * 12, "extjob_inspect", "plan:p:execute")
        self.run.status = "running"
        pipeline_runner.RUNS[self.run.run_id] = self.run
        pipeline_runner._ACTIVE_KEYS[self.run.key] = self.run.run_id
        self.enterContext(patch.object(extjobctl.planctl, "PLAN_STATE_DIR", self.root / "plans"))
        self.enterContext(patch.object(extjobctl, "REFERENCE_DIR", self.root / "references"))
        extjobctl.REFERENCE_DIR.mkdir(mode=0o700)
        self.reference = {"plan_sha256": "b" * 64, "database": "ORCL",
                          "archive_path": "/u02/opu-backup/ORCL/retained/oracle-home/dbhome_1.tar.gz", "archive_sha256": "c" * 64}
        self.reference_path = extjobctl.REFERENCE_DIR / "p.json"
        self.write_reference()
        self.plan = {"plan_id": "p", "plan_sha256": "b" * 64, "state": "running", "intent": "patch_apply",
                     "procedure": {"adapter": "database_single_instance_opatch"}, "nodes": ["source"],
                     "target": {"database_unique_name": "ORCL", "oracle_home": "/u01/dbhome_1", "owner": "oracle"}}
        self.authorization = seal({"plan_id": "p", "plan_sha256": "b" * 64, "decision": "execution_authorized", "actor": "operator"})
        self.auth_path = extjobctl.planctl.PLAN_STATE_DIR / "plans/p/authorization.json"
        self.auth_path.parent.mkdir(parents=True)
        self.auth_path.write_text(json.dumps(self.authorization))
        self.tasks = {task_id: {"plan_id": "p", "plan_sha256": "b" * 64, "task_id": task_id, "stage": stage,
                                "status": status, "evidence_sha256": "d" * 64}
                      for task_id, stage, status in (("001-precheck-source", "precheck", "succeeded"),
                                                    ("002-apply-source", "apply", "succeeded"),
                                                    ("003-validate-source", "validate", "succeeded"),
                                                    ("004-datapatch-local", "datapatch", "succeeded"),
                                                    ("005-final-validate-local", "final_validate", "pending"))}
        self.task_dir = self.auth_path.parent / "tasks"
        self.task_dir.mkdir()
        for task_id in self.tasks:
            (self.task_dir / (task_id + ".json")).write_text("{}")
        self.native = self.enterContext(patch.object(extjobctl.planctl, "_run", side_effect=self.native_status))
        self.host = {"id": "source", "node_name": "source", "ssh_alias": "source", "remote_root": "/opt/opu", "sudo": True}
        self.resolve = self.enterContext(patch.object(extjobctl.planctl, "_resolve_node_host", return_value=self.host))
        self.sync = self.enterContext(patch.object(extjobctl.tools_sync, "ensure_host_tools", side_effect=host_runtime_receipts))
        self.ssh = self.enterContext(patch.object(extjobctl.remote, "run_remote_raw"))
        self.detached = self.enterContext(patch.object(extjobctl.planctl, "_run_detached_remote"))
        self.report = seal({"schema_version": "1.0", "collector": {"name": "oracle.extjob.provenance", "version": "1"},
                            "status": "inspected", "read_only": True, "mutation_authorized": False,
                            "plan_id": "p", "plan_sha256": "b" * 64, "actor": "operator", "observed_at": "2026-09-14T20:00:00Z",
                            "target": {**self.plan["target"], "oracle_sid": "ORCL", "listener": "LISTENER"},
                            "requested_archive": {"path": self.reference["archive_path"], "sha256": "c" * 64},
                            "authority": {"authorization_sha256": hashlib.sha256(self.auth_path.read_bytes()).hexdigest(),
                                          "authorization_record_sha256": self.authorization["record_sha256"],
                                          "native_apply_evidence_sha256": "d" * 64,
                                          "native_target_binding_record_sha256": "e" * 64,
                                          "native_datapatch_evidence_sha256": "d" * 64,
                                          "plan_state": "running", "final_task_id": "005-final-validate-local",
                                          "final_task_status": "pending", "final_task_result_sha256": None},
                            "archive": {"path": self.reference["archive_path"], "sha256": "c" * 64, "expected_sha256": "c" * 64,
                                        "size": 1024, "metadata": {"uid": 0, "gid": 0, "mode": "0600", "size": 1024, "identity": [1, 2], "nlink": 1},
                                        "members_checked": 3, "same_open_descriptor_verified": True, "verified": True},
                            "member": {"name": "dbhome_1/bin/extjob", "sha256": "f" * 64, "size": 42, "uid": 0, "gid": 54321,
                                       "mode": "4750", "regular": True, "unique": True},
                            "current": {"path": "/u01/dbhome_1/bin/extjob", "sha256": "f" * 64, "size": 42, "uid": 54321, "gid": 54321,
                                        "mode": "0755", "regular": True, "nlink": 1, "identity": [1, 3], "same_open_descriptor_verified": True},
                            "bytes_match": True, "reference_root_setuid": True, "reference_kind": "retained_backup_audit_digest"})
        self.respond(self.report)

    def write_reference(self):
        self.reference_path.write_text(json.dumps(self.reference))
        self.reference_path.chmod(0o600)

    def native_status(self, args):
        return deepcopy(self.plan if args[0] == "status" else self.tasks[args[-1]])

    def respond(self, report, rc=0):
        self.ssh.return_value = SimpleNamespace(returncode=rc, stdout=json.dumps(report), stderr="")

    def inspect(self):
        return extjobctl.inspect("p", "operator", inspection_run_id=self.run.run_id)

    def scope(self):
        return extjobctl._scope("p", "operator", self.run.run_id)

    def test_fixed_argv_reference_and_no_task_or_permission_mutation(self):
        self.assertEqual(self.inspect(), self.report)
        self.ssh.assert_called_once_with("source", ["/usr/bin/env", "OPU_PLAN_STATE_DIR=/opt/opu/var/webapp-plans",
            runtime_receipt("source", "/opt/opu")["runtime_root"] + "/bin/opu-extjob-provenance-inspect", "--plan-id", "p", "--actor", "operator",
            "--archive", self.reference["archive_path"], "--archive-sha256", "c" * 64], timeout=900, sudo=True)
        self.assertTrue(all(call.args[0][0] in {"status", "task-status"} for call in self.native.call_args_list))
        self.detached.assert_not_called()
        self.assertFalse(self.run.context.get("detached_execution"))
        self.assertEqual(self.tasks["005-final-validate-local"]["status"], "pending")

    def test_blocked_archive_retains_bound_current_digest_as_normal_result(self):
        report = seal({**self.report, "status": "blocked", "archive": {"path": self.reference["archive_path"],
                       "expected_sha256": "c" * 64, "verified": False, "metadata_unverified": True,
                       "metadata": {"uid": 54321, "mode": "0600"}}, "bytes_match": None, "reference_root_setuid": None,
                       "error": "archive is not root-owned"})
        report.pop("member")
        report = seal(report)
        self.respond(report, 65)
        result = self.inspect()
        self.assertEqual((result["status"], result["current"]["sha256"]), ("blocked", "f" * 64))
        self.assertIs(result["mutation_authorized"], False)

    def test_early_blocked_requires_request_archive_binding(self):
        report = seal({key: self.report[key] for key in ("schema_version", "read_only", "mutation_authorized", "plan_id", "actor", "requested_archive")}
                      | {"status": "blocked", "error": "sealed native authorization unavailable"})
        self.respond(report, 65)
        self.assertEqual(self.inspect(), report)
        report.pop("requested_archive")
        self.respond(seal(report), 65)
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()

    def test_wrong_owner_or_active_execute_prevents_all_sync_and_ssh(self):
        for field, value in (("kind", "execute"), ("key", "plan:other:execute"), ("status", "succeeded"),
                             ("context", {"detached_execution": True})):
            with self.subTest(field=field), patch.object(self.run, field, value):
                with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        with patch.dict(pipeline_runner._ACTIVE_KEYS, {"plan:p:execute": "0" * 12}):
            with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        with self.assertRaises(extjobctl.ExtjobError):
            extjobctl.inspect("p", "operator", inspection_run_id="1" * 12)
        self.sync.assert_not_called()
        self.ssh.assert_not_called()

    def test_plan_actor_and_task_scope_fail_closed_before_sync(self):
        for field, value in (("state", "succeeded"), ("intent", "patch_rollback"), ("nodes", ["source", "other"]),
                             ("plan_id", "other"), ("procedure", {"adapter": "grid_opatchauto"})):
            with self.subTest(field=field), patch.dict(self.plan, {field: value}):
                with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        with self.assertRaises(extjobctl.ExtjobError):
            extjobctl.inspect("p", "someone", inspection_run_id=self.run.run_id)
        for task_id in self.tasks:
            with self.subTest(task_id=task_id), patch.dict(self.tasks[task_id], {"status": "running"}):
                with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.sync.assert_not_called()
        self.ssh.assert_not_called()

    def test_missing_changed_or_unprotected_reference_is_never_sent(self):
        for field, value in (("plan_sha256", "0" * 64), ("database", "OTHER"), ("archive_sha256", "bogus"),
                             ("archive_path", "/u02/opu-backup/OTHER/retained/oracle-home/dbhome_1.tar.gz"),
                             ("archive_path", "/u02/opu-backup/ORCL/../ORCL/retained/oracle-home/dbhome_1.tar.gz"),
                             ("archive_path", "/u02/opu-backup/ORCL/retained/oracle-home/other.tar.gz"), ("shell", "bad")):
            with self.subTest(field=field), patch.dict(self.reference, {field: value}):
                self.write_reference()
                with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.write_reference()
        self.reference_path.chmod(0o666)
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.reference_path.unlink()
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.reference_path.symlink_to(self.auth_path)
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.sync.assert_not_called()
        self.ssh.assert_not_called()

    def test_tampered_or_cross_scope_native_reports_never_accepted(self):
        scope = self.scope()
        cases = [("plan_id", "other"), ("plan_sha256", "0" * 64), ("actor", "other"), ("mutation_authorized", True),
                 ("read_only", False), ("target", {**self.report["target"], "oracle_home": "/other"}),
                 ("requested_archive", {**self.report["requested_archive"], "sha256": "0" * 64}),
                 ("archive", {**self.report["archive"], "sha256": "0" * 64}),
                 ("authority", {**self.report["authority"], "authorization_sha256": "0" * 64}),
                 ("authority", {**self.report["authority"], "final_task_id": "other"}),
                 ("authority", {**self.report["authority"], "native_datapatch_evidence_sha256": "0" * 64}),
                 ("authority", {**self.report["authority"], "final_task_status": "failed"}),
                 ("current", {**self.report["current"], "path": "/another/extjob"}),
                 ("member", {**self.report["member"], "name": "other/bin/extjob"}),
                 ("bytes_match", False), ("reference_root_setuid", False)]
        for field, value in cases:
            with self.subTest(field=field), self.assertRaises(extjobctl.ExtjobError):
                extjobctl._verify_report(seal({**self.report, field: value}), scope, 0)
        with self.assertRaises(extjobctl.ExtjobError):
            extjobctl._verify_report({**self.report, "observed_at": "changed"}, scope, 0)

    def test_inspected_mismatch_is_valid_read_only_evidence(self):
        self.respond(seal({**self.report, "member": {**self.report["member"], "sha256": "0" * 64, "mode": "0750"},
                           "bytes_match": False, "reference_root_setuid": False}))
        self.assertIs(self.inspect()["bytes_match"], False)

    def test_scope_change_during_sync_prevents_native_call(self):
        def change(_host):
            self.tasks["005-final-validate-local"]["status"] = "running"
        self.sync.side_effect = change
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.ssh.assert_not_called()

    def test_paused_failed_final_validation_is_inspectable_without_window_or_state_changes(self):
        self.plan.update(state="paused", maintenance_window={"start": "2020-01-01T00:00:00Z", "end": "2020-01-01T01:00:00Z"})
        self.tasks["005-final-validate-local"].update(status="failed", task_result_sha256="9" * 64)
        self.report = seal({**self.report, "authority": {**self.report["authority"], "plan_state": "paused",
                           "final_task_status": "failed", "final_task_result_sha256": "9" * 64}})
        self.respond(self.report)
        original = deepcopy((self.plan, self.tasks))
        result = self.inspect()
        self.assertFalse(result["mutation_authorized"])
        self.assertEqual((self.plan, self.tasks), original)
        self.assertEqual(result["authority"]["final_task_status"], "failed")
        self.assertTrue(all(call.args[0][0] in {"status", "task-status"} for call in self.native.call_args_list))

    def test_paused_unknown_unsealed_or_extra_work_is_rejected_before_ssh(self):
        self.plan["state"] = "paused"
        final = self.tasks["005-final-validate-local"]
        for status in ("pending", "running", "unknown", "succeeded"):
            final["status"] = status
            with self.subTest(status=status), self.assertRaises(extjobctl.ExtjobError): self.inspect()
        final["status"] = "failed"
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        final["task_result_sha256"] = "9" * 64
        self.tasks["003-validate-source"]["status"] = "unknown"
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.tasks["003-validate-source"]["status"] = "succeeded"
        (self.task_dir / "006-extra.json").write_text("{}")
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()
        self.ssh.assert_not_called()

    def test_failed_final_result_changed_during_collection_invalidates_report(self):
        self.plan["state"] = "paused"
        final = self.tasks["005-final-validate-local"]
        final.update(status="failed", task_result_sha256="9" * 64)
        report = seal({**self.report, "authority": {**self.report["authority"], "plan_state": "paused",
                       "final_task_status": "failed", "final_task_result_sha256": "9" * 64}})
        def change(*_args, **_kwargs):
            final["task_result_sha256"] = "8" * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")
        self.ssh.side_effect = change
        with self.assertRaisesRegex(extjobctl.ExtjobError, "scope changed"):
            self.inspect()

    def test_changed_reference_after_collection_invalidates_report(self):
        def change(*_args, **_kwargs):
            self.reference["archive_sha256"] = "0" * 64
            self.write_reference()
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.report), stderr="")
        self.ssh.side_effect = change
        with self.assertRaises(extjobctl.ExtjobError): self.inspect()

    def test_bad_json_and_exit_status_cannot_be_success(self):
        for rc, stdout in ((1, json.dumps(self.report)), (65, json.dumps(self.report)), (0, "[]"), (0, "{}"),
                           (0, '{"x":1,"x":2}'), (0, '{"x":NaN}')):
            self.ssh.return_value = SimpleNamespace(returncode=rc, stdout=stdout, stderr="native diagnostic")
            with self.subTest(rc=rc, stdout=stdout[:20]), self.assertRaises(extjobctl.ExtjobError): self.inspect()

    def test_inspection_reservation_blocks_execute_and_failure_is_not_unknown(self):
        with self.assertRaises(pipeline_runner.RunConflict):
            pipeline_runner.start_run("execute", "plan:p:execute", lambda _record: self.fail("must not launch"))
        pipeline_runner.RUNS.clear()
        pipeline_runner._ACTIVE_KEYS.clear()

        class InlineThread:
            def __init__(self, *, target, **_kwargs): self.target = target
            def start(self): self.target()

        self.ssh.side_effect = extjobctl.ExtjobError("read-only SSH timed out")
        with patch.object(pipeline_runner.threading, "Thread", InlineThread):
            record = pipeline_runner.start_run("extjob_inspect", "plan:p:execute", lambda current:
                extjobctl.inspect("p", "operator", inspection_run_id=current.run_id))
        self.assertEqual(record.status, "failed")
        self.assertFalse(record.context.get("detached_execution"))
        self.assertIsNone(pipeline_runner.active_run_id("plan:p:execute"))


class ExtjobRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-extjob-route-")
        self.addCleanup(self.temp.cleanup)
        principal_file = Path(self.temp.name) / "principals.json"
        principal_file.write_text(json.dumps({"principals": [
            {"actor": name, "roles": [role], "token_sha256": hashlib.sha256((name + "-token").encode()).hexdigest()}
            for name, role in (("operator", "operator"), ("viewer", "viewer"))]}))
        self.enterContext(patch.dict(os.environ, {"OPU_WEBAPP_PRINCIPALS_FILE": str(principal_file), "OPU_WEBAPP_RBAC": "1",
                                                "OPU_PRODUCTION_MODE": "0", "OPU_WEBAPP_TOKEN": "unused"}))
        self.enterContext(patch.object(auth, "_CACHED_TOKEN", None))
        self.start = self.enterContext(patch.object(pipeline_runner, "start_run", return_value=SimpleNamespace(run_id="a" * 12)))
        self.inspect = self.enterContext(patch.object(extjobctl, "inspect", return_value={"status": "blocked"}))

    def request(self, body=None, *, actor="operator", path="/api/plans/p/extjob-inspect"):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.headers = Message()
        handler.headers["Authorization"] = "Bearer " + actor + "-token"
        data = json.dumps(body or {}).encode()
        handler.headers["Content-Length"] = str(len(data))
        handler.rfile = io.BytesIO(data)
        result = []
        handler._send_json = lambda status, payload: result.append((status, payload))
        handler.do_POST()
        return result[-1]

    def test_route_binds_actor_and_reserves_execute_key(self):
        self.assertEqual(self.request(), (202, {"run_id": "a" * 12}))
        kind, key, callback = self.start.call_args.args
        self.assertEqual((kind, key), ("extjob_inspect", "plan:p:execute"))
        callback(SimpleNamespace(run_id="a" * 12))
        self.inspect.assert_called_once_with("p", "operator", inspection_run_id="a" * 12)

    def test_route_rejects_request_archive_paths_and_unknown_fields(self):
        for body in ({"archive": "/tmp/archive"}, {"archive_sha256": "a" * 64}, {"command": "true"},
                     {"requester": "operator"}, {"run_id": "a" * 12}, {"actor": 3}):
            with self.subTest(body=body): self.assertEqual(self.request(body)[0], 400)
        for path in ("/api/plans/../extjob-inspect", "/api/plans/p/extra/extjob-inspect", "/api/plans/%2f/extjob-inspect"):
            with self.subTest(path=path): self.assertEqual(self.request(path=path)[0], 400)
        self.start.assert_not_called()

    def test_viewer_and_forged_actor_cannot_start_inspection(self):
        self.assertEqual(self.request(actor="viewer")[0], 403)
        self.assertEqual(self.request({"actor": "viewer"})[0], 403)
        self.start.assert_not_called()

    def test_existing_active_execution_returns_conflict_without_inspection(self):
        self.start.side_effect = pipeline_runner.RunConflict("existing execute", run_id="b" * 12)
        status, result = self.request({"actor": "operator"})
        self.assertEqual((status, result["run_id"]), (409, "b" * 12))
        self.inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
