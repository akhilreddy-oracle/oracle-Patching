#!/usr/bin/env python3
"""Real loopback HTTP login against a TLS-verified local OIDC issuer simulation.

No tenant, deployment credentials, Oracle hosts or external endpoints are used.
The controller's OIDC request, token verification and session code are unchanged.
"""
import base64
from contextlib import redirect_stdout
import datetime as dt
import hashlib
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
try:
    import jwt
except ImportError:
    candidate = ROOT / ".venv/bin/python"
    if candidate.exists() and Path(sys.executable) != candidate:
        os.execv(str(candidate), [str(candidate), "-B", __file__, *sys.argv[1:]])
    raise SystemExit("Install webapp/requirements-sso.txt for company-login validation")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(ROOT / "webapp"))
import company_auth
import evidence
import fleet_metadata
import server


class DoNotFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class CompanyHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid="local-fixture", alg="RS256", use="sig")
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local OIDC test only")])
        now = dt.datetime.now(dt.timezone.utc)
        cls.certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(cls.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(cls.key, hashes.SHA256()))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-company-http-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cert, key = self.root / "local-cert.pem", self.root / "local-key.pem"
        cert.write_bytes(self.certificate.public_bytes(serialization.Encoding.PEM))
        key.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key.chmod(0o600)
        self.trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.trust.load_verify_locations(cafile=str(cert))
        self.assertTrue(self.trust.check_hostname)
        self.assertEqual(self.trust.verify_mode, ssl.CERT_REQUIRED)
        self.codes, self.provider_calls, self.provider_errors = {}, [], []
        self.claim_changes, self.issued_tokens, self.auth_queries = {}, [], []
        self.key_redirect = False
        self.pkce_verified = False
        fixture = self

        class Issuer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, data=None, *, location=None):
                raw = json.dumps(data or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                fixture.provider_calls.append(("GET", path))
                if path == "/.well-known/openid-configuration":
                    self.reply(200, {"issuer": fixture.issuer, "authorization_endpoint": fixture.issuer + "/authorize",
                        "token_endpoint": fixture.issuer + "/token", "jwks_uri": fixture.issuer + "/keys",
                        "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["none"]})
                elif path == "/authorize":
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    fixture.auth_queries.append(query)
                    if (query.get("client_id") != ["local-fixture-client"] or query.get("redirect_uri") != [fixture.origin + "/auth/callback"]
                            or query.get("response_type") != ["code"] or query.get("code_challenge_method") != ["S256"]):
                        fixture.provider_errors.append("Invalid authorization parameters")
                        self.reply(400); return
                    code = secrets.token_urlsafe(32)
                    fixture.codes[code] = query
                    self.reply(303, location=fixture.origin + "/auth/callback?" + urllib.parse.urlencode({
                        "code": code, "state": query["state"][0], "iss": fixture.issuer}))
                elif path == "/keys":
                    if fixture.key_redirect:
                        self.reply(302, location=fixture.issuer + "/unexpected-key-redirect")
                    else:
                        self.reply(200, {"keys": [fixture.jwk]})
                else:
                    fixture.provider_errors.append("Unexpected issuer endpoint: " + path)
                    self.reply(404)

            def do_POST(self):
                fixture.provider_calls.append(("POST", self.path))
                if self.path != "/token":
                    fixture.provider_errors.append("Unexpected token endpoint")
                    self.reply(404); return
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                query = fixture.codes.pop(form.get("code", [None])[0], None)
                verifier = form.get("code_verifier", [""])[0]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if (not query or query["code_challenge"] != [challenge]
                        or form.get("client_id") != ["local-fixture-client"]
                        or form.get("redirect_uri") != [fixture.origin + "/auth/callback"]
                        or form.get("grant_type") != ["authorization_code"]):
                    fixture.provider_errors.append("Code exchange failed PKCE or client binding")
                    self.reply(400, {"error": "invalid_grant"}); return
                fixture.pkce_verified = True
                claims = {"iss": fixture.issuer, "aud": "local-fixture-client", "sub": "employee-fixture",
                    "iat": int(time.time()) - 1, "exp": int(time.time()) + 600, "nonce": query["nonce"][0],
                    "groups": ["fixture-admins"], "name": "Local test user", **fixture.claim_changes}
                token = jwt.encode(claims, fixture.key, algorithm="RS256", headers={"kid": "local-fixture"})
                fixture.issued_tokens.append(token)
                self.reply(200, {"id_token": token, "token_type": "Bearer"})

        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(cert), str(key))
        self.idp = LocalServer(("127.0.0.1", 0), Issuer)
        self.idp.socket = tls.wrap_socket(self.idp.socket, server_side=True)
        self.issuer = "https://127.0.0.1:" + str(self.idp.server_port)
        self.app = LocalServer(("127.0.0.1", 0), server.Handler)
        self.origin = "http://127.0.0.1:" + str(self.app.server_port)
        self.config_path = self.root / "oidc.json"
        self.settings = {"issuer": self.issuer, "client_id": "local-fixture-client",
            "redirect_uri": self.origin + "/auth/callback", "allow_loopback_http": True,
            "group_roles": {"fixture-admins": ["admin"]}, "session_seconds": 300}
        self.save_config()
        hosts = self.root / "hosts.json"
        hosts.write_text('{"hosts":[{"id":"fixture","label":"Fixture"}]}')
        self.enterContext(patch.dict(os.environ, {"OPU_OIDC_CONFIG": str(self.config_path),
            "OPU_WEBAPP_PRINCIPALS_FILE": str(self.root / "no-service-principals.json"), "OPU_PRODUCTION_MODE": "0"}))
        self.enterContext(patch.object(company_auth, "STATE_DIR", self.root / "sessions"))
        self.enterContext(patch.object(server, "HOSTS_FILE", hosts))
        self.enterContext(patch.object(evidence, "VAR_DIR", self.root / "evidence"))
        self.enterContext(patch.object(fleet_metadata, "STORE_FILE", self.root / "fleet.json"))
        self.enterContext(patch.object(server.pipeline_runner, "start_run", side_effect=AssertionError("Login test cannot start Oracle work")))
        # Keep the production request and redirect policy. Add only a scoped
        # trust root and explicit no-proxy transport for the generated issuer.
        build_opener = urllib.request.build_opener
        def local_opener(*handlers):
            return build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.trust), *handlers)
        self.enterContext(patch.object(urllib.request, "build_opener", side_effect=local_opener))
        connect = socket.create_connection
        allowed_ports = {self.idp.server_port, self.app.server_port}
        def local_connect(address, *args, **kwargs):
            if address[0] != "127.0.0.1" or address[1] not in allowed_ports:
                raise AssertionError("OIDC fixture attempted an external connection")
            return connect(address, *args, **kwargs)
        self.enterContext(patch.object(socket, "create_connection", side_effect=local_connect))
        self.cookies = CookieJar()
        self.client = local_opener(urllib.request.HTTPCookieProcessor(self.cookies), DoNotFollow())
        self.logs = io.StringIO()
        self.enterContext(redirect_stdout(self.logs))
        for endpoint in (self.idp, self.app):
            thread = threading.Thread(target=endpoint.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.addCleanup(self.stop_server, endpoint, thread)

    @staticmethod
    def stop_server(endpoint, thread):
        endpoint.shutdown(); endpoint.server_close(); thread.join(timeout=3)

    def save_config(self):
        self.config_path.write_text(json.dumps(self.settings)); self.config_path.chmod(0o600)

    def request(self, path, *, body=None, headers=None, client=None):
        url = path if path.startswith("https://") else self.origin + path
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = (client or self.client).open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return {"status": response.status, "headers": response.headers,
                    "body": json.loads(raw) if raw and response.headers.get_content_type() == "application/json" else raw}

    def begin(self):
        login = self.request("/auth/login")
        self.assertEqual(login["status"], 303)
        authorization = self.request(login["headers"]["Location"])
        self.assertEqual(authorization["status"], 303)
        return urllib.parse.urlsplit(authorization["headers"]["Location"])

    def finish(self):
        callback = self.begin()
        result = self.request(callback.path + "?" + callback.query)
        return result, callback

    def session(self):
        result, callback = self.finish()
        self.assertEqual(result["status"], 303)
        session = self.request("/api/session")
        self.assertEqual(session["status"], 200)
        return session["body"], result, callback

    def test_http_tls_pkce_login_session_csrf_write_and_logout(self):
        self.assertEqual(self.request("/api/session")["status"], 401)
        session, callback_response, callback = self.session()
        self.assertTrue(self.pkce_verified)
        self.assertEqual(session["mode"], "company")
        self.assertEqual(session["roles"], ["admin"])
        self.assertTrue(session["actor"].startswith("sso-"))
        self.assertLessEqual(session["expires_at"], time.time() + 300)
        self.assertNotIn("groups", session)
        self.assertNotIn("id_token", session)
        cookies = callback_response["headers"].get_all("Set-Cookie")
        self.assertTrue(any("HttpOnly" in cookie and "SameSite=Lax" in cookie for cookie in cookies))
        self.assertTrue(any(company_auth.STATE_COOKIE in cookie and "Max-Age=0" in cookie for cookie in cookies))
        fleet = self.request("/api/fleet")["body"]
        self.assertTrue(fleet["can_manage_metadata"])
        body = {"expected_version": fleet["databases"][0]["metadata_version"], "environment": "fixture", "desired_patch_baseline": "39034528"}
        path = "/api/fleet/hosts/fixture/metadata"
        headers = {"Origin": self.origin, "X-CSRF-Token": session["csrf_token"]}
        self.assertEqual(self.request(path, body=body)["status"], 403)
        self.assertEqual(self.request(path, body=body, headers={**headers, "Origin": "https://wrong.example"})["status"], 403)
        self.assertFalse((self.root / "fleet.json").exists())
        self.assertEqual(self.request(path, body=body, headers=headers)["status"], 200)
        saved = json.loads((self.root / "fleet.json").read_text())["hosts"]["fixture"]
        self.assertEqual(saved["updated_by"], session["actor"])
        self.assertEqual(self.request(callback.path + "?" + callback.query)["status"], 401)
        self.assertEqual(self.provider_calls.count(("POST", "/token")), 1)
        self.assertIn(("GET", "/keys"), self.provider_calls)
        self.assertEqual(self.request("/api/auth/logout", body={}, headers=headers)["status"], 204)
        self.assertEqual(self.request("/api/session")["status"], 401)
        self.assertEqual(self.provider_errors, [])
        for value in [*self.issued_tokens, self.auth_queries[0]["state"][0], urllib.parse.parse_qs(callback.query)["code"][0]]:
            self.assertNotIn(value, self.logs.getvalue())

    def test_http_session_role_remapping_revocation_and_expiry(self):
        session, _, _ = self.session()
        self.settings["group_roles"] = {"fixture-admins": ["viewer"]}; self.save_config()
        current = self.request("/api/session")
        self.assertEqual(current["body"]["roles"], ["viewer"])
        self.assertEqual(current["body"]["actor"], session["actor"])
        self.assertFalse(self.request("/api/fleet")["body"]["can_manage_metadata"])
        self.assertEqual(self.request("/api/fleet/hosts/fixture/metadata", body={}, headers={
            "Origin": self.origin, "X-CSRF-Token": session["csrf_token"]})["status"], 403)
        self.settings["group_roles"] = {"another-group": ["viewer"]}; self.save_config()
        self.assertEqual(self.request("/api/session")["status"], 403)
        self.settings["group_roles"] = {"fixture-admins": ["admin"]}; self.save_config()
        later = time.time() + 301
        with patch.object(company_auth, "time", SimpleNamespace(time=lambda: later)):
            self.assertEqual(self.request("/api/session")["status"], 401)
        self.assertFalse((self.root / "fleet.json").exists())

    def test_http_callback_requires_the_original_browser_state_cookie(self):
        callback = self.begin()
        unrelated = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), DoNotFollow())
        result = self.request(callback.path + "?" + callback.query, client=unrelated)
        self.assertEqual(result["status"], 401)
        self.assertEqual(self.request(callback.path + "?" + callback.query)["status"], 401)
        self.assertNotIn(("POST", "/token"), self.provider_calls)
        self.assertEqual(self.request("/api/session")["status"], 401)

    def test_http_signed_wrong_nonce_never_creates_a_session(self):
        self.claim_changes = {"nonce": "different-login"}
        result, _ = self.finish()
        self.assertEqual(result["status"], 401)
        self.assertTrue(self.pkce_verified)
        self.assertEqual(self.request("/api/session")["status"], 401)
        self.assertFalse(any(cookie.name == company_auth.SESSION_COOKIE for cookie in self.cookies))

    def test_http_identity_endpoint_redirect_is_refused(self):
        self.key_redirect = True
        result, _ = self.finish()
        self.assertEqual(result["status"], 503)
        self.assertNotIn(("GET", "/unexpected-key-redirect"), self.provider_calls)
        self.assertEqual(self.request("/api/session")["status"], 401)
        self.assertEqual(self.provider_errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
