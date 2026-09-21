#!/usr/bin/env python3
"""Real Handler/auth/native-dispatch integration; isolated state and no Oracle/LLM calls.

Only the HTTP transport is replaced with byte buffers. In particular, _send_json
remains real so a confirmed action exercises the nested native response sink.
Native Oracle functions and model responses are the explicit test seams.
"""
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import assistant
import auth
import evidence
import pipeline_runner
import server


class AssistantApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-assistant-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hosts_file = self.root / "hosts.json"
        self.host = {"id": "source", "label": "Source fixture", "ssh_alias": "fixture.invalid",
                     "ssh_user": "fixture", "ssh_key": "/never-read-fixture-key"}
        self.hosts_file.write_text(json.dumps({"hosts": [self.host]}))
        self.original_hosts = self.hosts_file.read_bytes()
        self.principals = self.root / "principals.json"
        self.roles = {role: [role] for role in ("admin", "viewer", "requester", "approver", "operator")}
        self.write_principals()
        self.enterContext(patch.dict(os.environ, {
            "OPU_WEBAPP_RBAC": "1", "OPU_WEBAPP_PRINCIPALS_FILE": str(self.principals),
            "OPU_PRODUCTION_MODE": "0", "OPU_OIDC_CONFIG": "", "OPU_WEBAPP_ALLOW_FIXTURES": "0",
        }))
        self.enterContext(patch.object(auth, "TOKEN_FILE", self.root / "api-token"))
        self.enterContext(patch.object(auth, "_CACHED_TOKEN", None))
        self.company_configured = self.enterContext(patch.object(server.company_auth, "configured", return_value=False))
        self.enterContext(patch.object(server, "HOSTS_FILE", self.hosts_file))
        self.enterContext(patch.object(assistant, "STATE_DIR", self.root / "assistant"))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "evidence"))
        self.enterContext(patch.object(server.planctl, "PLAN_STATE_DIR", self.root / "plans"))
        self.enterContext(patch.object(server.recoveryctl, "LIVE_DIR", self.root / "recovery-live"))
        self.enterContext(patch.object(server.recoveryctl, "RECOVERY_DIR", self.root / "recovery-fixtures"))
        self.enterContext(patch.object(pipeline_runner, "RUNS_DIR", self.root / "runs"))
        self.enterContext(patch.object(pipeline_runner, "RUNS", {}))
        self.enterContext(patch.object(pipeline_runner, "_ACTIVE_KEYS", {}))
        self.notifications = self.enterContext(patch.object(server.notifications, "emit"))
        self.config = self.enterContext(patch.object(server.local_llm, "config_status", return_value={
            "enabled": True, "configured": True, "model": "fixture-local", "provider": "ollama", "reason": None,
        }))
        self.model = self.enterContext(patch.object(server.local_llm, "complete",
            return_value={"role": "assistant", "content": "Fixture response", "tool_calls": []}))
        self.saved_plan = {"plan_id": "plan-a", "state": "running", "patch": {"patch_id": "39034528"},
            "snapshot_evidence": [{"path": str(self.root / "hosts/source/evidence/snapshot.json")}],
            "target": {"database_unique_name": "ORCL"}}
        self.enterContext(patch.object(server.planctl, "status", return_value=self.saved_plan))
        self.enterContext(patch.object(server.planctl, "list_tasks", return_value=[]))
        self.recovery_status = self.enterContext(patch.object(server.recoveryctl, "status",
            side_effect=AssertionError("Assistant inspection and proposal binding must not call live recovery status")))
        backup_dir = server.recoveryctl.LIVE_DIR / "backup-a"
        backup_dir.mkdir(parents=True, mode=0o700)
        metadata_file = backup_dir / "metadata.json"
        metadata_file.write_text(json.dumps({
            "request_id": "backup-a", "host_id": "source", "mode": "live", "host": self.host,
            "last_status": {"request_id": "backup-a", "state": "authorized", "target": {"database_unique_name": "ORCL"}},
        }))
        metadata_file.chmod(0o600)
        self.native = {}
        self.native_original = {}
        for module, methods in ((server.planctl, ("create", "dispatch", "execute_remaining_tasks", "approve", "authorize")),
                                (server.recoveryctl, ("create_live", "analyze", "execute", "approve", "authorize", "create_testmode_demo"))):
            for name in methods:
                self.native_original[module.__name__ + "." + name] = getattr(module, name)
                self.native[module.__name__ + "." + name] = self.enterContext(patch.object(module, name, return_value={"state": "fixture"}))
        self.steps = {name: Mock(return_value={"status": "fixture"}) for name in ("discovery", "readiness-chain", "recovery-collect")}
        self.enterContext(patch.object(server.pipeline_steps, "STEPS", self.steps))
        procedure = {
            "patch_id": "39034528", "target": {"database_unique_name": "ORCL"},
            "artifact_sha256": "a" * 64,
            "oracle_references": [{"kind": "patch_readme", "identifier": "README.html", "sha256": "b" * 64}],
        }
        evidence.write_evidence("source", "procedure_input", procedure)
        evidence.write_evidence("source", "procedure", {"status": "ready_for_planning", "procedure": procedure})
        evidence.write_evidence("source", "artifact", {"artifact": {
            "path": "/fixture/stage/39034528", "sha256": "a" * 64,
            "readme_files": [{"path": "README.html", "sha256": "b" * 64}],
        }})
        evidence.write_evidence("source", "policy", {"schema_version": "1.0", "recovery": {"require_backup": True}})
        evidence.write_evidence("source", "readiness", {"status": "ready_for_approval", "valid_until": "2099-01-01T00:00:00Z"})
        self.addCleanup(self.finish_workers)

    def write_principals(self):
        self.principals.write_text(json.dumps({"principals": [
            {"actor": actor, "roles": roles, "token_sha256": hashlib.sha256((actor + "-fixture-token").encode()).hexdigest()}
            for actor, roles in self.roles.items()]}))

    def finish_workers(self):
        for record in list(pipeline_runner.RUNS.values()):
            self.wait_run(record.run_id)

    def wait_run(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = pipeline_runner.get_run(run_id)
            if record is not None and record.finished_at is not None:
                with pipeline_runner._REGISTRY_LOCK:
                    if pipeline_runner._ACTIVE_KEYS.get(record.key) != record.run_id:
                        return record
            time.sleep(0.005)
        self.fail("Isolated worker did not finish: " + run_id)

    def request(self, path="/api/assistant/conversations", *, method="GET", actor="admin", body=None, headers=None, token=None):
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command = path, method
        handler.headers = Message()
        if actor is not None or token is not None:
            handler.headers["Authorization"] = "Bearer " + (token or actor + "-fixture-token")
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        raw = json.dumps({} if body is None else body).encode()
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        result = {"headers": {}, "statuses": []}
        handler.send_response = lambda status: result["statuses"].append(status)
        handler.send_header = lambda name, value: result["headers"].update({name: value})
        handler.end_headers = lambda: None
        handler.send_error = lambda status, *args, **kwargs: handler._send_json(status, {"error": "http_error"})
        getattr(handler, "do_" + method)()
        self.assertEqual(len(result["statuses"]), 1, "The native response sink must yield exactly one outer HTTP response")
        result["status"] = result["statuses"][0]
        result["body"] = json.loads(handler.wfile.getvalue())
        self.assertEqual(result["headers"].get("Cache-Control"), "no-store")
        self.assertIsNone(getattr(handler, "_response_sink", None))
        self.assertEqual(self.hosts_file.read_bytes(), self.original_hosts)
        self.recovery_status.assert_not_called()
        return result

    def create(self, actor="admin", **kwargs):
        response = self.request(method="POST", actor=actor, **kwargs)
        self.assertEqual(response["status"], 201, response)
        return response["body"]["conversation"]

    def prepare(self, name, arguments, actor="admin"):
        conversation = self.create(actor=actor)
        assistant._proposal(actor, conversation["id"], name, arguments, server.load_hosts())
        action = assistant.get(actor, conversation["id"])["actions"][0]
        return conversation["id"], action

    def execute(self, conversation_id, action, actor="admin", **kwargs):
        return self.request(f"/api/assistant/conversations/{conversation_id}/actions/{action['id']}/execute",
                            method="POST", actor=actor, body={"digest": action["digest"]}, **kwargs)

    def assert_no_native_calls(self):
        for function in [*self.native.values(), *self.steps.values()]:
            function.assert_not_called()

    def test_message_turn_uses_actual_controller_ownership_and_persists_response(self):
        conversation = self.create(actor="operator")
        path = f"/api/assistant/conversations/{conversation['id']}"
        response = self.request(path + "/messages", method="POST", actor="operator",
                                body={"content": "Explain the patch review process"})
        self.assertEqual(response["status"], 202, response)
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual(record.owner["protocol"], "incarnation-lock-v1")
        self.assertEqual(record.status, "succeeded", record.error)
        current = self.request(path, actor="operator")["body"]["conversation"]
        self.assertFalse(current["busy"])
        self.assertEqual(current["messages"][-1]["content"], "Fixture response")
        self.model.assert_called_once()
        self.assert_no_native_calls()

    def test_individual_authentication_and_private_conversation_ownership(self):
        self.assertEqual(self.request(actor=None)["status"], 401)
        self.assertEqual(self.request(headers={"X-OPU-Actor": "operator"})["status"], 403)
        conversation = self.create(actor="requester")
        route = "/api/assistant/conversations/" + conversation["id"]
        self.assertEqual(self.request(route, actor="requester")["status"], 200)
        for other in ("admin", "viewer", "operator"):
            with self.subTest(other=other):
                self.assertEqual(self.request(route, actor=other)["status"], 404)
                self.assertEqual(self.request(actor=other)["body"]["conversations"], [])
                self.assertEqual(self.request(route + "/messages", method="POST", actor=other, body={"content": "Read their private chat"})["status"], 404)
        owner_dir = assistant.STATE_DIR / hashlib.sha256(b"requester").hexdigest()
        self.assertEqual(owner_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((owner_dir / (conversation["id"] + ".json")).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("owner", self.request(route, actor="requester")["body"]["conversation"])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_lab_token_can_inspect_configuration_but_cannot_chat_or_claim_an_identity(self):
        with patch.dict(os.environ, {"OPU_WEBAPP_RBAC": "0", "OPU_WEBAPP_PRINCIPALS_FILE": "", "OPU_WEBAPP_TOKEN": "lab-fixture-token"}):
            common = {"token": "lab-fixture-token", "headers": {"X-OPU-Actor": "admin"}}
            result = self.request("/api/assistant/config", **common)
            self.assertEqual(result["status"], 200)
            self.assertFalse(result["body"]["can_chat"])
            self.assertEqual(self.request(**common)["status"], 403)
            self.assertEqual(self.request(method="POST", **common)["status"], 403)
        self.assertFalse(assistant.STATE_DIR.exists())
        self.assert_no_native_calls()

    def test_assistant_payloads_reject_extra_fields_and_identity_spoofing(self):
        conversation_id, action = self.prepare("refresh_discovery", {"host_id": "source"})
        base = "/api/assistant/conversations/" + conversation_id
        cases = [("/api/assistant/conversations", {}), (base + "/messages", {"content": "Inspect"}),
                 (base + f"/actions/{action['id']}/execute", {"digest": action["digest"]}),
                 (base + f"/actions/{action['id']}/dismiss", {})]
        for route, body in cases:
            for field, value in (("actor", "admin"), ("requester", "admin"), ("tools", []),
                                 ("role", "system"), ("approval_ticket", "invented"), ("_record", {})):
                with self.subTest(route=route, field=field):
                    self.assertEqual(self.request(route, method="POST", body={**body, field: value})["status"], 400)
            self.assertEqual(self.request(route, method="POST", body={**body, "actor": "operator"})["status"], 403)
        self.assertEqual(self.request(base + "/messages", method="POST", body=[])["status"], 400)
        self.assertEqual(self.request(base + "/approve", method="POST", body={})["status"], 404)
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_confirmed_plan_actions_use_native_run_keys_requester_actor_and_notifications(self):
        windows = {"window_start": "2099-01-01T01:00:00Z", "window_end": "2099-01-01T02:00:00Z"}
        cases = [
            ("create_patch_plan", {"host_id": "source", "plan_id": "new-plan", "patch_id": "39034528", "database": "ORCL", **windows},
             "requester", "plan:new-plan:create", "planctl.create", ("new-plan", "requester", "source", *windows.values()), {}, "plan.created"),
            ("dispatch_plan", {"plan_id": "plan-a"}, "operator", "plan:plan-a:dispatch", "planctl.dispatch", ("plan-a", "operator"), {}, "plan.dispatched"),
            ("execute_plan", {"plan_id": "plan-a"}, "operator", "plan:plan-a:execute", "planctl.execute_remaining_tasks", ("plan-a", "operator"), {"max_tasks": 200}, "plan.execute.succeeded"),
        ]
        for name, arguments, actor, key, native, args, kwargs, event in cases:
            with self.subTest(tool=name):
                if name == "execute_plan":
                    self.native[native].return_value = {"plan_state": "succeeded", "task_results": [{"status": "succeeded"}]}
                previous_runs = set(pipeline_runner.RUNS)
                conversation_id, action = self.prepare(name, arguments, actor=actor)
                if name == "create_patch_plan":
                    approved = assistant._read(assistant._path(actor, conversation_id), actor)["actions"][0]["binding"]
                    kwargs = {"expected_creation_binding_sha256": approved, "patch_id": arguments["patch_id"],
                              "database": arguments["database"], "hosts": {"source": self.host}}
                self.assertEqual(set(pipeline_runner.RUNS), previous_runs, "Preparation must not launch a native command")
                response = self.execute(conversation_id, action, actor=actor)
                self.assertEqual(response["status"], 202, response)
                record = self.wait_run(response["body"]["run_id"])
                self.assertEqual((record.kind, record.key, record.status), ("plan", key, "succeeded"))
                self.native[native].assert_called_once_with(*args, **kwargs)
                self.assertTrue(any(call.args[0] == event for call in self.notifications.call_args_list))
                saved = assistant.get(actor, conversation_id)["actions"][0]
                self.assertEqual((saved["state"], saved["confirmed_by"], saved["run_id"]), ("completed", actor, record.run_id))
                self.assertEqual(self.execute(conversation_id, action, actor=actor)["status"], 409)
                self.native[native].assert_called_once()
        self.native["planctl.approve"].assert_not_called()
        self.native["planctl.authorize"].assert_not_called()

    def test_confirmed_backup_actions_use_native_scope_and_authenticated_requester(self):
        windows = {"window_start": "2099-01-01T01:00:00Z", "window_end": "2099-01-01T02:00:00Z"}
        arguments = {"host_id": "source", "request_id": "new-backup", "database": "ORCL", "backup_parent": "/fixture/backup", **windows}
        conversation_id, action = self.prepare("create_backup", arguments, actor="requester")
        response = self.execute(conversation_id, action, actor="requester")
        self.assertEqual(response["status"], 202, response)
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual((record.kind, record.key, record.status), ("recovery", "recovery:new-backup:create", "succeeded"))
        self.native["recoveryctl.create_live"].assert_called_once_with("new-backup", "requester", host=self.host,
            host_id="source", database="ORCL", backup_parent="/fixture/backup", **windows, policy=None)
        for name, actor, method in (("analyze_backup", "viewer", "analyze"), ("execute_backup", "operator", "execute")):
            with self.subTest(tool=name):
                conversation_id, action = self.prepare(name, {"request_id": "backup-a"}, actor=actor)
                response = self.execute(conversation_id, action, actor=actor)
                self.assertEqual(response["status"], 202, response)
                record = self.wait_run(response["body"]["run_id"])
                self.assertEqual((record.kind, record.key, record.status), ("recovery", f"recovery:backup-a:{method}", "succeeded"))
                args = ("backup-a", actor) if method == "execute" else ("backup-a",)
                self.native["recoveryctl." + method].assert_called_once_with(*args)
        self.native["recoveryctl.approve"].assert_not_called()
        self.native["recoveryctl.authorize"].assert_not_called()

    def test_offset_backup_window_reaches_native_handler_as_reviewed_utc(self):
        arguments = {"host_id": "source", "request_id": "offset-backup", "database": "ORCL",
            "backup_parent": "/fixture/backup", "window_start": "2099-01-01T06:30:00+05:30",
            "window_end": "2099-01-01T07:30:00+05:30"}
        conversation_id, action = self.prepare("create_backup", arguments, actor="requester")
        self.assertEqual(action["arguments"]["window_start"], "2099-01-01T01:00:00Z")
        self.assertEqual(action["arguments"]["window_end"], "2099-01-01T02:00:00Z")
        response = self.execute(conversation_id, action, actor="requester")
        self.assertEqual(response["status"], 202, response)
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual(record.status, "succeeded", record.error)
        self.native["recoveryctl.create_live"].assert_called_once_with("offset-backup", "requester",
            host=self.host, host_id="source", database="ORCL", backup_parent="/fixture/backup",
            window_start="2099-01-01T01:00:00Z", window_end="2099-01-01T02:00:00Z", policy=None)

    def test_confirmed_host_pipeline_uses_shared_host_key_and_server_owned_context(self):
        for name, step, arguments in (("refresh_discovery", "discovery", {"host_id": "source"}),
                                     ("check_live_inventory", "discovery", {"host_id": "source"}),
                                     ("refresh_readiness", "readiness-chain", {"host_id": "source"}),
                                     ("select_backup", "recovery-collect", {"host_id": "source", "request_id": "backup-a"})):
            with self.subTest(tool=name):
                conversation_id, action = self.prepare(name, arguments, actor="operator")
                response = self.execute(conversation_id, action, actor="operator")
                self.assertEqual(response["status"], 202, response)
                record = self.wait_run(response["body"]["run_id"])
                self.assertEqual((record.kind, record.key, record.status), ("pipeline", "host:source:pipeline", "succeeded"))
                body = {"actor": "operator", "requester": "operator",
                        "expected_configuration_sha256": assistant.live_inventory.configuration_digest(self.host)}
                if step == "readiness-chain":
                    body["_record"] = record
                    body["expected_action_binding_sha256"] = assistant._read(
                        assistant._path("operator", conversation_id), "operator")["actions"][0]["binding"]
                if step == "recovery-collect":
                    body["request_id"] = "backup-a"
                if name == "check_live_inventory":
                    body["inventory_receipt"] = True
                self.steps[step].assert_called_once_with("source", self.host, body)
                self.steps[step].reset_mock()

    def test_confirmed_readiness_rejects_malformed_binding_and_input_overrides(self):
        path = "/api/hosts/source/pipeline/readiness-chain"
        cases = [{"expected_action_binding_sha256": value}
                 for value in (None, [], 42, "", "a" * 63, "A" * 64)]
        cases.extend({"expected_action_binding_sha256": "a" * 64, field: value}
                     for field, value in (("artifact_dir", "/other/patch"), ("procedure", {}), ("policy", {})))
        for body in cases:
            with self.subTest(body=body):
                response = self.request(path, method="POST", actor="operator", body=body)
                self.assertEqual(response["status"], 400, response)
                self.assertEqual(response["body"]["error"], "invalid_confirmation")
        self.assertFalse(pipeline_runner.RUNS)
        self.assert_no_native_calls()

    def test_direct_readiness_refresh_keeps_explicit_wizard_inputs(self):
        body = {"artifact_dir": "/fixture/patch", "procedure": {"fixture": "reviewed"},
                "policy": {"fixture": "reviewed"}}
        response = self.request("/api/hosts/source/pipeline/readiness-chain", method="POST",
                                actor="operator", body=body)
        self.assertEqual(response["status"], 202, response)
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual(record.status, "succeeded", record.error)
        self.steps["readiness-chain"].assert_called_once_with("source", self.host,
            {**body, "actor": "operator", "requester": "operator", "_record": record})

    def test_confirmed_readiness_rechecks_saved_inputs_after_worker_queue_delay(self):
        original_start = pipeline_runner.start_run
        entered, release = threading.Event(), threading.Event()

        def delayed_start(kind, key, function):
            def delayed(record):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Readiness worker barrier timed out")
                return function(record)
            return original_start(kind, key, delayed)

        conversation_id, action = self.prepare("refresh_readiness", {"host_id": "source"}, actor="operator")
        try:
            with patch.object(pipeline_runner, "start_run", side_effect=delayed_start):
                response = self.execute(conversation_id, action, actor="operator")
            self.assertEqual(response["status"], 202, response)
            self.assertTrue(entered.wait(2))
            # Keep the prerequisites valid while changing the reviewed policy.
            policy = evidence.read_evidence("source", "policy") or {}
            evidence.write_evidence("source", "policy", {**policy, "maximum_snapshot_age_seconds": 600})
        finally:
            release.set()
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual(record.status, "failed", record.error)
        self.assertIn("changed after confirmation", record.error["message"])
        self.assert_no_native_calls()

    def test_confirmed_readiness_unchanged_saved_inputs_reach_native_worker(self):
        conversation_id, action = self.prepare("refresh_readiness", {"host_id": "source"}, actor="operator")
        saved = assistant._read(assistant._path("operator", conversation_id), "operator")["actions"][0]
        response = self.execute(conversation_id, action, actor="operator")
        self.assertEqual(response["status"], 202, response)
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual(record.status, "succeeded", record.error)
        self.steps["readiness-chain"].assert_called_once_with("source", self.host, {
            "actor": "operator", "requester": "operator", "_record": record,
            "expected_configuration_sha256": assistant.live_inventory.configuration_digest(self.host),
            "expected_action_binding_sha256": saved["binding"],
        })

    def test_confirmation_rechecks_current_roles_and_does_not_allow_another_owner(self):
        conversation_id, action = self.prepare("refresh_discovery", {"host_id": "source"}, actor="operator")
        self.assertEqual(self.execute(conversation_id, action, actor="admin")["status"], 404)
        self.roles["operator"] = ["viewer"]
        self.write_principals()
        self.assertEqual(self.execute(conversation_id, action, actor="operator")["status"], 403)
        self.assertEqual(assistant.get("operator", conversation_id)["actions"][0]["state"], "pending")
        self.assertFalse(pipeline_runner.RUNS)
        self.assert_no_native_calls()

    def test_cached_backup_changes_and_malformed_attribution_cannot_launch_a_confirmation(self):
        metadata_file = server.recoveryctl.LIVE_DIR / "backup-a" / "metadata.json"
        original = metadata_file.read_bytes()
        for change, status in (({"analysis": {"status": "blocked", "findings": ["Fixture capacity blocker"]}}, 409),
                               ({"host_id": []}, 400)):
            with self.subTest(change=change):
                metadata_file.write_bytes(original)
                conversation_id, action = self.prepare("execute_backup", {"request_id": "backup-a"}, actor="operator")
                changed = json.loads(original)
                changed.update(change)
                metadata_file.write_text(json.dumps(changed))
                response = self.execute(conversation_id, action, actor="operator")
                self.assertEqual(response["status"], status, response)
                self.assertFalse(pipeline_runner.RUNS)
                self.assert_no_native_calls()

    def test_native_dispatch_rechecks_role_after_the_proposal_binding_check(self):
        conversation_id, action = self.prepare("refresh_discovery", {"host_id": "source"}, actor="operator")
        original_binding = assistant.capabilities.binding
        def binding_then_revoke(*args, **kwargs):
            result = original_binding(*args, **kwargs)
            self.roles["operator"] = ["viewer"]
            self.write_principals()
            return result
        with patch.object(assistant.capabilities, "binding", side_effect=binding_then_revoke):
            response = self.execute(conversation_id, action, actor="operator")
        self.assertEqual(response["status"], 403, response)
        self.assertEqual(assistant.get("operator", conversation_id)["actions"][0]["state"], "failed")
        self.assertFalse(pipeline_runner.RUNS)
        self.assert_no_native_calls()

    def test_confirmed_host_proposal_rechecks_approved_configuration_before_native_launch(self):
        windows = {"window_start": "2099-01-01T01:00:00Z", "window_end": "2099-01-01T02:00:00Z"}
        cases = [("refresh_discovery", {"host_id": self.host["id"]}, "operator"),
                 ("check_live_inventory", {"host_id": self.host["id"]}, "operator"),
                 ("refresh_readiness", {"host_id": self.host["id"]}, "operator"),
                 ("select_backup", {"host_id": self.host["id"], "request_id": "backup-a"}, "operator"),
                 ("create_backup", {"host_id": self.host["id"], "request_id": "new-bound-backup",
                    "database": "ORCL", "backup_parent": "/fixture/backup", **windows}, "requester")]
        original_submit = server.Handler._submit_assistant_action
        def change_config_then_submit(handler, path, body):
            self.assertEqual(body.get("expected_configuration_sha256"),
                             assistant.live_inventory.configuration_digest(self.host))
            changed = {**self.host, "ssh_alias": "changed-after-confirmation.invalid"}
            self.hosts_file.write_text(json.dumps({"hosts": [changed]}))
            self.original_hosts = self.hosts_file.read_bytes()
            return original_submit(handler, path, body)
        for tool, arguments, actor in cases:
            with self.subTest(tool=tool):
                self.hosts_file.write_text(json.dumps({"hosts": [self.host]}))
                self.original_hosts = self.hosts_file.read_bytes()
                conversation_id, action = self.prepare(tool, arguments, actor=actor)
                with patch.object(server.Handler, "_submit_assistant_action", change_config_then_submit):
                    response = self.execute(conversation_id, action, actor=actor)
                self.assertEqual(response["status"], 409, response)
                saved = assistant.get(actor, conversation_id)["actions"][0]
                self.assertEqual(saved["state"], "failed")
                self.assertFalse(pipeline_runner.RUNS)
                self.assert_no_native_calls()

    def configure_plan_nodes(self):
        self.host.update(remote_root="/fixture/runtime", nodes=[{"name": "source", "ssh_alias": "reviewed.invalid"}])
        self.hosts_file.write_text(json.dumps({"hosts": [self.host]}))
        self.original_hosts = self.hosts_file.read_bytes()
        self.saved_plan.update(nodes=["source"], host_id="source")
        self.enterContext(patch.object(server.planctl, "_load_hosts", side_effect=server.load_hosts))

    def change_plan_route(self):
        changed = {**self.host, "nodes": [{"name": "source", "ssh_alias": "unreviewed.invalid"}]}
        self.hosts_file.write_text(json.dumps({"hosts": [changed]}))
        self.original_hosts = self.hosts_file.read_bytes()

    def test_confirmed_plan_rejects_route_drift_between_confirmation_and_native_admission(self):
        self.configure_plan_nodes()
        original_submit = server.Handler._submit_assistant_action
        def drift_then_submit(handler, path, body):
            self.assertRegex(body.get("expected_action_binding_sha256", ""), r"^[a-f0-9]{64}$")
            self.change_plan_route()
            return original_submit(handler, path, body)
        for tool in ("dispatch_plan", "execute_plan"):
            self.hosts_file.write_text(json.dumps({"hosts": [self.host]}))
            self.original_hosts = self.hosts_file.read_bytes()
            conversation_id, action = self.prepare(tool, {"plan_id": "plan-a"}, actor="operator")
            with patch.object(server.Handler, "_submit_assistant_action", drift_then_submit):
                response = self.execute(conversation_id, action, actor="operator")
            self.assertEqual(response["status"], 409, response)
            self.assertEqual(assistant.get("operator", conversation_id)["actions"][0]["state"], "failed")
        self.assertFalse(pipeline_runner.RUNS)
        self.assert_no_native_calls()

    def test_confirmed_plan_rechecks_route_after_worker_queue_delay(self):
        self.configure_plan_nodes()
        original_start = pipeline_runner.start_run
        for tool in ("dispatch_plan", "execute_plan"):
            self.hosts_file.write_text(json.dumps({"hosts": [self.host]}))
            self.original_hosts = self.hosts_file.read_bytes()
            entered, release = threading.Event(), threading.Event()
            def delayed_start(kind, key, function):
                def delayed(record):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("Worker barrier timed out")
                    return function(record)
                return original_start(kind, key, delayed)
            conversation_id, action = self.prepare(tool, {"plan_id": "plan-a"}, actor="operator")
            try:
                with patch.object(pipeline_runner, "start_run", side_effect=delayed_start):
                    response = self.execute(conversation_id, action, actor="operator")
                self.assertEqual(response["status"], 202, response)
                self.assertTrue(entered.wait(2))
                self.change_plan_route()
            finally:
                release.set()
            record = self.wait_run(response["body"]["run_id"])
            self.assertEqual(record.status, "failed", record.error)
            self.assertIn("changed after confirmation", record.error["message"])
        self.assert_no_native_calls()

    def test_confirmed_execution_pins_routes_across_native_tasks_and_keeps_other_threads_independent(self):
        self.configure_plan_nodes()
        selected = []
        entered, release = threading.Event(), threading.Event()
        tasks = [{"task_id": "001-precheck-source", "node": "source", "stage": "precheck"},
                 {"task_id": "002-apply-source", "node": "source", "stage": "apply"}]
        self.native["planctl.execute_remaining_tasks"].side_effect = self.native_original["planctl.execute_remaining_tasks"]
        self.enterContext(patch.object(server.planctl, "next_task", side_effect=tasks))
        self.enterContext(patch.object(server.planctl, "_require_controller_transport"))
        def remote_boundary(plan_id, plan, task, actor):
            target = server.planctl._resolve_live_host_for_task(plan, task)
            selected.append(target["ssh_alias"])
            if len(selected) == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Executor barrier timed out")
            else:
                self.saved_plan["state"] = "succeeded"
            return {"task_id": task["task_id"], "status": "succeeded"}
        self.enterContext(patch.object(server.planctl, "_execute_live", side_effect=remote_boundary))
        conversation_id, action = self.prepare("execute_plan", {"plan_id": "plan-a"}, actor="operator")
        try:
            response = self.execute(conversation_id, action, actor="operator")
            self.assertEqual(response["status"], 202, response)
            self.assertTrue(entered.wait(2))
            self.change_plan_route()
            # This thread must see current inventory while the confirmed worker
            # continues with the reviewed snapshot, including its second task.
            current = server.planctl._resolve_live_host_for_task(self.saved_plan, tasks[0])
            self.assertEqual(current["ssh_alias"], "unreviewed.invalid")
        finally:
            release.set()
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual(record.status, "succeeded", record.error)
        self.assertEqual(record.result["executed_count"], 2)
        self.assertEqual(selected, ["reviewed.invalid", "reviewed.invalid"])

    def test_plan_proposal_binds_nodes_outside_its_display_host(self):
        self.configure_plan_nodes()
        other = {"id": "other", "ssh_alias": "other-reviewed.invalid", "remote_root": "/fixture/other"}
        self.saved_plan["nodes"].append("other")
        self.hosts_file.write_text(json.dumps({"hosts": [self.host, other]}))
        self.original_hosts = self.hosts_file.read_bytes()
        conversation_id, action = self.prepare("execute_plan", {"plan_id": "plan-a"}, actor="operator")
        other["ssh_alias"] = "other-unreviewed.invalid"
        self.hosts_file.write_text(json.dumps({"hosts": [self.host, other]}))
        self.original_hosts = self.hosts_file.read_bytes()
        response = self.execute(conversation_id, action, actor="operator")
        self.assertEqual(response["status"], 409, response)
        self.assertEqual(assistant.get("operator", conversation_id)["actions"][0]["state"], "expired")
        self.assertFalse(pipeline_runner.RUNS)
        self.assert_no_native_calls()

    def test_native_dispatch_reauthenticates_revoked_token_after_binding_check(self):
        original_binding = assistant.capabilities.binding
        for change in ({"disabled": True}, {"expires_at": "2000-01-01T00:00:00Z"},
                       {"token_sha256": hashlib.sha256(b"replacement-fixture-token").hexdigest()}):
            with self.subTest(change=list(change)):
                self.write_principals()
                conversation_id, action = self.prepare("refresh_discovery", {"host_id": self.host["id"]}, actor="operator")
                def binding_then_revoke(*args, **kwargs):
                    binding = original_binding(*args, **kwargs)
                    principals = json.loads(self.principals.read_text())
                    entry = next(item for item in principals["principals"] if item["actor"] == "operator")
                    entry.update(change)
                    self.principals.write_text(json.dumps(principals))
                    return binding
                with patch.object(assistant.capabilities, "binding", side_effect=binding_then_revoke):
                    response = self.execute(conversation_id, action, actor="operator")
                self.assertEqual(response["status"], 401, response)
                self.assertEqual(assistant.get("operator", conversation_id)["actions"][0]["state"], "failed")
                self.assertFalse(pipeline_runner.RUNS)
                self.assert_no_native_calls()

    def test_native_dispatch_reauthenticates_expired_or_changed_company_session(self):
        self.company_configured.return_value = True
        headers = {"Cookie": server.company_auth.SESSION_COOKIE + "=fixture-session",
                   "X-CSRF-Token": "fixture-csrf", "Origin": "https://controller.fixture.invalid"}
        session = {"actor": "employee", "roles": ["operator"]}
        for final, status in ((auth.AuthError("Fixture session expired", status=401), 401),
                              ({"actor": "different-employee", "roles": ["operator"]}, 403),
                              ({"actor": "employee", "roles": ["viewer"]}, 403)):
            with self.subTest(status=status, value_type=type(final).__name__):
                # Both HTTP POSTs authenticate before and after their body;
                # only the final native-dispatch check sees the revocation.
                with patch.object(server.company_auth, "authenticate", side_effect=[session] * 4 + [final]) as authenticate:
                    conversation = self.create(actor=None, headers=headers)
                    assistant._proposal("employee", conversation["id"], "refresh_discovery",
                                        {"host_id": self.host["id"]}, server.load_hosts())
                    action = assistant.get("employee", conversation["id"])["actions"][0]
                    response = self.execute(conversation["id"], action, actor=None, headers=headers)
                self.assertEqual(response["status"], status, response)
                self.assertEqual(authenticate.call_count, 5)
                self.assertEqual(assistant.get("employee", conversation["id"])["actions"][0]["state"], "failed")
                self.assertFalse(pipeline_runner.RUNS)
                self.assert_no_native_calls()

    def test_company_session_identity_and_csrf_gate_are_preserved_for_nested_native_dispatch(self):
        self.company_configured.return_value = True
        headers = {"Cookie": server.company_auth.SESSION_COOKIE + "=fixture-session", "X-CSRF-Token": "fixture-csrf", "Origin": "https://controller.fixture.invalid"}
        session = {"actor": "employee", "roles": ["operator"]}
        with patch.object(server.company_auth, "authenticate", return_value=session) as authenticate:
            conversation = self.create(actor=None, headers=headers)
            assistant._proposal("employee", conversation["id"], "refresh_discovery", {"host_id": "source"}, server.load_hosts())
            action = assistant.get("employee", conversation["id"])["actions"][0]
            response = self.execute(conversation["id"], action, actor=None, headers=headers)
            self.assertEqual(response["status"], 202, response)
            self.wait_run(response["body"]["run_id"])
            self.steps["discovery"].assert_called_once_with("source", self.host, {"actor": "employee", "requester": "employee",
                "expected_configuration_sha256": assistant.live_inventory.configuration_digest(self.host)})
            self.assertEqual(authenticate.call_count, 5, "Both POST bodies and native dispatch must reauthenticate the company session")
            authenticate.assert_called_with(headers["Cookie"], method="POST", csrf="fixture-csrf", origin=headers["Origin"])
        with patch.object(server.company_auth, "authenticate", side_effect=auth.AuthError("CSRF verification failed", status=403)):
            self.assertEqual(self.request(method="POST", actor=None, headers={"Cookie": headers["Cookie"]})["status"], 403)
        self.assertEqual(len(assistant.list_conversations("employee")), 1)

    def test_model_calls_cannot_approve_authorize_spoof_an_actor_or_run_an_unknown_tool(self):
        conversation = self.create()
        bad_calls = [("approve_plan", {"plan_id": "plan-a", "approval_ticket": "invented"}),
                     ("authorize_plan", {"plan_id": "plan-a"}),
                     ("execute_plan", {"plan_id": "plan-a", "actor": "operator"}),
                     ("refresh_discovery", {"host_id": "https://attacker.invalid"}),
                     ("shell", {"command": "pretend-command"})]
        self.model.side_effect = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                for index, (name, arguments) in enumerate(bad_calls)]},
            {"role": "assistant", "content": "These requests cannot be performed through chat.", "tool_calls": []},
        ]
        response = self.request(f"/api/assistant/conversations/{conversation['id']}/messages", method="POST",
            body={"content": "Private fixture prompt: approve and run everything"})
        self.assertEqual(response["status"], 202, response)
        record = self.wait_run(response["body"]["run_id"])
        self.assertEqual((record.kind, record.status), ("assistant", "succeeded"))
        self.assertRegex(record.key, f"^assistant:{hashlib.sha256(b'admin').hexdigest()}:{conversation['id']}:[a-f0-9]{{32}}$")
        saved = assistant.get("admin", conversation["id"])
        self.assertEqual(saved["actions"], [])
        self.assertEqual(saved["messages"][-1]["content"], "These requests cannot be performed through chat.")
        offered = {tool["function"]["name"] for tool in self.model.call_args_list[0].args[1]}
        self.assertFalse(offered.intersection({"approve_plan", "authorize_plan", "shell"}))
        tool_results = [json.loads(message["content"]) for message in self.model.call_args_list[1].args[0] if message["role"] == "tool"]
        self.assertEqual(len(tool_results), len(bad_calls))
        self.assertTrue(all("error" in result for result in tool_results))
        public_run = self.request("/api/runs/" + record.run_id, actor="viewer")
        self.assertNotIn("Private fixture prompt", json.dumps(public_run["body"]))
        self.assert_no_native_calls()

    def test_valid_model_proposal_waits_for_explicit_confirmation(self):
        conversation = self.create(actor="operator")
        self.model.side_effect = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-discovery", "type": "function",
                "function": {"name": "refresh_discovery", "arguments": '{"host_id":"source"}'}}]},
            {"role": "assistant", "content": "Discovery is prepared for your review.", "tool_calls": []},
        ]
        response = self.request(f"/api/assistant/conversations/{conversation['id']}/messages", method="POST", actor="operator",
                                body={"content": "Refresh source discovery"})
        self.assertEqual(response["status"], 202, response)
        self.assertEqual(self.wait_run(response["body"]["run_id"]).status, "succeeded")
        action = assistant.get("operator", conversation["id"])["actions"][0]
        self.assertEqual(action["state"], "pending")
        self.assert_no_native_calls()
        self.assertEqual({record.kind for record in pipeline_runner.RUNS.values()}, {"assistant"})
        confirmed = self.execute(conversation["id"], action, actor="operator")
        self.assertEqual(confirmed["status"], 202, confirmed)
        self.assertEqual(self.wait_run(confirmed["body"]["run_id"]).key, "host:source:pipeline")
        self.steps["discovery"].assert_called_once()

    def test_existing_native_run_conflict_does_not_join_or_relaunch_the_action(self):
        release, entered = threading.Event(), threading.Event()
        def blocking_step(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release its isolated native worker")
            return {"status": "fixture"}
        self.steps["discovery"].side_effect = blocking_step
        active = self.request("/api/hosts/source/pipeline/discovery", method="POST", actor="operator")
        self.assertEqual(active["status"], 202)
        try:
            self.assertTrue(entered.wait(1))
            conversation_id, action = self.prepare("refresh_discovery", {"host_id": "source"}, actor="operator")
            result = self.execute(conversation_id, action, actor="operator")
            self.assertEqual(result["status"], 409, result)
            saved = assistant.get("operator", conversation_id)["actions"][0]
            self.assertEqual(saved["state"], "failed")
            self.assertNotIn("run_id", saved, "A conflict run belongs to the existing operation, not this confirmation")
            self.steps["discovery"].assert_called_once()
            self.assertEqual(len(pipeline_runner.RUNS), 1)
        finally:
            release.set()
            self.wait_run(active["body"]["run_id"])

    def test_native_failure_is_not_a_successful_or_repeatable_chat_action(self):
        self.native["planctl.execute_remaining_tasks"].side_effect = server.planctl.PlanError("Fixture native readiness blocker")
        conversation_id, action = self.prepare("execute_plan", {"plan_id": "plan-a"}, actor="operator")
        response = self.execute(conversation_id, action, actor="operator")
        self.assertEqual(response["status"], 202, response)
        self.assertEqual(self.wait_run(response["body"]["run_id"]).status, "failed")
        result = self.request("/api/assistant/conversations/" + conversation_id, actor="operator")["body"]["conversation"]["actions"][0]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["run_status"], "failed")
        self.assertEqual(self.execute(conversation_id, action, actor="operator")["status"], 409)
        self.native["planctl.execute_remaining_tasks"].assert_called_once()

    def test_disabled_fixture_routes_cannot_start_a_run(self):
        for path in ("/api/plans/testmode-demo", "/api/recovery/testmode-demo"):
            with self.subTest(path=path):
                response = self.request(path, method="POST", actor="requester", body={"request_id": "fixture", "plan_id": "fixture"})
                self.assertEqual(response["status"], 403, response)
                self.assertEqual(response["body"]["error"], "fixtures_disabled")
        self.assertFalse(pipeline_runner.RUNS)
        self.assert_no_native_calls()


if __name__ == "__main__":
    unittest.main(verbosity=2)
