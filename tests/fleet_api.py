#!/usr/bin/env python3
"""Actual Handler/auth/metadata integration in temporary files; no sockets or SSH."""
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import auth
import evidence
import fleet_metadata
import server


class FleetApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-fleet-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "fleet-metadata.json"
        self.hosts = self.root / "hosts.json"
        self.hosts.write_text(json.dumps({"hosts": [{"id": "source", "label": "Source", "ssh_alias": "fixture.invalid",
            "ssh_user": "fixture", "ssh_key": "fixture-private-key", "environment": "original", "desired_patch_baseline": "12345"}]}))
        self.original_hosts = self.hosts.read_bytes()
        principals = self.root / "principals.json"
        principals.write_text(json.dumps({"principals": [
            {"actor": role, "roles": [role], "token_sha256": hashlib.sha256((role + "-fixture-token").encode()).hexdigest()}
            for role in ("admin", "viewer", "requester", "approver", "operator")]}))
        self.enterContext(patch.dict(os.environ, {"OPU_WEBAPP_RBAC": "1", "OPU_WEBAPP_PRINCIPALS_FILE": str(principals),
            "OPU_PRODUCTION_MODE": "0", "OPU_OIDC_CONFIG": ""}))
        self.enterContext(patch.object(auth, "TOKEN_FILE", self.root / "api-token"))
        self.enterContext(patch.object(auth, "_CACHED_TOKEN", None))
        self.enterContext(patch.object(server.company_auth, "configured", return_value=False))
        self.enterContext(patch.object(server, "HOSTS_FILE", self.hosts))
        self.enterContext(patch.object(fleet_metadata, "STORE_FILE", self.store))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "evidence"))
        self.start = self.enterContext(patch.object(server.pipeline_runner, "start_run"))

    def request(self, path="/api/fleet", *, actor="admin", method="GET", body=None, headers=None, token=None):
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command = path, method
        handler.headers = Message()
        if actor is not None or token is not None:
            handler.headers["Authorization"] = "Bearer " + (token or actor + "-fixture-token")
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        raw = json.dumps(body if body is not None else {}).encode()
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        result = {"headers": {}}
        handler.send_response = lambda status: result.update(status=status)
        handler.send_header = lambda name, value: result["headers"].update({name: value})
        handler.end_headers = lambda: None
        getattr(handler, "do_" + method)()
        self.assertIn("status", result, "Handler must finish every request with an HTTP response")
        payload = handler.wfile.getvalue()
        result["body"] = json.loads(payload) if payload else None
        self.start.assert_not_called()
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts, "Fleet metadata must never rewrite host connection settings")
        return result

    def edit_body(self):
        row = self.request()["body"]["databases"][0]
        return {"expected_version": row["metadata_version"], "environment": "QA", "desired_patch_baseline": "39034528"}

    def post(self, body, **kwargs):
        return self.request("/api/fleet/hosts/source/metadata", method="POST", body=body, **kwargs)

    def test_admin_write_persists_to_sidecar_and_get_returns_effective_metadata(self):
        result = self.post(self.edit_body())
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.assertEqual(result["body"]["desired_patch_baseline"], "39034528")
        stored = json.loads(self.store.read_text())["hosts"]["source"]
        self.assertEqual(stored["updated_by"], "admin")
        self.assertNotIn("fixture-private-key", self.store.read_text())
        fetched = self.request(actor="viewer")["body"]
        row = fetched["databases"][0]
        self.assertEqual((row["environment"], row["desired_patch_baseline"]), ("QA", "39034528"))
        self.assertEqual(row["metadata_version"], result["body"]["metadata_version"])
        self.assertEqual(row["baseline_status"], "unknown")
        self.assertFalse(fetched["can_manage_metadata"])

    def test_edit_hint_and_mutation_follow_real_bearer_roles(self):
        body = self.edit_body()
        self.assertTrue(self.request()["body"]["can_manage_metadata"])
        for role in ("viewer", "requester", "approver", "operator"):
            with self.subTest(role=role):
                self.assertFalse(self.request(actor=role)["body"]["can_manage_metadata"])
                self.assertEqual(self.post(body, actor=role)["status"], 403)
        self.assertFalse(self.store.exists())

    def test_missing_auth_and_identity_spoofing_cannot_write(self):
        body = self.edit_body()
        self.assertEqual(self.post(body, actor=None)["status"], 401)
        self.assertEqual(self.post(body, headers={"X-OPU-Actor": "operator"})["status"], 403)
        self.assertEqual(self.post({**body, "actor": "operator"})["status"], 403)
        self.assertEqual(self.request(actor="operator", headers={"X-OPU-Actor": "admin"})["status"], 403)
        self.assertFalse(self.store.exists())

    def test_lab_token_requires_an_explicit_actor_for_metadata_audit(self):
        with patch.dict(os.environ, {"OPU_WEBAPP_RBAC": "0", "OPU_WEBAPP_PRINCIPALS_FILE": "", "OPU_WEBAPP_TOKEN": "lab-fixture-token"}):
            response = self.request(token="lab-fixture-token")
            self.assertTrue(response["body"]["can_manage_metadata"])
            body = {"expected_version": response["body"]["databases"][0]["metadata_version"], "environment": "test", "desired_patch_baseline": None}
            self.assertEqual(self.post(body, token="lab-fixture-token")["status"], 400)
            result = self.post(body, token="lab-fixture-token", headers={"X-OPU-Actor": "lab-operator"})
            self.assertEqual(result["status"], 200)
        self.assertEqual(json.loads(self.store.read_text())["hosts"]["source"]["updated_by"], "lab-operator")

    def test_unexpected_fields_missing_version_invalid_values_and_unknown_host_are_rejected(self):
        body = self.edit_body()
        for field in ("address", "ssh_alias", "ssh_key", "ssh_user", "requester", "actor"):
            with self.subTest(field=field):
                value = "admin" if field in {"actor", "requester"} else "changed"
                self.assertEqual(self.post({**body, field: value})["status"], 400)
        self.assertEqual(self.post({key: value for key, value in body.items() if key != "expected_version"})["status"], 400)
        self.assertEqual(self.post({**body, "desired_patch_baseline": "0"})["status"], 400)
        self.assertEqual(self.request("/api/fleet/hosts/absent/metadata", method="POST", body=body)["status"], 404)
        self.assertFalse(self.store.exists())

    def test_stale_form_gets_conflict_and_cannot_replace_the_newer_configuration(self):
        stale = self.edit_body()
        self.assertEqual(self.post(stale)["status"], 200)
        before = self.store.read_bytes()
        rejected = self.post({**stale, "environment": "different-team"})
        self.assertEqual(rejected["status"], 409)
        self.assertEqual(rejected["body"]["error"], "fleet_metadata_conflict")
        self.assertEqual(self.store.read_bytes(), before)
        latest = self.request()["body"]["databases"][0]
        cleared = self.post({"expected_version": latest["metadata_version"], "environment": None, "desired_patch_baseline": ""})
        self.assertEqual(cleared["status"], 200)
        self.assertIsNone(cleared["body"]["desired_patch_baseline"])

    def test_recovery_get_uses_one_configured_host_and_rejects_ambiguous_or_absent_hosts(self):
        with patch.object(server.recoveryctl, "target_capabilities", return_value={"targets": [{"database": "ORCL"}]}) as targets, \
             patch.object(server.recoveryctl, "list_requests", return_value=[]) as requests, \
             patch.object(server.recoveryctl, "capability", return_value={"live_available": True}):
            result = self.request("/api/recovery?host_id=source", actor="viewer")
            self.assertEqual(result["status"], 200)
            targets.assert_called_once_with("source")
            requests.assert_called_once_with(host_id="source")
            targets.reset_mock(); requests.reset_mock()
            for query, status in (("host_id=source&host_id=source", 400), ("host_id=", 400), ("host_id=absent", 404)):
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/recovery?" + query, actor="viewer")["status"], status)
            targets.assert_not_called(); requests.assert_not_called()

    def test_recovery_saved_view_is_explicit_and_rejects_ambiguous_views(self):
        with patch.object(server.recoveryctl, "target_capabilities", return_value={}), \
             patch.object(server.recoveryctl, "list_requests", return_value=[{"state": "unknown", "evidence_mode": "saved"}]) as requests:
            result = self.request("/api/recovery?host_id=source&view=saved", actor="viewer")
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["body"]["requests"][0]["evidence_mode"], "saved")
            requests.assert_called_once_with(host_id="source", saved_only=True)
            requests.reset_mock()
            for view in ("", "cached", "saved&view=live", "saved&view=saved"):
                with self.subTest(view=view):
                    result = self.request("/api/recovery?host_id=source&view=" + view, actor="viewer")
                    self.assertEqual(result["status"], 400)
                    self.assertEqual(result["body"]["error"], "invalid_view")
            requests.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
