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
        for module, methods in ((server.planctl, ("create", "dispatch", "execute_remaining_tasks", "approve", "authorize")),
                                (server.recoveryctl, ("create_live", "analyze", "execute", "approve", "authorize", "create_testmode_demo"))):
            for name in methods:
                self.native[module.__name__ + "." + name] = self.enterContext(patch.object(module, name, return_value={"state": "fixture"}))
        self.steps = {name: Mock(return_value={"status": "fixture"}) for name in ("discovery", "readiness-chain", "recovery-collect")}
        self.enterContext(patch.object(server.pipeline_steps, "STEPS", self.steps))
        evidence.write_evidence("source", "procedure_input", {
            "patch_id": "39034528", "target": {"database_unique_name": "ORCL"},
        })
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
                previous_runs = set(pipeline_runner.RUNS)
                conversation_id, action = self.prepare(name, arguments, actor=actor)
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

    def test_confirmed_host_pipeline_uses_shared_host_key_and_server_owned_context(self):
        for name, step, arguments in (("refresh_discovery", "discovery", {"host_id": "source"}),
                                     ("refresh_readiness", "readiness-chain", {"host_id": "source"}),
                                     ("select_backup", "recovery-collect", {"host_id": "source", "request_id": "backup-a"})):
            with self.subTest(tool=name):
                conversation_id, action = self.prepare(name, arguments, actor="operator")
                response = self.execute(conversation_id, action, actor="operator")
                self.assertEqual(response["status"], 202, response)
                record = self.wait_run(response["body"]["run_id"])
                self.assertEqual((record.kind, record.key, record.status), ("pipeline", "host:source:pipeline", "succeeded"))
                body = {"actor": "operator", "requester": "operator"}
                if step == "readiness-chain":
                    body["_record"] = record
                if step == "recovery-collect":
                    body["request_id"] = "backup-a"
                self.steps[step].assert_called_once_with("source", self.host, body)

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
            self.steps["discovery"].assert_called_once_with("source", self.host, {"actor": "employee", "requester": "employee"})
            self.assertEqual(authenticate.call_count, 2, "Authenticate each outer request; nested dispatcher retains that session")
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
