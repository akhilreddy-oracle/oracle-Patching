#!/usr/bin/env python3
"""Real signed OIDC token fixtures; network and provider are isolated."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
try:
    import jwt
except ImportError:
    candidate = ROOT / '.venv/bin/python'
    if candidate.exists() and Path(sys.executable) != candidate:
        os.execv(str(candidate), [str(candidate), '-B', __file__, *sys.argv[1:]])
    raise SystemExit('Install webapp/requirements-sso.txt for company-login validation')
from cryptography.hazmat.primitives.asymmetric import rsa
sys.path.insert(0, str(ROOT / 'webapp'))
import company_auth as c
import auth


class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid='fixture', alg='RS256', use='sig')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'oidc.json'
        self.settings = {'issuer': 'https://identity.example/tenant', 'client_id': 'fixture-app',
            'redirect_uri': 'https://patching.example/auth/callback', 'group_roles': {'operators': ['operator'], 'readers': ['viewer']},
            'session_seconds': 300}
        self.save()
        self.enterContext(patch.dict(os.environ, {'OPU_OIDC_CONFIG': str(self.path)}))
        self.enterContext(patch.object(c, 'STATE_DIR', self.root / 'sessions'))
        self.discovery = {'issuer': self.settings['issuer'], 'authorization_endpoint': 'https://identity.example/authorize',
            'token_endpoint': 'https://identity.example/token', 'jwks_uri': 'https://identity.example/keys', 'response_types_supported': ['code']}
        self.enterContext(patch.object(c, '_request', side_effect=self.provider))
        self.claim_changes = {}
        self.request_form = None

    def save(self):
        self.path.write_text(json.dumps(self.settings)); self.path.chmod(0o600)

    def provider(self, url, form=None, *, basic_auth=None):
        if url.endswith('openid-configuration'): return self.discovery
        if url.endswith('/keys'): return {'keys': [self.jwk]}
        self.request_form = form
        self.basic_auth = basic_auth
        claims = {'iss': self.settings['issuer'], 'sub': 'employee-123', 'aud': 'fixture-app',
            'iat': int(time.time())-1, 'exp': int(time.time())+600, 'nonce': self.nonce,
            'name': 'Fixture Employee', 'groups': ['operators'], **self.claim_changes}
        return {'id_token': jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'fixture'})}

    def begin(self):
        url, state_cookie = c.login()
        query = parse_qs(urlsplit(url).query)
        self.nonce = query['nonce'][0]
        return query, state_cookie.split(';')[0]

    def finish(self):
        query, browser_cookie = self.begin()
        cookies = c.callback({'state': query['state'], 'code': ['fixture-code']}, browser_cookie)
        return cookies[0].split(';')[0]

    def test_pkce_signed_login_session_and_logout(self):
        header = self.finish()
        session = c.authenticate(header)
        self.assertEqual(session['roles'], ['operator'])
        self.assertTrue(session['actor'].startswith('sso-'))
        self.assertLessEqual(session['expires_at'], time.time()+300)
        self.assertEqual(self.request_form['code'], 'fixture-code')
        self.assertEqual(len(self.request_form['code_verifier']), 43)
        c.authenticate(header, method='POST', csrf=session['csrf_token'], origin='https://patching.example')
        c.logout(header, csrf=session['csrf_token'], origin='https://patching.example')
        with self.assertRaises(auth.AuthError): c.authenticate(header)

    def test_browser_binding_and_one_time_state(self):
        query, cookie = self.begin()
        payload = {'state': query['state'], 'code': ['code']}
        with self.assertRaises(auth.AuthError): c.callback(payload, '')
        with self.assertRaises(auth.AuthError): c.callback(payload, cookie)

    def test_oidc_nonce_issuer_audience_expiry_and_authorized_party(self):
        for changes in ({'nonce':'wrong'}, {'iss':'https://other.example'}, {'aud':'other'}, {'exp':1},
                        {'aud':['fixture-app','other'],'azp':'other'}, {'groups': []}):
            with self.subTest(changes=changes):
                self.claim_changes = changes
                with self.assertRaises(auth.AuthError): self.finish()

    def test_session_expiry_and_role_revocation(self):
        header = self.finish()
        self.settings['group_roles'] = {'operators': ['viewer']}; self.save()
        self.assertEqual(c.authenticate(header)['roles'], ['viewer'])
        with patch.object(c.time, 'time', return_value=time.time()+400):
            with self.assertRaises(auth.AuthError): c.authenticate(header)

    def test_csrf_and_origin_guard(self):
        header = self.finish(); session = c.authenticate(header)
        for csrf, origin in [(None,None), ('bad','https://patching.example'), (session['csrf_token'],'https://other.example')]:
            with self.subTest(csrf=bool(csrf), origin=origin), self.assertRaises(auth.AuthError):
                c.authenticate(header, method='POST', csrf=csrf, origin=origin)

    def test_configuration_https_roles_and_no_silent_lab_fallback(self):
        self.assertTrue(auth.rbac_enabled())
        for key, value in [('issuer','http://identity.example'), ('redirect_uri','http://patching.example/auth/callback'),
                           ('group_roles',{'group':['superuser']}), ('session_seconds',86400)]:
            original = self.settings[key]
            self.settings[key] = value; self.save()
            with self.assertRaises(auth.AuthError): c.config()
            self.settings[key] = original
        self.path.unlink()
        self.assertTrue(auth.rbac_enabled())
        with self.assertRaises(auth.AuthError): c.config()

    def test_configuration_rejects_malformed_endpoint_and_auth_method_without_uncontrolled_errors(self):
        for key, value in [('issuer', 'https://identity.example:bad'), ('issuer', 'https://identity.example:0'),
                           ('issuer', 'https://[broken'), ('issuer', 'https://identity.example\n/tenant'),
                           ('token_endpoint_auth_method', [])]:
            with self.subTest(key=key, value=value):
                original = dict(self.settings); self.settings[key] = value; self.save()
                with self.assertRaises(auth.AuthError): c.config()
                self.settings = original

    def test_configuration_fifo_swap_during_open_is_nonblocking_and_rejected(self):
        open_file = c.os.open
        def swap(path, flags):
            self.assertTrue(flags & os.O_NONBLOCK, 'admission must not block before fstat can reject a FIFO')
            self.path.unlink(); os.mkfifo(self.path)
            return open_file(path, flags)
        with patch.object(c.os, 'open', side_effect=swap):
            with self.assertRaises(auth.AuthError): c.config()

    def test_browser_origin_matches_equivalent_configured_hostname_and_default_port(self):
        self.settings['redirect_uri'] = 'https://PATCHING.EXAMPLE:443/auth/callback'; self.save()
        header = self.finish(); session = c.authenticate(header)
        c.authenticate(header, method='POST', csrf=session['csrf_token'], origin='https://patching.example')
        with self.assertRaises(auth.AuthError):
            c.authenticate(header, method='POST', csrf=session['csrf_token'], origin='https://patching.example:444')
        self.settings['group_roles'] = {'different': ['viewer']}; self.save()
        self.assertIn('Max-Age=0', c.logout(header, origin='https://patching.example'))

    def test_wrong_signature_and_algorithm_fail(self):
        self.nonce = 'nonce'; settings = c.config()
        claims = {'iss': settings['issuer'], 'sub':'u', 'aud': settings['client_id'], 'iat':int(time.time()),
                  'exp':int(time.time())+60, 'nonce':self.nonce}
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        tokens = [jwt.encode(claims, other, algorithm='RS256', headers={'kid':'fixture'}),
                  jwt.encode(claims, 'fixture-secret-that-is-at-least-32-bytes', algorithm='HS256', headers={'kid':'fixture'})]
        for token in tokens:
            with self.assertRaises(auth.AuthError): c.verify_id_token(token, settings, self.discovery, self.nonce)

    def test_expiring_service_credentials(self):
        principals = self.root / 'principals.json'
        entry = {'actor':'service','roles':['viewer'],'token_sha256':hashlib.sha256(b'fixture-token').hexdigest(),
                 'expires_at':'2000-01-01T00:00:00Z'}
        principals.write_text(json.dumps({'principals':[entry]}))
        with patch.dict(os.environ, {'OPU_WEBAPP_PRINCIPALS_FILE':str(principals)}):
            with self.assertRaises(auth.AuthError): auth.require_api_auth('Bearer fixture-token')
            entry['expires_at'] = '2999-01-01T00:00:00Z'
            principals.write_text(json.dumps({'principals':[entry]}))
            self.assertEqual(auth.require_api_auth('Bearer fixture-token'), 'service')

    def test_malformed_client_authentication_metadata_is_controlled(self):
        for value in [None, "none", [None]]:
            with self.subTest(value=value):
                self.discovery['token_endpoint_auth_methods_supported'] = value
                with self.assertRaises(auth.AuthError): self.finish()

    def test_confidential_client_authentication_is_configurable(self):
        self.settings['client_secret_env'] = 'OPU_TEST_CLIENT_SECRET'
        self.discovery['token_endpoint_auth_methods_supported'] = ['client_secret_basic', 'client_secret_post']
        for method in ['client_secret_basic', 'client_secret_post']:
            with self.subTest(method=method), patch.dict(os.environ, {'OPU_TEST_CLIENT_SECRET':'fixture-client-secret'}):
                self.settings['token_endpoint_auth_method'] = method; self.save(); self.finish()
                if method == 'client_secret_basic':
                    self.assertEqual(self.basic_auth, ('fixture-app','fixture-client-secret'))
                    self.assertNotIn('client_secret', self.request_form)
                else:
                    self.assertIsNone(self.basic_auth)
                    self.assertEqual(self.request_form['client_secret'],'fixture-client-secret')


if __name__ == '__main__': unittest.main()
