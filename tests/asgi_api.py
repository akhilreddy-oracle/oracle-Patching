#!/usr/bin/env python3
"""ASGI security/dispatch regressions using isolated state and no live services.

HTTPX exercises the application; direct ASGI messages preserve malformed headers
and provide a real body-stream barrier that HTTP clients normally normalize away.
Only native execution, notifications, and the signed identity-provider fixture are
replaced. Authentication, routing, CSRF, assistant confirmation, and runs are real.
"""
import asyncio
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
try:
    import fastapi  # noqa: F401
    import httpx
    import jwt  # noqa: F401
except ImportError:
    candidate = ROOT / ".venv/bin/python"
    if candidate.exists() and Path(sys.executable) != candidate:
        os.execv(str(candidate), [str(candidate), "-B", __file__, *sys.argv[1:]])
    raise SystemExit("Install the webapp ASGI and company-login dependencies")

sys.path.insert(0, str(ROOT / "webapp"))
import api
import api_transport
import assistant
import auth
import company_auth
import evidence
import pipeline_runner
import remote
import server

spec = importlib.util.spec_from_file_location("asgi_signed_login_fixtures", ROOT / "tests/company_auth.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)

LIMIT = 1024 * 1024
EXECUTE = "/api/plans/plan-a/execute-next"


class AsgiApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LoginTests.setUpClass()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="opu-asgi-tests-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.principals = self.root / "principals.json"
        self.entries = [{"actor": actor, "roles": [actor],
                         "token_sha256": hashlib.sha256((actor + "-token").encode()).hexdigest()}
                        for actor in ("admin", "viewer", "requester", "approver", "operator")]
        self.write_principals()
        self.enterContext(patch.dict(os.environ, {
            "OPU_WEBAPP_PRINCIPALS_FILE": str(self.principals), "OPU_WEBAPP_RBAC": "1",
            "OPU_WEBAPP_TOKEN": "shared-lab-token", "OPU_PRODUCTION_MODE": "0",
            "OPU_OIDC_CONFIG": "", "OPU_ITSM_REQUIRED": "0", "OPU_WEBAPP_ALLOW_FIXTURES": "0",
        }))
        self.enterContext(patch.object(auth, "TOKEN_FILE", self.root / "api-token"))
        self.enterContext(patch.object(auth, "_CACHED_TOKEN", None))
        self.host = {"id": "source", "label": "Source fixture", "ssh_alias": "fixture.invalid",
                     "ssh_user": "fixture", "ssh_key": "/never-read-fixture-key"}
        hosts = self.root / "hosts.json"
        hosts.write_text(json.dumps({"hosts": [self.host]}))
        self.enterContext(patch.object(server, "HOSTS_FILE", hosts))
        self.enterContext(patch.object(assistant, "STATE_DIR", self.root / "assistant"))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "hosts"))
        self.enterContext(patch.object(server.planctl, "PLAN_STATE_DIR", self.root / "plans"))
        self.enterContext(patch.object(server.recoveryctl, "LIVE_DIR", self.root / "recovery-live"))
        self.enterContext(patch.object(server.recoveryctl, "RECOVERY_DIR", self.root / "recovery"))
        self.enterContext(patch.object(pipeline_runner, "RUNS_DIR", self.root / "runs"))
        self.enterContext(patch.object(pipeline_runner, "RUNS", {}))
        self.enterContext(patch.object(pipeline_runner, "_ACTIVE_KEYS", {}))
        self.enterContext(patch.object(server.notifications, "emit"))
        self.enterContext(patch.object(server.local_llm, "config_status", return_value={
            "enabled": True, "configured": True, "model": "fixture-local", "provider": "ollama", "reason": None,
        }))
        self.model = self.enterContext(patch.object(server.local_llm, "complete",
            side_effect=AssertionError("ASGI regressions must not contact a model")))
        for name in ("run_remote_raw", "run_remote", "run_remote_shell", "pipe_remote", "push_file", "pull_file"):
            self.enterContext(patch.object(remote, name, side_effect=AssertionError("No live SSH: " + name)))

    async def asyncSetUp(self):
        self.app = api.create_app()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://patching.example")
        self.addAsyncCleanup(self.client.aclose)

    def write_principals(self):
        self.principals.write_text(json.dumps({"principals": self.entries}))
        self.principals.chmod(0o600)

    async def request(self, path, *, actor="operator", method="GET", body=None, headers=None):
        fields = {} if actor is None else {"Authorization": "Bearer " + actor + "-token"}
        fields.update(headers or {})
        kwargs = {"headers": fields}
        if method == "POST":
            kwargs["json"] = {} if body is None else body
        return await self.client.request(method, path, **kwargs)

    async def raw_request(self, path=EXECUTE, *, method="POST", headers=None, chunks=None, barrier=None,
                          add_length=True, disconnect_after=None):
        """Preserve supplied framing; default to the exact body length."""
        parsed = urlsplit(path)
        parts = list(chunks if chunks is not None else [b"{}"])
        headers = list(headers) if headers is not None else [(b"authorization", b"Bearer operator-token")]
        if add_length and not any(name.lower() in {b"content-length", b"transfer-encoding"} for name, _ in headers):
            headers.append((b"content-length", str(sum(map(len, parts))).encode()))
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": method, "scheme": "https",
                 "path": parsed.path, "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(),
                 "root_path": "", "headers": headers,
                 "client": ("127.0.0.1", 31337), "server": ("patching.example", 443)}
        events = []
        finished = asyncio.Event()
        reads = 0

        async def receive():
            nonlocal reads
            if disconnect_after is not None and reads == disconnect_after:
                return {"type": "http.disconnect"}
            if parts:
                if barrier and reads == 1:
                    barrier[0].set()
                    await barrier[1].wait()
                reads += 1
                return {"type": "http.request", "body": parts.pop(0), "more_body": bool(parts)}
            await finished.wait()
            return {"type": "http.disconnect"}

        async def send(event):
            events.append(event)
            if event["type"] == "http.response.body" and not event.get("more_body", False):
                finished.set()

        await asyncio.wait_for(self.app(scope, receive, send), timeout=5)
        starts = [event for event in events if event["type"] == "http.response.start"]
        self.assertEqual(len(starts), 1, "Each ASGI request must produce exactly one response")
        payload = b"".join(event.get("body", b"") for event in events if event["type"] == "http.response.body")
        return SimpleNamespace(status=starts[0]["status"], headers=starts[0]["headers"], raw=payload, reads=reads)

    async def wait_run(self, run_id):
        async def wait():
            while True:
                record = pipeline_runner.get_run(run_id)
                if record is not None and record.finished_at is not None:
                    with pipeline_runner._REGISTRY_LOCK:
                        if pipeline_runner._ACTIVE_KEYS.get(record.key) != record.run_id:
                            return record
                await asyncio.sleep(0.005)
        return await asyncio.wait_for(wait(), timeout=5)

    def company_session(self, groups=None):
        fixture = fixtures.LoginTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if groups is not None:
            fixture.claim_changes = {"groups": groups}
        cookie = fixture.finish()
        session = company_auth.authenticate(cookie)
        return fixture, cookie, session

    @staticmethod
    def mutation_headers(cookie, session):
        return {"Cookie": cookie, "X-CSRF-Token": session["csrf_token"], "Origin": "https://patching.example"}

    async def test_health_public_and_all_typed_reads_authenticate(self):
        reply = await self.request("/api/health", actor=None)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["status"], "ok")
        self.assertIsInstance(reply.json()["time"], (float, int))
        for route in ("/api/session", "/api/auth/whoami", "/api/validation", "/api/approvals", "/api/openapi.json"):
            with self.subTest(route=route):
                denied = await self.request(route, actor=None)
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.headers["cache-control"], "no-store")
                self.assertIn("Bearer", denied.headers["www-authenticate"])
        for route in ("/api/session", "/api/auth/whoami"):
            reply = await self.request(route, actor="approver")
            self.assertEqual(reply.status_code, 200)
            self.assertEqual((reply.json()["actor"], reply.json()["roles"]), ("approver", ["approver"]))
            self.assertFalse(reply.json()["permissions"]["live_discovery"])
        self.assertEqual((await self.request("/api/session", headers={"X-OPU-Actor": "admin"})).status_code, 403)
        self.assertEqual((await self.request("/api/session", actor=None, headers={"Authorization": "Bearer shared-lab-token"})).status_code, 401)

    async def test_unknown_routes_and_unsupported_methods_never_dispatch(self):
        with patch.object(pipeline_runner, "start_run") as start:
            self.assertEqual((await self.request("/api/no-such-route", actor=None)).status_code, 401)
            self.assertEqual((await self.request("/api/no-such-route", actor="viewer")).status_code, 404)
            self.assertEqual((await self.request("/api/no-such-route", actor="viewer", method="POST")).status_code, 403)
            self.assertEqual((await self.request("/api/no-such-route", method="POST")).status_code, 404)
            for method in ("PUT", "PATCH", "DELETE", "TRACE"):
                self.assertEqual((await self.request(EXECUTE, method=method)).status_code, 405)
            for route in ("/docs", "/redoc", "/openapi.json"):
                self.assertNotEqual((await self.request(route, actor=None)).status_code, 200)
            start.assert_not_called()

    async def test_principal_configuration_fails_closed(self):
        for content in (b"{}", b"not-json", b"\xff"):
            with self.subTest(content=content):
                self.principals.write_bytes(content)
                reply = await self.request("/api/session")
                self.assertEqual(reply.status_code, 503)

    async def test_roles_and_actor_binding_on_bridged_native_commands(self):
        with patch.object(pipeline_runner, "start_run", return_value=SimpleNamespace(run_id="fixture-run")) as start:
            for actor in ("viewer", "requester", "approver"):
                self.assertEqual((await self.request(EXECUTE, actor=actor, method="POST")).status_code, 403)
            self.assertEqual((await self.request(EXECUTE, method="POST", body={"actor": "admin"})).status_code, 403)
            self.assertEqual((await self.request("/api/plans/p/approve", method="POST", body={"approval_ticket": "CHG-42"})).status_code, 403)
            start.assert_not_called()
            reply = await self.request(EXECUTE, method="POST")
            self.assertEqual((reply.status_code, reply.json()), (202, {"run_id": "fixture-run"}))
            self.assertEqual(start.call_args.args[:2], ("plan", "plan:plan-a:execute"))
            self.assertEqual((await self.request("/api/plans/p/approve", actor="approver", method="POST", body={"approval_ticket": "CHG-42"})).status_code, 202)

    async def test_typed_validation_and_approval_payloads_preserve_domain_fields(self):
        validation = {"fixture_tested": {"status": "passed", "scopes": ["check"], "source_sha256": "a" * 64,
                       "reason": "Fixture only", "completed_at": 1234.5},
                      "live_lab_verified": {"status": "unverified", "reason": "No live evidence"},
                      "production_approved": {"status": "unverified", "reason": "No production approval"}}
        window = {"start": "2030-01-01T10:00:00Z", "end": "2030-01-01T12:00:00Z"}
        plans = [{"plan_id": "p", "state": "awaiting_approval", "requester": "viewer", "host_id": "source",
                  "target": {"database_unique_name": "ORCL"}, "maintenance_window": window},
                 {"plan_id": "done", "state": "succeeded"}]
        with patch.object(server.application_views.release_status, "status", return_value=validation):
            reply = await self.request("/api/validation", actor="viewer")
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json(), validation)
        with patch.object(server.planctl, "list_plans", return_value=plans), \
             patch.object(server.recoveryctl, "list_requests", return_value=[{"request_id": "r", "state": "approved", "requester": "other"}]):
            reply = await self.request("/api/approvals", actor="viewer")
        self.assertEqual(reply.status_code, 200)
        data = reply.json()
        self.assertEqual(data["actor"], "viewer")
        self.assertEqual([(row["kind"], row["id"]) for row in data["items"]], [("plan", "p"), ("recovery", "r")])
        self.assertEqual(data["items"][0]["window"], window)
        self.assertEqual(data["items"][0]["target"], {"database_unique_name": "ORCL"})
        self.assertEqual(data["items"][0]["host_id"], "source")
        self.assertTrue(data["items"][0]["self_requested"])
        self.assertIsNone(data["items"][1]["host_id"])
        self.assertEqual(data["items"][1]["next_action"], "review_authorization")

    async def test_typed_response_corruption_is_not_coerced_or_disclosed(self):
        secret = "sensitive-model-input-must-not-leak"
        valid_session = {"actor": "operator", "roles": ["operator"], "mode": "principal", "rbac_enabled": True,
                         "permissions": {"live_discovery": True}}
        cases = [("health", "/api/health", {"status": "ok", "time": "123.5", "private": secret}),
                 ("health", "/api/health", {"status": "ok", "time": float("inf"), "private": secret}),
                 ("session", "/api/session", {**valid_session, "roles": secret}),
                 ("session", "/api/session", {**valid_session, "rbac_enabled": "true", "private": secret}),
                 ("session", "/api/session", {**valid_session, "permissions": {"live_discovery": 1}, "private": secret}),
                 ("approvals", "/api/approvals", {"items": [{"id": secret}], "actor": "operator"})]
        for service, route, value in cases:
            with self.subTest(service=service, value=value), \
                 patch.object(server.application_views, service, return_value=value), self.assertLogs("opu.api", level="ERROR") as logs:
                reply = await self.request(route)
                self.assertEqual(reply.status_code, 500, reply.text)
                self.assertEqual(reply.json()["error"], "invalid_response")
                self.assertNotIn(secret, reply.text)
                self.assertNotIn(secret, "\n".join(logs.output))

    async def test_schema_is_protected_and_lists_typed_response_contracts(self):
        reply = await self.request("/api/openapi.json", actor="viewer")
        self.assertEqual(reply.status_code, 200)
        document = reply.json()
        self.assertEqual(set(document["paths"]), {"/api/health", "/api/session", "/api/auth/whoami", "/api/validation", "/api/approvals"})
        for route in document["paths"]:
            response = document["paths"][route]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertIn("$ref", response)
            if route != "/api/health":
                self.assertEqual(document["paths"][route]["get"]["security"], [{"PrincipalBearer": []}, {"CompanySession": []}])

    async def test_main_pins_single_local_worker_and_tls_options(self):
        settings = {"WEB_CONCURRENCY": "8", "OPU_WEBAPP_PORT": "9876", "OPU_WEBAPP_TLS_CERT": "",
                    "OPU_WEBAPP_TLS_KEY": "", "OPU_PRODUCTION_CERT_FILE": str(self.root / "no-production-cert"),
                    "OPU_PRODUCTION_REQUIRE_CHECKLIST": "0"}
        with patch.dict(os.environ, settings), patch("uvicorn.run") as run, redirect_stdout(io.StringIO()):
            server.main()
            self.assertEqual(run.call_count, 1)
            self.assertIsInstance(run.call_args.args[0], fastapi.FastAPI)
            opts = run.call_args.kwargs
            self.assertEqual((opts["host"], opts["port"], opts["workers"]), ("127.0.0.1", 9876, 1))
            for name in ("proxy_headers", "access_log", "server_header"):
                self.assertIs(opts[name], False)
            self.assertIsNone(opts["ssl_certfile"])
            self.assertIsNone(opts["ssl_keyfile"])
            run.reset_mock()
            cert, key = str(self.root / "fixture-cert.pem"), str(self.root / "fixture-key.pem")
            os.environ.update(OPU_WEBAPP_TLS_CERT=cert, OPU_WEBAPP_TLS_KEY=key)
            server.main()
            self.assertEqual((run.call_args.kwargs["ssl_certfile"], run.call_args.kwargs["ssl_keyfile"]), (cert, key))
            self.assertEqual(run.call_args.kwargs["workers"], 1)

    async def test_invalid_startup_configuration_never_serves(self):
        baseline = {"OPU_WEBAPP_PORT": "9876", "OPU_WEBAPP_TLS_CERT": "", "OPU_WEBAPP_TLS_KEY": "",
                    "OPU_PRODUCTION_CERT_FILE": str(self.root / "no-production-cert"), "OPU_PRODUCTION_REQUIRE_CHECKLIST": "0"}
        cases = [{"OPU_WEBAPP_PORT": "0"}, {"OPU_WEBAPP_PORT": "65536"}, {"OPU_WEBAPP_PORT": "bad"},
                 {"OPU_WEBAPP_TLS_CERT": "/fixture-cert"}, {"OPU_WEBAPP_TLS_KEY": "/fixture-key"},
                 {"OPU_WEBAPP_RBAC": "typo"}, {"OPU_PRODUCTION_MODE": "typo"},
                 {"OPU_WEBAPP_PRINCIPALS_FILE": str(self.root / "missing-principals")},
                 {"OPU_OIDC_CONFIG": str(self.root / "missing-oidc")}]
        with patch("uvicorn.run") as run, redirect_stdout(io.StringIO()):
            for change in cases:
                with self.subTest(change=change), patch.dict(os.environ, {**baseline, **change}):
                    with self.assertRaises((SystemExit, ValueError, auth.AuthError, server.production.ProductionError)):
                        server.main()
                run.assert_not_called()
        self.principals.write_text("{}")
        with self.assertRaises(auth.AuthError):
            async with self.app.router.lifespan_context(self.app):
                self.fail("Invalid identity configuration must fail even with direct ASGI startup")

    async def test_strict_object_json_and_request_limit_before_submission(self):
        bad = [b"[]", b"null", b"true", b'"text"', b"1", b"{", b"\xff", b'{"x":NaN}',
               b'{"x":Infinity}', b'{"nested":[1e999]}', b'{"nested":{"x":-1e999}}',
               b'{"actor":"operator","actor":"admin"}', b'{"nested":{"x":1,"x":2}}']
        with patch.object(pipeline_runner, "start_run", return_value=SimpleNamespace(run_id="limit-fixture")) as start:
            for content in bad:
                with self.subTest(content=content):
                    result = await self.raw_request(chunks=[content])
                    self.assertEqual(result.status, 400, result.raw)
            start.assert_not_called()
            exact = b'{"padding":"' + b"x" * (LIMIT - len(b'{"padding":""}')) + b'"}'
            self.assertEqual(len(exact), LIMIT)
            accepted = await self.raw_request(chunks=[exact[:100], exact[100:]])
            self.assertEqual(accepted.status, 202, accepted.raw)
            self.assertEqual(start.call_count, 1)
            for chunks, headers in (([exact + b" "], None),
                                    ([exact[:100], exact[100:], b" "], [(b"authorization", b"Bearer operator-token"), (b"content-length", str(LIMIT).encode())]),
                                    ([b"{}"], [(b"authorization", b"Bearer operator-token"), (b"content-length", str(LIMIT + 1).encode())])):
                rejected = await self.raw_request(chunks=chunks, headers=headers)
                self.assertIn(rejected.status, (400, 413), rejected.raw)
            self.assertEqual(start.call_count, 1)

    async def test_duplicate_sensitive_headers_rejected_before_auth_or_submission(self):
        values = {b"authorization": b"Bearer operator-token", b"cookie": b"fixture=value", b"x-csrf-token": b"fixture",
                  b"origin": b"https://patching.example", b"x-opu-actor": b"operator", b"content-length": b"2",
                  b"transfer-encoding": b"chunked"}
        with patch.object(pipeline_runner, "start_run") as start:
            for name, value in values.items():
                for duplicate in (value, b"conflicting-value"):
                    with self.subTest(header=name, conflicting=duplicate != value):
                        headers = [] if name == b"authorization" else [(b"authorization", b"Bearer operator-token")]
                        headers += [(name, value), (name.upper(), duplicate)]
                        result = await self.raw_request(headers=headers)
                        self.assertEqual(result.status, 400, result.raw)
                        self.assertEqual(result.reads, 0, "Reject ambiguous framing/credentials before reading a body")
            start.assert_not_called()

    async def test_missing_length_disallows_body_and_timeout_never_submits(self):
        with patch.object(pipeline_runner, "start_run") as start:
            reply = await self.raw_request(add_length=False)
            self.assertEqual(reply.status, 400)
            entered, release = asyncio.Event(), asyncio.Event()
            with patch.object(api_transport, "BODY_TIMEOUT_SECONDS", 0.05):
                reply = await self.raw_request(chunks=[b"{", b"}"], barrier=(entered, release))
            self.assertTrue(entered.is_set())
            self.assertEqual(reply.status, 408)
            start.assert_not_called()

    async def test_disconnect_mid_body_never_submits(self):
        with patch.object(pipeline_runner, "start_run") as start:
            reply = await self.raw_request(chunks=[b"{", b"}"], disconnect_after=1)
            self.assertEqual(reply.status, 400, reply.raw)
            self.assertEqual(reply.reads, 1)
            start.assert_not_called()

    async def test_controller_timeout_is_not_a_request_upload_timeout(self):
        with patch.object(server.application_views, "approvals", side_effect=TimeoutError("private-native-timeout")), \
             self.assertLogs("opu.api", level="ERROR"):
            reply = await self.request("/api/approvals", actor="viewer")
        self.assertEqual(reply.status_code, 500, reply.text)
        self.assertEqual(reply.json()["error"], "internal_error")
        self.assertNotIn("private-native-timeout", reply.text)

    async def test_invalid_or_oversized_header_envelope_is_rejected_before_body(self):
        base = [(b"authorization", b"Bearer operator-token")]
        cases = [[(b"x-fixture", b"x")] * 101, [(b"x-fixture", b"x" * 32769)],
                 [(b"bad header", b"x")], [(b"\xff", b"x")], [(b"x-fixture", b"x\r\ny")],
                 [(b"x-fixture", b"x\x00y")], [(b"host", b"a"), (b"host", b"b")],
                 [(b"content-type", b"application/json"), (b"content-type", b"text/plain")],
                 [(b"content-encoding", b"gzip")]]
        with patch.object(pipeline_runner, "start_run") as start:
            for extra in cases:
                with self.subTest(header=extra[0][0], value_length=len(extra[0][1]), count=len(extra)):
                    reply = await self.raw_request(headers=base + extra)
                    self.assertEqual(reply.status, 400, reply.raw)
                    self.assertEqual(reply.reads, 0)
            start.assert_not_called()

    async def test_public_read_routes_do_not_exempt_post_authentication(self):
        with patch.object(pipeline_runner, "start_run") as start:
            for route in ("/api/health", "/api/auth/config"):
                for headers, expected in (([], 401), ([(b"authorization", b"Bearer viewer-token")], 403)):
                    with self.subTest(route=route, expected=expected):
                        reply = await self.raw_request(path=route, headers=headers)
                        self.assertEqual(reply.status, expected, reply.raw)
                        self.assertEqual(reply.reads, 0)
            start.assert_not_called()

    async def test_malformed_or_conflicting_framing_and_truncation_are_rejected(self):
        base = [(b"authorization", b"Bearer operator-token")]
        cases = [[(b"content-length", value)] for value in (b"-1", b"bogus", b"2.0", b"2, 2", b"+2", b" 2 ")]
        cases += [[(b"transfer-encoding", b"chunked")], [(b"content-length", b"2"), (b"transfer-encoding", b"chunked")],
                  [(b"content-length", b"1")], [(b"content-length", b"3")]]
        with patch.object(pipeline_runner, "start_run") as start:
            for extra in cases:
                with self.subTest(headers=extra):
                    result = await self.raw_request(headers=base + extra)
                    self.assertEqual(result.status, 400, result.raw)
            start.assert_not_called()

    async def delayed_body(self, mutate, *, headers=None):
        waiting, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.raw_request(headers=headers, chunks=[b"{", b"}"], barrier=(waiting, release)))
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            mutate()
        finally:
            release.set()
        return await task

    async def test_delayed_body_rechecks_credentials_roles_and_identity(self):
        original = json.dumps(self.entries)
        changes = [({"disabled": True}, 401), ({"roles": ["viewer"]}, 403),
                   ({"token_sha256": hashlib.sha256(b"rotated").hexdigest()}, 401), ({"actor": "replacement"}, 403)]
        with patch.object(pipeline_runner, "start_run") as start:
            for change, expected in changes:
                self.entries = json.loads(original)
                self.write_principals()
                with self.subTest(change=change), patch.object(auth, "require_api_auth", wraps=auth.require_api_auth) as authenticate:
                    def mutate():
                        self.assertGreater(authenticate.call_count, 0, "Authenticate before accepting body bytes")
                        next(row for row in self.entries if row["actor"] == "operator").update(change)
                        self.write_principals()
                    result = await self.delayed_body(mutate)
                    self.assertEqual(result.status, expected, result.raw)
            start.assert_not_called()

    async def test_rejected_authentication_does_not_consume_body(self):
        with patch.object(pipeline_runner, "start_run") as start:
            for headers, status in (([], 401), ([(b"authorization", b"Bearer viewer-token")], 403)):
                result = await self.raw_request(headers=headers)
                self.assertEqual(result.status, status)
                self.assertEqual(result.reads, 0)
            start.assert_not_called()

    async def test_delayed_body_cannot_change_principal_to_lab_identity(self):
        with patch.object(pipeline_runner, "start_run") as start, patch.dict(os.environ, {}):
            def change_mode():
                os.environ.update(OPU_WEBAPP_RBAC="0", OPU_WEBAPP_TOKEN="operator-token")
            reply = await self.delayed_body(change_mode)
            self.assertEqual(reply.status, 403, reply.raw)
            start.assert_not_called()

    async def test_company_cookie_precedence_session_fields_and_csrf(self):
        _, cookie, session = self.company_session()
        with patch.object(auth, "require_api_auth", side_effect=AssertionError("Cookie must have priority")):
            reply = await self.request("/api/session", headers={"Cookie": cookie})
        self.assertEqual(reply.status_code, 200)
        data = reply.json()
        for field in ("actor", "roles", "expires_at", "csrf_token"):
            self.assertEqual(data[field], session[field])
        self.assertEqual(data["mode"], "company")
        self.assertTrue(data["rbac_enabled"])
        self.assertTrue(data["permissions"]["live_discovery"])
        self.assertNotIn("groups", data)
        self.assertNotIn("id_token", data)
        self.assertEqual(reply.headers["cache-control"], "no-store")
        headers = self.mutation_headers(cookie, session)
        with patch.object(pipeline_runner, "start_run", return_value=SimpleNamespace(run_id="company-fixture")) as start:
            for invalid in ({"Cookie": cookie}, {**headers, "X-CSRF-Token": "wrong"}, {**headers, "Origin": "https://other.example"}):
                self.assertEqual((await self.request(EXECUTE, actor=None, method="POST", headers=invalid)).status_code, 403)
            start.assert_not_called()
            self.assertEqual((await self.request(EXECUTE, actor=None, method="POST", headers=headers)).status_code, 202)
            start.assert_called_once()

    async def test_company_delayed_body_rechecks_role_and_logout(self):
        fixture, cookie, session = self.company_session()
        headers = [(key.lower().encode(), value.encode()) for key, value in self.mutation_headers(cookie, session).items()]
        with patch.object(pipeline_runner, "start_run") as start:
            def revoke_role():
                fixture.settings["group_roles"] = {"operators": ["viewer"]}
                fixture.save()
            result = await self.delayed_body(revoke_role, headers=headers)
            self.assertEqual(result.status, 403, result.raw)
            fixture.settings["group_roles"] = {"operators": ["operator"]}
            fixture.save()
            result = await self.delayed_body(lambda: company_auth.logout(cookie, csrf=session["csrf_token"], origin="https://patching.example"), headers=headers)
            self.assertEqual(result.status, 401, result.raw)
            start.assert_not_called()

    async def test_delayed_body_cannot_change_company_to_bearer_with_same_actor(self):
        _, cookie, session = self.company_session()
        self.entries.append({"actor": session["actor"], "roles": ["operator"],
                             "token_sha256": hashlib.sha256(b"same-actor-token").hexdigest()})
        self.write_principals()
        fields = {**self.mutation_headers(cookie, session), "Authorization": "Bearer same-actor-token"}
        headers = [(key.lower().encode(), value.encode()) for key, value in fields.items()]
        with patch.object(pipeline_runner, "start_run") as start, patch.dict(os.environ, {}):
            reply = await self.delayed_body(lambda: os.environ.update(OPU_OIDC_CONFIG=""), headers=headers)
            self.assertEqual(reply.status, 403, reply.raw)
            start.assert_not_called()

    async def test_company_live_get_requires_csrf_and_role(self):
        _, cookie, session = self.company_session(["readers"])
        with patch.object(server, "run_discovery") as discovery, patch.object(server, "build_estate", return_value=[]) as estate:
            for route in ("/api/hosts/source/discovery", "/api/estate?live=1", "/api/estate?live=%31"):
                self.assertEqual((await self.request(route, actor=None, headers=self.mutation_headers(cookie, session))).status_code, 403)
            discovery.assert_not_called()
            estate.assert_not_called()
            self.assertEqual((await self.request("/api/estate?live=0", actor=None, headers={"Cookie": cookie})).status_code, 200)
            estate.assert_called_once_with(live=False)

    async def test_company_callback_preserves_both_set_cookie_headers_and_logout(self):
        fixture, _, _ = self.company_session()
        begin = await self.request("/auth/login", actor=None)
        self.assertEqual(begin.status_code, 303)
        query = parse_qs(urlsplit(begin.headers["location"]).query)
        fixture.nonce = query["nonce"][0]
        browser_cookie = begin.headers["set-cookie"].split(";", 1)[0]
        callback = await self.request("/auth/callback?" + urlencode({"state": query["state"][0], "code": "fixture-code"}),
                                      actor=None, headers={"Cookie": browser_cookie})
        self.assertEqual(callback.status_code, 303)
        self.assertEqual(callback.headers["location"], "/")
        cookies = callback.headers.get_list("set-cookie")
        self.assertEqual(len(cookies), 2)
        self.assertTrue(any("Max-Age=0" in cookie for cookie in cookies))
        cookie = next(item.split(";", 1)[0] for item in cookies if item.startswith(company_auth.SESSION_COOKIE + "="))
        session = company_auth.authenticate(cookie)
        self.assertEqual((await self.request("/api/session", actor=None, headers={"Cookie": cookie})).status_code, 200)
        logout = await self.request("/api/auth/logout", actor=None, method="POST", headers=self.mutation_headers(cookie, session))
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(logout.content, b"")
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])
        self.assertEqual((await self.request("/api/session", actor=None, headers={"Cookie": cookie})).status_code, 401)

    async def test_confirmed_assistant_action_keeps_native_control_and_failure(self):
        plan = {"plan_id": "plan-a", "state": "running", "patch": {"patch_id": "39034528"},
                "snapshot_evidence": [{"path": str(self.root / "hosts/source/evidence/snapshot.json")}],
                "target": {"database_unique_name": "ORCL"}}
        with patch.object(server.planctl, "status", return_value=plan), \
             patch.object(server.planctl, "list_tasks", return_value=[]), \
             patch.object(server.planctl, "execute_remaining_tasks", side_effect=server.planctl.PlanError("Native authorization expired")) as execute, \
             patch.object(server.planctl, "approve") as approve, patch.object(server.planctl, "authorize") as authorize:
            reply = await self.request("/api/assistant/conversations", method="POST")
            self.assertEqual(reply.status_code, 201, reply.text)
            cid = reply.json()["conversation"]["id"]
            assistant._proposal("operator", cid, "execute_plan", {"plan_id": "plan-a"}, server.load_hosts())
            action = assistant.get("operator", cid)["actions"][0]
            route = f"/api/assistant/conversations/{cid}/actions/{action['id']}/execute"
            rejected = await self.request(route, method="POST", body={"digest": "incorrect"})
            self.assertEqual(rejected.status_code, 409, rejected.text)
            execute.assert_not_called()
            confirmed = await self.request(route, method="POST", body={"digest": action["digest"]})
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            record = await self.wait_run(confirmed.json()["run_id"])
            self.assertEqual((record.kind, record.key, record.status), ("plan", "plan:plan-a:execute", "failed"))
            execute.assert_called_once_with("plan-a", "operator", max_tasks=200)
            saved = assistant.get("operator", cid)["actions"][0]
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(saved["confirmed_by"], "operator")
            self.assertEqual(saved["run_id"], record.run_id)
            approve.assert_not_called()
            authorize.assert_not_called()
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
