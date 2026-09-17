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
from runtime_fixture import runtime_receipt, host_runtime_receipts
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

    def request(self, path, *, actor="operator", body=None, raw=None, length=None, method="POST", extra_headers=None, before_body=None):
        handler = server.Handler.__new__(server.Handler)
        handler.command = method
        handler.path = path
        handler.headers = Message()
        handler.headers["Authorization"] = "Bearer " + actor + "-token"
        content = raw if raw is not None else json.dumps(body or {}).encode()
        handler.headers["Content-Length"] = str(len(content) if length is None else length)
        for key, value in (extra_headers or {}).items():
            handler.headers[key] = value
        class Body(io.BytesIO):
            def read(self, length=-1):
                if before_body:
                    before_body()
                return super().read(length)
        handler.rfile = Body(content)
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

    def test_delayed_body_cannot_submit_after_principal_revocation_or_token_rotation(self):
        original = self.principals.read_text()
        for route in ("/api/recovery/r/execute", "/api/plans/p/execute-next", "/api/hosts/h/pipeline/discovery"):
            for change in ({"disabled": True}, {"roles": ["viewer"]},
                           {"token_sha256": hashlib.sha256(b"replacement-fixture-token").hexdigest()}):
                with self.subTest(route=route, change=change):
                    self.principals.write_text(original)
                    def revoke():
                        data = json.loads(original)
                        next(row for row in data["principals"] if row["actor"] == "operator").update(change)
                        self.principals.write_text(json.dumps(data))
                    with patch.object(pipeline_runner, "start_run") as start:
                        status, _ = self.request(route, before_body=revoke)
                        self.assertEqual(status, 403 if "roles" in change else 401)
                        start.assert_not_called()

    def test_delayed_body_cannot_transfer_authority_to_a_reassigned_token_or_lab_mode(self):
        original = self.principals.read_text()
        for transition in ("principal", "lab"):
            with self.subTest(transition=transition), patch.dict(os.environ, {}):
                self.principals.write_text(original)
                def transfer():
                    if transition == "lab":
                        os.environ.update(OPU_WEBAPP_RBAC="0", OPU_WEBAPP_TOKEN="operator-token")
                    else:
                        data = json.loads(original)
                        next(row for row in data["principals"] if row["actor"] == "operator")["actor"] = "replacement-operator"
                        self.principals.write_text(json.dumps(data))
                with patch.object(pipeline_runner, "start_run") as start:
                    status, payload = self.request("/api/recovery/r/execute", before_body=transfer)
                    self.assertEqual(status, 403)
                    self.assertEqual(payload["message"], "Authenticated identity changed before dispatch")
                    start.assert_not_called()

    def test_legacy_live_get_routes_require_operator_and_keep_saved_reads_available(self):
        with patch.object(server, "run_discovery", return_value={"fresh": True}) as discovery, \
             patch.object(server, "build_estate", return_value=[]) as estate:
            for route in ("/api/hosts/h/discovery", "/api/estate?live=1", "/api/estate?live=%31"):
                for role in ("viewer", "requester", "approver"):
                    self.assertEqual(self.request(route, actor=role, method="GET")[0], 403)
            discovery.assert_not_called()
            estate.assert_not_called()
            self.assertEqual(self.request("/api/estate?live=0", actor="viewer", method="GET")[0], 200)
            estate.assert_called_once_with(live=False)
            self.assertEqual(self.request("/api/hosts/h/discovery", method="GET"), (200, {"fresh": True}))
            self.assertEqual(self.request("/api/estate?live=1", method="GET")[0], 200)
            estate.assert_called_with(live=True)

    def test_discovery_session_hint_matches_principal_policy_and_post_authorization(self):
        with patch.object(pipeline_runner, "start_run") as start:
            for role in ("viewer", "requester", "approver", "operator"):
                status, session = self.request("/api/session", actor=role, method="GET")
                self.assertEqual(status, 200)
                self.assertEqual(session["permissions"]["live_discovery"], role == "operator")
                if role != "operator":
                    self.assertEqual(self.request("/api/hosts/h/pipeline/discovery", actor=role)[0], 403)
            start.assert_not_called()
            start.return_value = SimpleNamespace(run_id="fixture-discovery")
            self.assertEqual(self.request("/api/hosts/h/pipeline/discovery"), (202, {"run_id": "fixture-discovery"}))
            start.assert_called_once()
        # The hint follows the authority policy, rather than a separate role list.
        with patch.dict(auth.ACTION_ROLES, {"execute": {"requester"}}):
            self.assertTrue(self.request("/api/session", actor="requester", method="GET")[1]["permissions"]["live_discovery"])
            self.assertFalse(self.request("/api/session", actor="operator", method="GET")[1]["permissions"]["live_discovery"])

    def test_discovery_session_hint_allows_authenticated_lab_mode_without_roles(self):
        with patch.dict(os.environ, {"OPU_WEBAPP_RBAC": "0", "OPU_WEBAPP_TOKEN": "operator-token"}):
            status, session = self.request("/api/session", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(session["mode"], "lab")
        self.assertEqual(session["roles"], [])
        self.assertTrue(session["permissions"]["live_discovery"])

    def test_company_live_get_requires_csrf_before_discovery(self):
        observed = []
        def authenticate(cookie, *, method, csrf, origin):
            observed.append(method)
            if method != "GET" and csrf != "valid-proof":
                raise auth.AuthError("CSRF required", status=403)
            return {"actor": "operator", "roles": ["operator"]}
        with patch.object(server.company_auth, "configured", return_value=True), \
             patch.object(server.company_auth, "authenticate", side_effect=authenticate), \
             patch.object(server, "run_discovery", return_value={}) as discovery, \
             patch.object(server, "build_estate", return_value=[]) as estate:
            headers = {"Cookie": "opu_company_session=fixture"}
            for route in ("/api/hosts/h/discovery", "/api/estate?live=1"):
                self.assertEqual(self.request(route, method="GET", extra_headers=headers)[0], 403)
            discovery.assert_not_called()
            estate.assert_not_called()
            headers["X-CSRF-Token"] = "valid-proof"
            self.assertEqual(self.request("/api/hosts/h/discovery", method="GET", extra_headers=headers)[0], 200)
            self.assertEqual(observed, ["POST", "POST", "POST"])

    def test_synchronous_discovery_shares_async_pipeline_reservation(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def discover(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"fresh": True}
        with patch.object(server.pipeline_steps, "step_discovery", side_effect=discover):
            worker = threading.Thread(target=lambda: results.append(server.run_discovery({"id": "h"})))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(pipeline_runner.RunConflict):
                    pipeline_runner.start_run("pipeline", "host:h:pipeline", lambda record: None)
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [{"fresh": True}])

    def test_principal_registry_rejects_writable_linked_and_non_utf8_files(self):
        self.principals.chmod(0o666)
        with self.assertRaises(auth.AuthError):
            auth.require_api_auth("Bearer operator-token")
        self.principals.chmod(0o600)
        alias = self.root / "linked-principals"
        os.link(self.principals, alias)
        with self.assertRaises(auth.AuthError):
            auth.require_api_auth("Bearer operator-token")
        alias.unlink()
        self.principals.write_bytes(b"\xff")
        with self.assertRaises(auth.AuthError):
            auth.require_api_auth("Bearer operator-token")

    def test_lab_token_is_private_rotatable_and_never_follows_a_link(self):
        path = self.root / "api-token"
        with patch.object(auth, "TOKEN_FILE", path), patch.dict(os.environ, {"OPU_WEBAPP_TOKEN": ""}):
            first = auth.ensure_token()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("rotated-token\n")
            self.assertEqual(auth.ensure_token(), "rotated-token")
            self.assertNotEqual(first, "rotated-token")
            path.chmod(0o644)
            with self.assertRaises(auth.AuthError):
                auth.ensure_token()
            path.unlink()
            victim = self.root / "preserved"
            victim.write_text("do not overwrite")
            path.symlink_to(victim)
            with self.assertRaises(auth.AuthError):
                auth.ensure_token()
            self.assertEqual(victim.read_text(), "do not overwrite")

    def test_production_flags_cannot_silently_disable_guards(self):
        certificate = self.root / "missing.cert"
        with patch.dict(os.environ, {"OPU_PRODUCTION_CERT_FILE": str(certificate)}):
            for value in ("1", "TRUE", "Yes", " on ", "tru"):
                with self.subTest(value=value), patch.dict(os.environ, {"OPU_PRODUCTION_MODE": value}):
                    with self.assertRaises(server.production.ProductionError):
                        server.production.require_live_mutation_allowed()
            certificate.write_text("OPU_PRODUCTION_CERTIFIED=1\n")
            for value in ("TRUE", " yes ", "invalid"):
                with self.subTest(checklist=value), patch.dict(os.environ, {"OPU_PRODUCTION_MODE": "1", "OPU_PRODUCTION_REQUIRE_CHECKLIST": value}):
                    with self.assertRaises(server.production.ProductionError):
                        server.production.require_live_mutation_allowed()

    def test_lab_token_storage_errors_are_controlled(self):
        with patch.dict(os.environ, {"OPU_WEBAPP_TOKEN": ""}):
            for error in (OSError("denied"), ValueError("unsafe lock"), TimeoutError("busy")):
                with self.subTest(error=type(error).__name__), patch.object(auth, "file_lock", side_effect=error):
                    with self.assertRaises(auth.AuthError) as raised:
                        auth.ensure_token()
                    self.assertEqual(raised.exception.status, 503)

    def test_artifact_staging_rejects_ambiguous_types_before_remote_work(self):
        stages = server.pipeline_steps
        valid = {"artifact_dir": "/patch", "owner": "oracle", "source": {"zip_path": "/patch.zip"}}
        with patch.object(stages.tools_sync, "ensure_host_tools", side_effect=host_runtime_receipts) as sync, \
             patch.object(stages.remote, "run_remote_raw") as run:
            for update in ({"replace": "false"}, {"replace": 1}, {"owner": []}, {"artifact_dir": {}},
                           {"source": []}, {"source": {"zip_path": "/patch.zip", "unexpected": True}},
                           {"source": {"host_id": 7}}):
                with self.subTest(update=update), self.assertRaises(server.remote.RemoteError):
                    stages.step_stage_artifact("h", {}, {**valid, **update})
            sync.assert_not_called()
            run.assert_not_called()

    def test_failed_media_replacement_invalidates_prior_approval_evidence(self):
        stages = server.pipeline_steps
        for name in stages._ARTIFACT_BOUND_EVIDENCE:
            evidence.write_evidence("h", name, {"status": "passed"})
        with patch.object(stages.tools_sync, "ensure_host_tools", side_effect=server.remote.RemoteError("install", "fixture failure")):
            with self.assertRaises(server.remote.RemoteError):
                stages.step_stage_artifact("h", {}, {"artifact_dir": "/patch", "owner": "oracle",
                    "source": {"zip_path": "/patch.zip"}, "replace": True})
        for name in stages._ARTIFACT_BOUND_EVIDENCE:
            self.assertIsNone(evidence.read_evidence("h", name))

    def test_collector_exit_status_cannot_be_masked_by_json_output(self):
        for code in (-9, 1, 65, 126, 127, 255):
            with self.subTest(code=code):
                result = subprocess.CompletedProcess([], code, '{"status":"passed"}', "failure")
                with patch.object(server.remote.subprocess, "run", return_value=result):
                    with self.assertRaises(server.remote.RemoteError):
                        server.remote.run_remote_json("fixture", ["/fixture/collector"])
                with patch.object(server.pipeline_steps.localtools.subprocess, "run", return_value=result):
                    with self.assertRaises(server.pipeline_steps.localtools.LocalToolError):
                        server.pipeline_steps.localtools.run_tool("opu-readiness-evaluate", [])
        for payload in ({"status": "blocked"}, {"artifact": {"status": "blocked"}}):
            result = subprocess.CompletedProcess([], 2, json.dumps(payload), "findings")
            with patch.object(server.remote.subprocess, "run", return_value=result):
                self.assertEqual(server.remote.run_remote_json("fixture", ["/fixture/collector"]), payload)
        for code, payload in ((2, {"status": "passed"}), (0, []), (0, None)):
            result = subprocess.CompletedProcess([], code, json.dumps(payload), "")
            with patch.object(server.remote.subprocess, "run", return_value=result):
                with self.assertRaises(server.remote.RemoteError):
                    server.remote.run_remote_json("fixture", ["/fixture/collector"])
        with patch.object(server.remote.subprocess, "run") as run:
            for alias in ("-oProxyCommand=anything", "has space", "bad\nname", ""):
                with self.assertRaises(server.remote.RemoteError):
                    server.remote.run_remote_raw(alias, ["true"])
            run.assert_not_called()

    def test_evidence_identifiers_cannot_escape_or_include_trailing_newline(self):
        for name in ("../outside", "a/b", "valid\n", None, []):
            with self.subTest(name=name):
                with self.assertRaises(evidence.EvidenceError):
                    evidence.evidence_path("h", name)
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_host_id(name)
                with self.assertRaises(planctl.PlanError):
                    planctl.validate_plan_id(name)

    def test_inventory_cannot_silently_choose_duplicate_or_malformed_hosts(self):
        inventory = self.root / "hosts.json"
        host = {"id": "h", "ssh_alias": "fixture", "remote_root": "/fixture", "sudo": False}
        with patch.object(server, "HOSTS_FILE", inventory), patch.object(planctl, "HOSTS_FILE", inventory), \
             patch.object(server.remote, "run_remote_raw") as ssh:
            for hosts in ([host, {**host, "ssh_alias": "wrong"}], [host, {**host, "id": "H"}],
                          [{**host, "sudo": "false"}], [{**host, "ssh_alias": "-oProxyCommand=anything"}],
                          *[[{**host, "remote_root": root}] for root in ("/", "//", "/./", "/opt/./opu")],
                          [{**host, "nodes": [{"name": "n"}, {"name": "N"}]}]):
                inventory.write_text(json.dumps({"hosts": hosts}))
                self.assertEqual(self.request("/api/estate", method="GET")[0], 503)
                with self.assertRaises(planctl.PlanError):
                    planctl._load_hosts()
            inventory.write_text(json.dumps({"hosts": [host]}))
            self.assertEqual(server.load_hosts(), {"h": host})
            ssh.assert_not_called()

    def test_failed_pipeline_attempt_invalidates_older_success(self):
        stages = server.pipeline_steps
        documents = ("snapshot", "reconciliation", "artifact", "procedure", "compatibility", "compatibility_reconciliation", "readiness")
        cases = (
            (stages.step_reconcile, {}, ("reconciliation", "compatibility_reconciliation", "readiness")),
            (stages.step_artifact_inspect, {"artifact_dir": "/new-artifact"}, ("artifact", "procedure", "compatibility", "compatibility_reconciliation", "readiness")),
            (stages.step_compatibility_collect, {"artifact_dir": "/new-artifact"}, ("compatibility", "compatibility_reconciliation", "readiness")),
            (stages.step_compatibility_reconcile, {}, ("compatibility_reconciliation", "readiness")),
            (stages.step_readiness_evaluate, {"policy": {}}, ("readiness",)),
        )
        for function, body, invalidated in cases:
            with self.subTest(stage=function.__name__):
                for name in documents:
                    evidence.write_evidence("h", name, {"status": "passed", "old": True})
                with patch.object(stages.tools_sync, "ensure_host_tools", side_effect=RuntimeError("fixture failure")), \
                     patch.object(stages.localtools, "run_tool", side_effect=RuntimeError("fixture failure")):
                    with self.assertRaises(RuntimeError):
                        function("h", {"id": "h", "ssh_alias": "fixture", "remote_root": "/fixture"}, body)
                for name in invalidated:
                    self.assertIsNone(evidence.read_evidence("h", name), name)

    def test_partial_multinode_refresh_cannot_publish_mixed_or_previous_inventory(self):
        stages = server.pipeline_steps
        host = {"id": "h", "ssh_alias": "n1", "remote_root": "/fixture", "nodes": [
            {"name": "n1", "ssh_alias": "n1"}, {"name": "n2", "ssh_alias": "n2"}]}
        for name in ("snapshot", "snapshot_nodes", "snapshot_n1", "readiness"):
            evidence.write_evidence("h", name, {"old": True})
        with patch.object(stages.tools_sync, "ensure_host_tools", side_effect=host_runtime_receipts), \
             patch.object(stages.remote, "run_remote_json", side_effect=[{"host": {"name": "n1"}}, server.remote.RemoteError("ssh_timeout", "n2 failed")]):
            with self.assertRaises(server.remote.RemoteError):
                stages.step_discovery("h", host, {})
        self.assertEqual(evidence.read_evidence("h", "snapshot_n1"), {"old": True})
        self.assertEqual(evidence.list_snapshot_paths("h"), [])
        self.assertIsNone(evidence.read_evidence("h", "readiness"))
        host["nodes"][1]["name"] = "n1.other-domain"
        with self.assertRaises(server.remote.RemoteError):
            stages._configured_nodes(host)

    def test_execution_limits_and_notifications_do_not_claim_completion(self):
        with patch.object(planctl, "status", return_value={"state": "running"}), \
             patch.object(planctl, "execute_next_task", return_value={"status": "succeeded"}) as execute:
            result = planctl.execute_remaining_tasks("p", "operator", max_tasks=1)
            self.assertEqual(result["stopped_reason"], "max_tasks_reached")
            execute.assert_called_once()
        cases = ((result, "progress"), ({"plan_state": "paused", "task_results": [{"status": "failed"}]}, "failed"),
                 ({"plan_state": "succeeded", "task_results": [{"status": "succeeded"}]}, "succeeded"))
        with patch.object(server.notifications, "emit") as emit:
            for payload, event in cases:
                server._notify_execution("p", "operator", payload, remaining=True)
                self.assertEqual(emit.call_args.args[0], "plan.execute." + event)
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

    def test_lock_routes_require_execution_role_and_typed_bound_identity(self):
        with patch.object(pipeline_runner, "start_run") as start:
            for operation in ("inspect", "recover"):
                path = "/api/plans/p/lock-" + operation
                for actor in ("viewer", "requester", "approver"):
                    self.assertEqual(self.request(path, actor=actor)[0], 403)
                self.assertEqual(self.request(path, body={"actor": "other"})[0], 403)
                for field in ("actor", "run_id"):
                    for value in (1, [], {}, None, True):
                        with self.subTest(operation=operation, field=field, value=value):
                            self.assertEqual(self.request(path, body={field: value})[0], 400)
                self.assertEqual(self.request(path, body={"run_id": " "})[0], 400)
            start.assert_not_called()

    def test_lock_routes_use_distinct_managed_runs_and_never_execute_tasks(self):
        launches = []

        def start(kind, key, function):
            launches.append((kind, key))
            record = SimpleNamespace(run_id="bbbbbbbbbbbb")
            function(record)
            return record

        with patch.object(pipeline_runner, "start_run", side_effect=start), \
             patch.object(server.lockctl, "inspect", return_value={}) as inspect, \
             patch.object(server.lockctl, "recover", return_value={}) as recover, \
             patch.object(planctl, "execute_next_task") as execute, \
             patch.object(planctl, "execute_remaining_tasks") as remaining:
            for operation in ("inspect", "recover"):
                status, payload = self.request("/api/plans/p/lock-" + operation, body={"run_id": "aaaaaaaaaaaa"})
                self.assertEqual((status, payload), (202, {"run_id": "bbbbbbbbbbbb"}))
            self.assertEqual(launches, [("lock_inspect", "plan:p:lock-inspect"), ("lock_recovery", "plan:p:lock-recovery")])
            inspect.assert_called_once_with("p", "operator", "aaaaaaaaaaaa")
            recover.assert_called_once_with("p", "operator", "aaaaaaaaaaaa", maintenance_run_id="bbbbbbbbbbbb")
            execute.assert_not_called()
            remaining.assert_not_called()
        with patch.object(pipeline_runner, "start_run", side_effect=pipeline_runner.RunConflict("existing maintenance", "cccccccccccc")):
            status, payload = self.request("/api/plans/p/lock-recover", body={"run_id": "aaaaaaaaaaaa"})
            self.assertEqual(status, 409)
            self.assertEqual(payload["run_id"], "cccccccccccc")

    def test_reconcile_route_dispatches_maintenance_and_original_execution_inspectors(self):
        for is_maintenance in (False, True):
            with self.subTest(maintenance=is_maintenance):
                record = {"context": {"lock_recovery": is_maintenance}}

                def reconcile(run_id, **options):
                    self.assertEqual(run_id, "aaaaaaaaaaaa")
                    self.assertEqual(options["actor"], "operator")
                    return options["inspect"](record)

                with patch.object(pipeline_runner, "reconcile_run", side_effect=reconcile), \
                     patch.object(server.lockctl, "reconcile_detached_run", return_value={"status": "unknown"}) as maintenance, \
                     patch.object(server.lockctl, "reconcile_execution_run", return_value={"status": "unknown"}) as execution, \
                     patch.object(server.lockctl, "recover") as recover:
                    self.assertEqual(self.request("/api/runs/aaaaaaaaaaaa/reconcile"), (200, {"status": "unknown"}))
                    (maintenance if is_maintenance else execution).assert_called_once_with(record)
                    (execution if is_maintenance else maintenance).assert_not_called()
                    recover.assert_not_called()

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
        with patch.object(planctl, "_resolve_node_host", return_value=host), patch.object(planctl.remote, "run_remote_shell", return_value=SimpleNamespace(returncode=0, stdout="RC\n0\n")) as shell, patch.object(planctl.remote, "run_remote_raw", side_effect=[SimpleNamespace(returncode=0, stdout='{"status":"succeeded","task_id":"t"}'), SimpleNamespace(returncode=0, stdout="")]), patch.object(planctl, "_sync_plan_from_host") as sync, patch.object(planctl, "status", return_value={"state": "running"}), patch.object(planctl, "_run", return_value={"status": "succeeded"}) as verified:
            result = planctl.reconcile_detached_run({"context": context})
            self.assertEqual(result["status"], "succeeded")
            self.assertNotIn("nohup", shell.call_args.args[1])
            sync.assert_called_once()
            self.assertEqual(verified.call_args.args[0], ["task-status", "--plan-id", "p", "--task-id", "t"])

    def test_live_nonterminal_task_keeps_bounded_native_diagnostics(self):
        host = {"id": "h", "ssh_alias": "alias", "remote_root": "/opt/opu"}
        task = {"task_id": "t", "adapter": "database_single_instance_opatch", "task_definition_sha256": "a" * 64, "retry_count": 1}
        stderr = "x" * 5000 + "\npassword=private-value token='private-token'\nAuthorization: Bearer private-bearer\nanother Oracle executor owns this host\n"
        with patch.object(planctl.production, "require_live_mutation_allowed"), \
             patch.object(planctl, "_resolve_live_host_for_task", return_value=host), \
             patch.object(planctl, "_sync_plan_to_host", return_value=("/opt/opu/plans", runtime_receipt(host["ssh_alias"], host["remote_root"]))), \
             patch.object(planctl, "_run_detached_remote", return_value=(75, "private stdout", stderr)) as launch, \
             patch.object(planctl, "_sync_plan_from_host"), \
             patch.object(planctl, "_run", return_value={"status": "pending"}) as verified, \
             patch.object(pipeline_runner, "set_execution_context") as context:
            with self.assertRaises(planctl.PlanError) as raised:
                planctl._execute_live("p", {}, task, "operator")
            error = raised.exception.to_json()
            self.assertEqual(error["result"], {"exit_code": 75, "task_status": "pending"})
            self.assertLessEqual(len(error["stderr"]), 4000)
            self.assertIn("another Oracle executor owns this host", error["stderr"])
            for secret in ("private-value", "private-token", "private-bearer", "private stdout"):
                self.assertNotIn(secret, json.dumps(error))
            self.assertIn("[REDACTED]", error["stderr"])
            launch.assert_called_once()
            verified.assert_called_once_with(["task-status", "--plan-id", "p", "--task-id", "t"])
            context.assert_called_once_with(runtime_root=runtime_receipt("alias", "/opt/opu")["runtime_root"], runtime_fingerprint="a" * 64, task_definition_sha256="a" * 64, task_retry_count=1)

    def test_live_verified_terminal_result_is_unchanged(self):
        host = {"id": "h", "ssh_alias": "alias", "remote_root": "/opt/opu"}
        task = {"task_id": "t", "adapter": "database_single_instance_opatch", "task_definition_sha256": "a" * 64, "retry_count": 1}
        with patch.object(planctl.production, "require_live_mutation_allowed"), \
             patch.object(planctl, "_resolve_live_host_for_task", return_value=host), \
             patch.object(planctl, "_sync_plan_to_host", return_value=("/opt/opu/plans", runtime_receipt(host["ssh_alias"], host["remote_root"]))), \
             patch.object(planctl, "_run_detached_remote", return_value=(0, '{"status":"succeeded","task_id":"t"}', "")) as launch, \
             patch.object(planctl, "_sync_plan_from_host"), \
             patch.object(planctl, "_run", return_value={"status": "succeeded"}) as verified, \
             patch.object(planctl, "status", return_value={"state": "running"}), \
             patch.object(pipeline_runner, "set_execution_context") as context:
            self.assertEqual(planctl._execute_live("p", {}, task, "operator"), {"status": "succeeded", "task_id": "t"})
            launch.assert_called_once()
            verified.assert_called_once_with(["task-status", "--plan-id", "p", "--task-id", "t"])
            self.assertEqual([item.kwargs for item in context.call_args_list],
                             [{"runtime_root":runtime_receipt("alias", "/opt/opu")["runtime_root"], "runtime_fingerprint":"a" * 64, "task_definition_sha256": "a" * 64, "task_retry_count": 1}, {"detached_terminal": True}])

    def test_reconciliation_reports_preclaim_error_without_clearing_unknown(self):
        host = {"id": "h", "node_name": "n", "ssh_alias": "alias", "remote_root": "/opt/opu"}
        context = {"detached_execution": True, "plan_id": "p", "task_id": "t", "node": "n", "host_id": "h", "ssh_alias": "alias", "remote_root": "/opt/opu", "remote_run_dir": "/opt/opu/var/webapp-runs/p/t/" + "a" * 32}
        old = self.orphan(context=context)
        stderr = "another Oracle executor owns this host\ntoken=private-token"
        with patch.object(planctl, "_resolve_node_host", return_value=host), \
             patch.object(planctl.remote, "run_remote_shell", return_value=SimpleNamespace(returncode=0, stdout="RC\n75\n")) as shell, \
             patch.object(planctl.remote, "run_remote_raw", side_effect=[SimpleNamespace(returncode=0, stdout="private stdout"), SimpleNamespace(returncode=0, stdout=stderr)]) as read, \
             patch.object(planctl, "_sync_plan_from_host") as sync, \
             patch.object(planctl, "_run", return_value={"status": "pending"}) as verified, \
             patch.object(planctl, "_run_detached_remote") as launch:
            result = pipeline_runner.reconcile_run(old.run_id, actor="operator", inspect=planctl.reconcile_detached_run)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["error"]["result"], {"exit_code": 75, "task_status": "pending"})
            self.assertIn("another Oracle executor owns this host", result["error"]["stderr"])
            self.assertNotIn("private-token", json.dumps(result))
            self.assertNotIn("private stdout", json.dumps(result))
            self.assertEqual(pipeline_runner.active_run_id(old.key), old.run_id)
            with self.assertRaises(pipeline_runner.RunConflict):
                pipeline_runner.start_run("plan", old.key, lambda record: {})
            shell.assert_called_once()
            self.assertNotIn("nohup", shell.call_args.args[1])
            self.assertEqual(read.call_count, 2)
            sync.assert_called_once()
            verified.assert_called_once_with(["task-status", "--plan-id", "p", "--task-id", "t"])
            launch.assert_not_called()

    def test_plan_status_exposes_matching_unknown_run_without_reconciliation(self):
        record = pipeline_runner.RunRecord("123456abcdef", "plan", "plan:p:execute")
        record.status = "unknown"
        record.context = {"plan_id": "p", "task_id": "t", "detached_execution": True}
        record.error = {"message": "No terminal result", "stderr": "host is locked token=private-token", "result": {"exit_code": 75, "task_status": "pending", "stdout": "private stdout"}}
        record._persist()
        before = (pipeline_runner.RUNS_DIR / record.run_id / "run.json").read_bytes()
        # Exercise disk lookup while the reconciliation endpoint's file lock
        # is already held. Status must not reacquire that file lock.
        with pipeline_runner.file_lock(pipeline_runner.RUNS_DIR / ".registry.lock"), \
             patch.object(planctl, "_run", return_value={"plan_id": "p", "state": "running"}) as native, \
             patch.object(planctl, "reconcile_detached_run") as reconcile, \
             patch.object(planctl, "_run_detached_remote") as launch:
            plan = planctl.status("p")
            self.assertEqual(plan["unresolved_run"]["run_id"], record.run_id)
            self.assertEqual(plan["unresolved_run"]["status"], "unknown")
            self.assertEqual(plan["unresolved_run"]["context"], record.context)
            self.assertEqual(plan["unresolved_run"]["error"]["result"], {"exit_code": 75, "task_status": "pending"})
            self.assertIn("host is locked", plan["unresolved_run"]["error"]["stderr"])
            self.assertNotIn("private-token", json.dumps(plan))
            self.assertNotIn("private stdout", json.dumps(plan))
            native.assert_called_once_with(["status", "--plan-id", "p"])
            reconcile.assert_not_called()
            launch.assert_not_called()
        self.assertEqual((pipeline_runner.RUNS_DIR / record.run_id / "run.json").read_bytes(), before)
        self.assertEqual(pipeline_runner.active_run_id(record.key), record.run_id)

    def test_plan_status_excludes_other_and_terminal_runs(self):
        record = pipeline_runner.RunRecord("123456abcdef", "plan", "plan:other:execute")
        record.status = "unknown"
        record._persist()
        with patch.object(planctl, "_run", side_effect=lambda args: {"plan_id": "p", "state": "running"}):
            self.assertNotIn("unresolved_run", planctl.status("p"))
            pipeline_runner.RUNS[record.run_id] = record
            # A stale/mismatched in-memory index must not attribute another
            # plan's run to this plan.
            pipeline_runner._ACTIVE_KEYS["plan:p:execute"] = record.run_id
            self.assertNotIn("unresolved_run", planctl.status("p"))
            record.key = "plan:p:execute"
            for run_status in ("queued", "running", "succeeded", "failed"):
                record.status = run_status
                self.assertNotIn("unresolved_run", planctl.status("p"), run_status)
            record.status = "reconciling"
            self.assertEqual(planctl.status("p")["unresolved_run"]["status"], "reconciling")

    def test_remote_archive_cannot_escape_plan_root(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo("../../escaped")
            entry.size = 6
            archive.addfile(entry, io.BytesIO(b"unsafe"))
        with patch.object(planctl, "_temporary_remote_archive", return_value="/tmp/opu-plan-transfer.ABCDEFGHIJKL"), \
             patch.object(planctl.remote, "run_remote_checked"), patch.object(planctl.remote, "pull_file", return_value=buffer.getvalue()):
            with self.assertRaises(planctl.PlanError):
                planctl._sync_plan_from_host({"ssh_alias": "fixture"}, "p", "/fixture")
        self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
