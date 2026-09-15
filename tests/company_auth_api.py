#!/usr/bin/env python3
"""Signed company-session HTTP integration without a server or real provider."""
from contextlib import redirect_stdout
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
try:
    import jwt  # noqa: F401 - select this test's dependency environment first
except ImportError:
    candidate = ROOT / ".venv/bin/python"
    if candidate.exists() and Path(sys.executable) != candidate:
        os.execv(str(candidate), [str(candidate), "-B", __file__, *sys.argv[1:]])
    raise SystemExit("Install webapp/requirements-sso.txt for company-login validation")
sys.path.insert(0, str(ROOT / "webapp"))
# Reuse the provider/RSA fixture without inheriting and rerunning its unit suite.
spec = importlib.util.spec_from_file_location("signed_login_fixtures", ROOT / "tests/company_auth.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
import auth
import company_auth
import server


class CompanyApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LoginTests.setUpClass()

    def setUp(self):
        self.fx = fixtures.LoginTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.start = self.enterContext(patch.object(server.pipeline_runner, "start_run"))
        self.execute = self.enterContext(patch.object(server.planctl, "execute_next_task"))
        self.create = self.enterContext(patch.object(server.planctl, "create"))
        self.approve = self.enterContext(patch.object(server.planctl, "approve"))

    def session(self, groups=None):
        if groups is not None:
            self.fx.claim_changes = {"groups": groups}
        cookie = self.fx.finish()
        return cookie, company_auth.authenticate(cookie)

    def request(self, path, *, cookie=None, method="GET", body=None, headers=None):
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command = path, method
        handler.headers = Message()
        if cookie is not None:
            handler.headers["Cookie"] = cookie
        for key, value in (headers or {}).items():
            handler.headers[key] = value
        raw = json.dumps(body or {}).encode()
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        result = {"headers": []}
        def response(status):
            result["status"] = status
            # Exercise the real access logger using the full sensitive URL.
            handler.log_message('"%s %s HTTP/1.1" %s', method, path, status)
        handler.send_response = response
        handler.send_header = lambda name, value: result["headers"].append((name,value))
        handler.end_headers = lambda: None
        handler.address_string = lambda: "127.0.0.1"
        handler._resolved_host = lambda host_id: {"id":host_id}
        output = io.StringIO()
        with redirect_stdout(output):
            getattr(handler, "do_" + method)()
        payload = handler.wfile.getvalue()
        result["body"] = json.loads(payload) if payload else None
        result["logs"] = output.getvalue()
        return result

    @staticmethod
    def mutation_headers(session):
        return {"X-CSRF-Token":session["csrf_token"], "Origin":"https://patching.example"}

    def assert_no_mutation(self):
        self.start.assert_not_called()
        self.execute.assert_not_called()
        self.create.assert_not_called()
        self.approve.assert_not_called()

    def test_cookie_identity_has_priority_over_bearer_and_exposes_session_fields(self):
        cookie, session = self.session()
        with patch.object(auth, "require_api_auth", side_effect=AssertionError("Cookie must win over bearer")):
            reply = self.request("/api/session", cookie=cookie, headers={"Authorization":"Bearer unrelated-service-token"})
        self.assertEqual(reply["status"], 200)
        self.assertEqual(reply["body"]["actor"], session["actor"])
        self.assertEqual(reply["body"]["roles"], ["operator"])
        self.assertEqual(reply["body"]["expires_at"], session["expires_at"])
        self.assertEqual(reply["body"]["csrf_token"], session["csrf_token"])
        self.assertEqual(reply["body"]["mode"], "company")
        self.assertTrue(reply["body"]["rbac_enabled"])
        self.assertNotIn("groups", reply["body"])
        self.assertNotIn("id_token", reply["body"])
        self.assertIn(("Cache-Control", "no-store"), reply["headers"])
        self.assert_no_mutation()

    def test_csrf_or_origin_failure_never_submits_plan_work(self):
        cookie, session = self.session()
        for headers in ({}, {"X-CSRF-Token":"wrong"}, {**self.mutation_headers(session),"Origin":"https://other.example"}):
            reply = self.request("/api/plans/p/execute-next", cookie=cookie, method="POST", headers=headers)
            self.assertEqual(reply["status"], 403)
        self.assert_no_mutation()

    def test_viewer_cannot_execute_even_with_valid_company_csrf(self):
        cookie, session = self.session(["readers"])
        reply = self.request("/api/plans/p/execute-next", cookie=cookie, method="POST", headers=self.mutation_headers(session))
        self.assertEqual(reply["status"], 403)
        self.assert_no_mutation()

    def test_forged_actor_requester_and_actor_header_are_rejected(self):
        self.fx.settings["group_roles"]["all-actions"] = ["requester","approver","operator"]
        self.fx.save()
        cookie, session = self.session(["all-actions"])
        for field in ("actor","requester"):
            reply = self.request("/api/plans", cookie=cookie, method="POST", body={field:"different-person"}, headers=self.mutation_headers(session))
            self.assertEqual(reply["status"],403)
        reply = self.request("/api/session", cookie=cookie, headers={"X-OPU-Actor":"different-person"})
        self.assertEqual(reply["status"],403)
        self.assert_no_mutation()

    def test_logout_revokes_the_server_side_session_and_expires_cookie(self):
        cookie, session = self.session()
        reply = self.request("/api/auth/logout", cookie=cookie, method="POST", headers=self.mutation_headers(session))
        self.assertEqual(reply["status"],204)
        self.assertTrue(any(name == "Set-Cookie" and "Max-Age=0" in value for name,value in reply["headers"]))
        self.assertEqual(self.request("/api/session", cookie=cookie)["status"],401)
        self.assert_no_mutation()

    def test_login_callback_uses_signed_token_without_logging_code_or_tokens(self):
        begin = self.request("/auth/login")
        self.assertEqual(begin["status"],303)
        location = next(value for name,value in begin["headers"] if name == "Location")
        query = parse_qs(urlsplit(location).query)
        self.fx.nonce = query["nonce"][0]
        browser_cookie = next(value.split(";")[0] for name,value in begin["headers"] if name == "Set-Cookie")
        code = "sensitive-fixture-authorization-code"
        callback = self.request("/auth/callback?" + urlencode({"state":query["state"][0],"code":code}), cookie=browser_cookie)
        self.assertEqual(callback["status"],303)
        self.assertIn(("Location","/"),callback["headers"])
        for value in (code, query["state"][0], "id_token", "code_verifier"):
            self.assertNotIn(value, begin["logs"] + callback["logs"])
        self.assertIn("/auth/callback", callback["logs"])
        session_cookie = next(value.split(";")[0] for name,value in callback["headers"] if name == "Set-Cookie" and value.startswith(company_auth.SESSION_COOKIE+"="))
        self.assertEqual(self.request("/api/session",cookie=session_cookie)["status"],200)
        self.assert_no_mutation()

    def test_approval_inbox_is_readable_without_granting_mutation_rights(self):
        cookie, session = self.session(["readers"])
        window = {"start":"2030-01-01T10:00:00Z", "end":"2030-01-01T12:00:00Z"}
        plans = [{"plan_id":"p","state":"awaiting_approval","requester":session["actor"], "maintenance_window":window},
                 {"plan_id":"done","state":"completed","requester":"other"}]
        recoveries = [{"request_id":"r","state":"approved","requester":"other","host_id":"source"}]
        with patch.object(server.planctl,"list_plans",return_value=plans), \
             patch.object(server.recoveryctl,"list_requests",return_value=recoveries):
            reply = self.request("/api/approvals",cookie=cookie)
        self.assertEqual(reply["status"],200)
        self.assertEqual([(item["kind"],item["id"]) for item in reply["body"]["items"]],[("plan","p"),("recovery","r")])
        self.assertTrue(reply["body"]["items"][0]["self_requested"])
        self.assertEqual(reply["body"]["items"][0]["window"], window)
        denied = self.request("/api/plans/p/approve",cookie=cookie,method="POST",body={"approval_ticket":"CHG-42"},headers=self.mutation_headers(session))
        self.assertEqual(denied["status"],403)
        self.assert_no_mutation()


if __name__ == "__main__":
    unittest.main(verbosity=2)
