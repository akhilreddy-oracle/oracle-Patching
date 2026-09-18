#!/usr/bin/env python3
"""Recovery HTTP routing and identity tests with no server, sockets, or SSH."""
from email.message import Message
from datetime import datetime, timedelta, timezone
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import auth
import recoveryctl
import server


class RecoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opu-recovery-api-")
        self.addCleanup(self.tmp.cleanup)
        principals = Path(self.tmp.name) / "principals.json"
        principals.write_text(json.dumps({"principals": [
            {"actor": actor, "roles": [role], "token_sha256": hashlib.sha256((actor + "-token").encode()).hexdigest()}
            for actor, role in (("requester", "requester"), ("approver", "approver"), ("operator", "operator"), ("viewer", "viewer"))]}))
        self.enterContext(patch.dict(os.environ, {"OPU_WEBAPP_RBAC":"1", "OPU_WEBAPP_PRINCIPALS_FILE":str(principals), "OPU_PRODUCTION_MODE":"0"}))
        self.enterContext(patch.object(server.company_auth, "configured", return_value=False))
        self.host = {"id":"source", "ssh_alias":"source-alias", "remote_root":"/opt/opu", "sudo":True}
        self.submissions = []
        def submit(kind, key, work):
            self.submissions.append((kind, key, work(None)))
            return SimpleNamespace(run_id="a" * 12)
        self.start = self.enterContext(patch.object(server.pipeline_runner, "start_run", side_effect=submit))

    def request(self, path, *, actor="operator", body=None, method="POST"):
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command = path, method
        handler.headers = Message()
        handler.headers["Authorization"] = "Bearer " + actor + "-token"
        raw = json.dumps(body or {}).encode()
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        replies = []
        handler._send_json = lambda status, payload: replies.append((status,payload))
        handler.send_response = lambda status: replies.append((status,None))
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler._resolved_host = lambda host_id: self.host if host_id == self.host["id"] else None
        getattr(handler, "do_" + method)()
        return replies[-1]

    def body(self):
        return {"request_id":"backup-1", "host_id":"source", "database":"ORCL", "backup_parent":"/u02/backups",
                "window_start":"2026-09-15T12:00:00Z", "window_end":"2026-09-15T14:00:00Z",
                "policy":{"schema_version":"1.0", "maximum_snapshot_age_seconds":1800,"require_xml_inventory":True,
                          "database":{},"recovery":{"require_backup":True}}}

    def test_live_create_passes_only_configured_host_and_authenticated_requester(self):
        body = self.body()
        with patch.object(recoveryctl, "create_live", return_value={"state":"awaiting_approval"}) as create:
            self.assertEqual(self.request("/api/recovery", actor="requester", body=body)[0], 202)
            create.assert_called_once_with("backup-1", "requester", host=self.host, host_id="source", database="ORCL",
                backup_parent="/u02/backups",window_start=body["window_start"],window_end=body["window_end"],policy=body["policy"])
        self.assertEqual(self.submissions[0][:2], ("recovery", "recovery:backup-1:create"))

    def test_discovery_to_http_to_controller_preserves_the_recorded_route(self):
        # Retain the real discovery publisher, HTTP dispatch and recovery
        # controller. Only SSH/process boundaries use disposable transport.
        from recoveryctl_live import LiveRecoveryTests
        from runtime_fixture import runtime_receipt
        import pipeline_steps
        fixture = LiveRecoveryTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.host = fixture.host
        with patch.object(pipeline_steps.tools_sync, "ensure_host_tools", return_value=[runtime_receipt("source", "/opt/opu")]), \
             patch.object(pipeline_steps.remote, "run_remote_json", return_value=fixture.snapshot):
            pipeline_steps.step_discovery("sourcedb", self.host, {})
        with patch.object(recoveryctl, "list_requests", return_value=[]):
            status, guidance = self.request("/api/recovery?host_id=sourcedb", actor="viewer", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(guidance["target_capabilities"][0]["can_create"])
        now = datetime.now(timezone.utc); fmt = "%Y-%m-%dT%H:%M:%SZ"
        body = {**self.body(), "request_id": "r1", "host_id": "sourcedb", "policy": fixture.policy,
                "window_start": now.strftime(fmt), "window_end": (now + timedelta(hours=1)).strftime(fmt)}
        fixture.raw.side_effect = lambda *a, **k: fixture.response({"request_id": "r1", "state": "awaiting_approval"})
        self.assertEqual(self.request("/api/recovery", actor="requester", body=body)[0], 202)
        self.assertEqual(fixture.raw.call_args.args[0], "source")
        binding = recoveryctl._metadata("r1")["discovery_binding"]
        self.assertEqual(binding["route"], {"node": "sourcedb", "ssh_alias": "source"})
        self.host["nodes"] = [{"name": "other-node", "ssh_alias": "other-alias"}]
        fixture.hostfile.write_text(json.dumps({"hosts": [self.host]}))
        fixture.sync.reset_mock(); fixture.raw.reset_mock()
        with patch.object(recoveryctl, "list_requests", return_value=[]):
            status, guidance = self.request("/api/recovery?host_id=sourcedb", actor="viewer", method="GET")
        self.assertEqual(status, 200)
        self.assertFalse(guidance["target_capabilities"][0]["can_create"])
        self.assertIn("discovery_routing", [item["id"] for item in guidance["target_capabilities"][0]["blockers"]])
        status, _ = self.request("/api/recovery", actor="requester", body={**body, "request_id": "r2"})
        self.assertGreaterEqual(status, 400)
        fixture.sync.assert_not_called(); fixture.raw.assert_not_called()

    def test_create_role_identity_unknown_host_and_extra_transport_fields_fail_closed(self):
        with patch.object(recoveryctl, "create_live") as create:
            self.assertEqual(self.request("/api/recovery", actor="operator", body=self.body())[0], 403)
            for changes, code in (({"requester":"operator"},403), ({"host_id":"other"},400),
                                  ({"host":self.host},400), ({"ssh_alias":"other"},400), ({"database":{}},400)):
                self.assertEqual(self.request("/api/recovery", actor="requester", body={**self.body(),**changes})[0], code)
            create.assert_not_called()
        self.start.assert_not_called()

    def test_approval_sequence_uses_real_separate_principals(self):
        with patch.object(recoveryctl,"analyze",return_value={"status":"passed"}) as analyze, \
             patch.object(recoveryctl,"approve",return_value={"state":"approved"}) as approve, \
             patch.object(recoveryctl,"authorize",return_value={"state":"authorized"}) as authorize, \
             patch.object(recoveryctl,"execute",return_value={"state":"completed"}) as execute:
            for action,actor,body in (("analyze","viewer",{}),("approve","approver",{"approval_ticket":"CHG-42"}),
                                      ("authorize","operator",{}),("execute","operator",{})):
                self.assertEqual(self.request("/api/recovery/backup-1/" + action,actor=actor,body=body)[0],202)
            analyze.assert_called_once_with("backup-1")
            approve.assert_called_once_with("backup-1","approver","CHG-42")
            authorize.assert_called_once_with("backup-1","operator")
            execute.assert_called_once_with("backup-1","operator")
            self.assertEqual(self.request("/api/recovery/backup-1/approve",actor="requester",body={"actor":"approver","approval_ticket":"CHG-42"})[0],403)

    def test_recovery_run_inspection_dispatch_preserves_existing_launch(self):
        record = {"context":{"recovery_request_id":"backup-1"}}
        def reconcile(run_id, **kwargs):
            self.assertEqual(kwargs["actor"], "operator")
            return kwargs["inspect"](record)
        with patch.object(server.pipeline_runner,"reconcile_run",side_effect=reconcile), \
             patch.object(recoveryctl,"reconcile_detached_run",return_value={"status":"unknown"}) as inspect, \
             patch.object(recoveryctl,"reconcile") as restore, \
             patch.object(server.lockctl,"reconcile_execution_run") as ordinary:
            self.assertEqual(self.request("/api/runs/abcdef123456/reconcile",body={})[1]["status"], "unknown")
            inspect.assert_called_once_with(record)
            restore.assert_not_called()
            ordinary.assert_not_called()
        self.start.assert_not_called()

    def test_capability_is_explicit_and_detail_exposes_unknown_run(self):
        unknown={"run_id":"a"*12,"status":"unknown"}
        with patch.object(recoveryctl,"list_requests",return_value=[]), \
             patch.object(recoveryctl,"capability",return_value={"live_available":False,"live_reason":"not certified"}), \
             patch.object(recoveryctl,"status",return_value={"request_id":"backup-1","state":"running"}), \
             patch.object(server.pipeline_runner,"list_runs",return_value=[unknown]):
            self.assertEqual(self.request("/api/recovery",actor="viewer",method="GET")[1]["live_available"],False)
            detail=self.request("/api/recovery/backup-1",actor="viewer",method="GET")[1]
            self.assertEqual(detail["active_run"],unknown)
        self.start.assert_not_called()

    def test_live_status_transport_failure_is_structured(self):
        with patch.object(recoveryctl,"status",side_effect=server.remote.RemoteError("ssh_timeout","Host disconnected")):
            status, payload = self.request("/api/recovery/backup-1",actor="viewer",method="GET")
            self.assertIn(status,(502,504))
            self.assertEqual(payload["error"],"ssh_timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
