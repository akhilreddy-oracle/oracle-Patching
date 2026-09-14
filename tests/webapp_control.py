#!/usr/bin/env python3
"""Control-plane boundary regressions. No sockets, SSH, Oracle or webhooks."""
import hashlib
from email.message import Message
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import tarfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import auth
import evidence
import pipeline_runner
import planctl
import recoveryctl
import recovery_fixtures
import server


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-control-tests-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.principals = self.root / "principals.json"
        entries = [
            {"actor": name, "roles": [role], "token_sha256": hashlib.sha256((name + "-token").encode()).hexdigest()}
            for name, role in [("viewer", "viewer"), ("requester", "requester"), ("approver", "approver"), ("operator", "operator")]
        ]
        self.principals.write_text(json.dumps({"principals": entries}))
        self.enterContext(patch.dict(os.environ, {
            "OPU_WEBAPP_PRINCIPALS_FILE": str(self.principals), "OPU_WEBAPP_RBAC": "1",
            "OPU_PRODUCTION_MODE": "0", "OPU_WEBAPP_TOKEN": "shared-lab-token", "OPU_ITSM_REQUIRED": "0",
        }))
        self.enterContext(patch.object(auth, "_CACHED_TOKEN", None))
        self.enterContext(patch.object(pipeline_runner, "RUNS_DIR", self.root / "runs"))
        self.enterContext(patch.object(pipeline_runner, "RUNS", {}))
        self.enterContext(patch.object(pipeline_runner, "_ACTIVE_KEYS", {}))
        self.enterContext(patch.object(pipeline_runner.notifications, "emit"))
        self.enterContext(patch.object(planctl, "PLAN_STATE_DIR", self.root / "plans"))
        self.enterContext(patch.object(recoveryctl, "RECOVERY_DIR", self.root / "recovery"))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "hosts"))

    def request(self, path, *, actor="operator", body=None, raw=None, length=None, method="POST", extra_headers=None):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.headers = Message()
        handler.headers["Authorization"] = "Bearer " + actor + "-token"
        content = raw if raw is not None else json.dumps(body or {}).encode()
        handler.headers["Content-Length"] = str(len(content) if length is None else length)
        for key, value in (extra_headers or {}).items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(content)
        handler.wfile = io.BytesIO()
        result = []
        handler._send_json = lambda status, payload: result.append((status, payload))
        handler.send_response = lambda status: result.append((status, None))
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda status: result.append((status, None))
        handler._resolved_host = lambda host_id: {"id": host_id}
        getattr(handler, "do_" + method)()
        return result[-1]

    def test_identity_is_bound_to_credential(self):
        self.assertEqual(auth.require_api_auth("Bearer approver-token"), "approver")
        with self.assertRaises(auth.AuthError):
            auth.require_api_auth("Bearer shared-lab-token")
        self.assertEqual(self.request("/api/plans/p/approve", actor="approver", body={"actor": "operator"})[0], 403)
        self.assertEqual(self.request("/api/session", actor="approver", method="GET", extra_headers={"X-OPU-Actor": "operator"})[0], 403)
        status, payload = self.request("/api/session", actor="approver", method="GET")
        self.assertEqual((status, payload["actor"], payload["roles"]), (200, "approver", ["approver"]))

    def test_procedure_hints_route_reads_selected_readme_without_starting_work(self):
        hint = {"required_opatch_version": "12.2.0.1.49", "readme_identifier": "README notes.html"}
        with patch.object(server.procedure_hints, "get_hints", return_value=hint) as read, \
             patch.object(pipeline_runner, "start_run") as start:
            status, payload = self.request("/api/hosts/h/procedure-hints?readme_identifier=README%20notes.html", actor="viewer", method="GET")
            self.assertEqual((status, payload), (200, hint))
            read.assert_called_once_with("h", {"id": "h"}, "README notes.html")
            start.assert_not_called()
            read.reset_mock()
            self.assertEqual(self.request("/api/hosts/h/procedure-hints?readme_identifier=a&readme_identifier=b", method="GET")[0], 400)
            read.assert_not_called()
        with patch.object(server.procedure_hints, "get_hints", side_effect=server.remote.RemoteError("readme_changed", "Run artifact inspection again")):
            status, payload = self.request("/api/hosts/h/procedure-hints?readme_identifier=README.html", method="GET")
            self.assertEqual((status, payload["error"]), (400, "readme_changed"))

    def test_failed_procedure_validation_cannot_keep_older_success_or_readiness(self):
        evidence.write_evidence("h", "artifact", {"artifact": {"status": "ready_for_catalog"}})
        for name in ("procedure", "compatibility", "compatibility_reconciliation", "readiness"):
            evidence.write_evidence("h", name, {"status": "ready_for_planning", "old": True})
        draft = {"required_opatch_version": "", "patch_id": "12345678"}
        error = server.pipeline_steps.localtools.LocalToolError("opu-procedure-validate", "Required OPatch is missing")
        with patch.object(server.pipeline_steps.localtools, "run_tool", side_effect=error):
            with self.assertRaises(type(error)):
                server.pipeline_steps.step_procedure_validate("h", {}, {"procedure": draft})
        for name in ("procedure", "compatibility", "compatibility_reconciliation", "readiness"):
            self.assertIsNone(evidence.read_evidence("h", name), name)
        state = next(s for s in server.pipeline_steps.pipeline_state("h") if s["step"] == "procedure-validate")
        self.assertFalse(state["done"])
        self.assertEqual(state["input"], draft)
        self.assertIsNotNone(evidence.read_evidence("h", "artifact"))

    def test_remote_read_limit_is_enforced_before_returning_content(self):
        result = subprocess.CompletedProcess([], 0, b"abcd", b"")
        with patch.object(server.remote.subprocess, "run", return_value=result) as run:
            with self.assertRaises(server.remote.RemoteError) as failure:
                server.remote.pull_file("fixture", "/stage/README notes.html", sudo=True, max_bytes=3)
            self.assertEqual(failure.exception.error, "remote_file_too_large")
            self.assertIn("sudo -n head -c 4 --", run.call_args.args[0][-1])
        with patch.object(server.remote.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"abc", b"")):
            self.assertEqual(server.remote.pull_file("fixture", "/stage/README.html", max_bytes=3), b"abc")

    def test_registry_damage_cannot_disable_rbac(self):
        os.environ.pop("OPU_WEBAPP_RBAC")
        for content in ("not-json", "[]", '{"principals":[]}', '{"principals":[null]}'):
            self.principals.write_text(content)
            self.assertTrue(auth.rbac_enabled())
            with self.assertRaises(auth.AuthError):
                auth.require_api_auth("Bearer shared-lab-token")
        self.principals.unlink()
        self.assertTrue(auth.rbac_enabled())
        with self.assertRaises(auth.AuthError):
            auth.require_api_auth("Bearer operator-token")

    def test_every_mutation_route_family_checks_roles(self):
        paths = ["/api/hosts/h/pipeline/discovery", "/api/recovery/r/approve", "/api/recovery/r/authorize", "/api/recovery/r/execute", "/api/agent/claim", "/api/runs/123456abcdef/reconcile", "/api/plans/p/execute-next"]
        with patch.object(pipeline_runner, "start_run") as start:
            for path in paths:
                self.assertEqual(self.request(path, actor="viewer")[0], 403, path)
            start.assert_not_called()
        with patch.object(pipeline_runner, "start_run", return_value=SimpleNamespace(run_id="123456abcdef")) as start:
            self.assertEqual(self.request("/api/hosts/h/pipeline/discovery")[0], 202)
            start.assert_called_once()

    def test_body_shape_size_and_numeric_fields(self):
        with patch.object(pipeline_runner, "start_run") as start:
            for raw in (b"[]", b"null", b'"text"', b'{"lease_seconds":{}}', b'{"actor":[]}', b'{"max_tasks":true}', b"{bad"):
                self.assertEqual(self.request("/api/hosts/h/pipeline/discovery", raw=raw)[0], 400, raw)
            self.assertEqual(self.request("/api/hosts/h/pipeline/discovery", raw=b"{}", length=1024 * 1024 + 1)[0], 400)
            self.assertEqual(self.request("/api/hosts/h/pipeline/discovery", raw=b"{}", length=-1)[0], 400)
            start.assert_not_called()

    def test_agent_credentials_and_fence_are_forwarded(self):
        with patch.object(server.agent_queue, "claim", return_value={"job_id": "p__t"}) as claim:
            self.assertEqual(self.request("/api/agent/claim", body={"agent_id": "operator", "node": "n", "agent_token": "secret"})[0], 200)
            self.assertEqual(claim.call_args.kwargs["agent_token"], "secret")
        with patch.object(server.agent_queue, "complete", return_value={}) as complete:
            self.assertEqual(self.request("/api/agent/complete", body={"agent_id": "operator", "job_id": "p__t", "agent_token": "secret", "claim_token": "fence"})[0], 200)
            self.assertEqual(complete.call_args.kwargs["claim_token"], "fence")
            self.assertEqual(complete.call_args.kwargs["agent_token"], "secret")
        with patch.object(server.agent_queue, "extend_lease", return_value={}) as renew:
            self.assertEqual(self.request("/api/agent/renew", body={"agent_id": "someone-else", "job_id": "p__t", "agent_token": "secret", "claim_token": "fence"})[0], 403)
            renew.assert_not_called()
            self.assertEqual(self.request("/api/agent/renew", body={"agent_id": "operator", "job_id": "p__t", "agent_token": "secret", "claim_token": "fence"})[0], 200)
            self.assertEqual(renew.call_args.kwargs["claim_token"], "fence")

    def test_recovery_cannot_escape_or_replace_fixture(self):
        victim = self.root / "victim"
        victim.mkdir()
        sentinel = victim / "sentinel"
        sentinel.write_text("preserve")
        with patch.object(recoveryctl, "_run", return_value={"request_id": "safe"}):
            for request_id in (str(victim), "../victim", "a/b", ".", ""):
                with self.assertRaises(recoveryctl.RecoveryError):
                    recoveryctl.create_testmode_demo(request_id, "requester")
            result = recoveryctl.create_testmode_demo("safe", "requester", host_id="host-a")
            self.assertEqual(result["host_id"], "host-a")
            with self.assertRaises(recovery_fixtures.FixtureError):
                recoveryctl.create_testmode_demo("safe", "requester")
        self.assertEqual(sentinel.read_text(), "preserve")
        link = recoveryctl.RECOVERY_DIR / "linked"
        link.symlink_to(victim, target_is_directory=True)
        with self.assertRaises(recoveryctl.RecoveryError):
            recoveryctl.create_testmode_demo("linked", "requester")
        self.assertEqual(sentinel.read_text(), "preserve")

    def orphan(self, *, context=None):
        record = pipeline_runner.RunRecord("123456abcdef", "plan", "plan:p:execute")
        record.status = "running"
        record.owner = {"pid": 2147483647, "instance": "dead-controller"}
        record.context = context or {}
        record._persist()
        return record

    def test_restart_unknown_blocks_relaunch_and_requires_reconciliation(self):
        old = self.orphan()
        current = pipeline_runner.get_run(old.run_id)
        self.assertEqual(current.status, "unknown")
        self.assertEqual(pipeline_runner.active_run_id(old.key), old.run_id)
        with self.assertRaises(pipeline_runner.RunConflict):
            pipeline_runner.start_run("plan", old.key, lambda record: {})
        with self.assertRaises(pipeline_runner.RunConflict):
            pipeline_runner.reconcile_run(old.run_id, actor="operator", inspect=lambda record: {})
        result = pipeline_runner.reconcile_run(old.run_id, actor="operator", inspect=lambda record: {}, confirm_no_active_execution=True, note="Verified no child execution remains")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(pipeline_runner.active_run_id(old.key))

    def test_remote_unknown_cannot_be_manually_cleared(self):
        old = self.orphan(context={"detached_execution": True})
        with patch.object(planctl, "reconcile_detached_run", return_value={"status": "unknown", "error": {"message": "RUNNING"}}) as inspect:
            result = pipeline_runner.reconcile_run(old.run_id, actor="operator", inspect=inspect, confirm_no_active_execution=True, note="override")
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(pipeline_runner.active_run_id(old.key), old.run_id)
        result = pipeline_runner.reconcile_run(old.run_id, actor="operator", inspect=lambda record: {"status": "succeeded", "result": {"verified": True}})
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(pipeline_runner.active_run_id(old.key))

    def test_failed_contact_keeps_durable_unknown_owner(self):
        def work(record):
            pipeline_runner.set_execution_context(detached_execution=True, detached_terminal=False)
            raise RuntimeError("contact lost")
        record = pipeline_runner.start_run("plan", "plan:p:execute", work)
        # Wait on the actual worker state, without invoking any external service.
        import time
        persisted = {}
        for _ in range(100):
            persisted = json.loads((pipeline_runner.RUNS_DIR / record.run_id / "run.json").read_text())
            if persisted.get("status") == "unknown":
                break
            time.sleep(0.01)
        self.assertEqual(record.status, "unknown")
        self.assertEqual(pipeline_runner.active_run_id(record.key), record.run_id)
        self.assertEqual(persisted["status"], "unknown")

    def test_second_controller_cannot_launch_owned_key(self):
        program = """
import sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pipeline_runner
pipeline_runner.RUNS_DIR = Path(sys.argv[2])
pipeline_runner.notifications.emit = lambda *args: None
record = pipeline_runner.start_run('plan', 'shared-key', lambda record: threading.Event().wait(30))
print(record.run_id, flush=True)
sys.stdin.readline()
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", program, str(Path(server.__file__).parent), str(pipeline_runner.RUNS_DIR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            run_id = child.stdout.readline().strip()
            self.assertRegex(run_id, r"^[a-f0-9]{12}$")
            with self.assertRaises(pipeline_runner.RunConflict):
                pipeline_runner.start_run("plan", "shared-key", lambda record: {})
            child.communicate("exit\n", timeout=5)
            self.assertEqual(pipeline_runner.get_run(run_id).status, "unknown")
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_custom_plan_names_use_explicit_host_identity(self):
        plan = {"plan_id": "custom-change-name", "created_at": "2026-09-14T00:00:00Z", "source_documents": {"readiness": {"path": str(evidence.VAR_DIR / "host-a" / "evidence" / "readiness.json")}}}
        self.assertEqual(planctl._with_host_identity(plan)["host_id"], "host-a")
        planctl._record_host(plan["plan_id"], "host-b")
        self.assertEqual(planctl._with_host_identity(plan)["host_id"], "host-b")
        self.assertIsNone(planctl._with_host_identity({"plan_id": "host-a-prefix-only"})["host_id"])
        with patch.object(planctl, "_run", return_value={"plan_id": "operator-chosen-name"}):
            created = planctl.create("operator-chosen-name", "requester", "host-c", "2026-09-14T00:00:00Z", "2026-09-14T01:00:00Z")
            self.assertEqual(created["host_id"], "host-c")

    def test_recovery_list_is_scoped_by_recorded_host(self):
        recoveryctl.RECOVERY_DIR.mkdir()
        for name, host in [("r1", "a"), ("r2", "b"), ("r3", None)]:
            directory = recoveryctl.RECOVERY_DIR / name
            directory.mkdir()
            (directory / "webapp-metadata.json").write_text(json.dumps({"host_id": host}))
        with patch.object(recoveryctl, "_run", side_effect=lambda request_id, args: {"request_id": request_id}):
            self.assertEqual([r["request_id"] for r in recoveryctl.list_requests(host_id="a")], ["r1"])
            self.assertEqual(len(recoveryctl.list_requests()), 3)

    def test_remote_reconciliation_inspects_existing_launch_only(self):
        host = {"id": "h", "node_name": "n", "ssh_alias": "alias", "remote_root": "/opt/opu"}
        context = {"plan_id": "p", "task_id": "t", "node": "n", "host_id": "h", "ssh_alias": "alias", "remote_root": "/opt/opu", "remote_run_dir": "/opt/opu/var/webapp-runs/p/t/" + "a" * 32}
        with patch.object(planctl, "_resolve_node_host", return_value=host), patch.object(planctl.remote, "run_remote_shell", return_value=SimpleNamespace(returncode=0, stdout="RC\n0\n")) as shell, patch.object(planctl.remote, "run_remote_raw", side_effect=[SimpleNamespace(returncode=0, stdout='{"status":"succeeded"}'), SimpleNamespace(returncode=0, stdout="")]), patch.object(planctl, "_sync_plan_from_host") as sync, patch.object(planctl, "status", return_value={"state": "running"}), patch.object(planctl, "_run", return_value={"status": "succeeded"}) as verified:
            result = planctl.reconcile_detached_run({"context": context})
            self.assertEqual(result["status"], "succeeded")
            self.assertNotIn("nohup", shell.call_args.args[1])
            sync.assert_called_once()
            self.assertEqual(verified.call_args.args[0], ["task-status", "--plan-id", "p", "--task-id", "t"])

    def test_remote_archive_cannot_escape_plan_root(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo("../../escaped")
            entry.size = 6
            archive.addfile(entry, io.BytesIO(b"unsafe"))
        with patch.object(planctl.remote, "run_remote_checked"), patch.object(planctl.remote, "pull_file", return_value=buffer.getvalue()):
            with self.assertRaises(planctl.PlanError):
                planctl._sync_plan_from_host({"ssh_alias": "fixture"}, "p", "/fixture")
        self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
