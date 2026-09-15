"""Configurable OIDC code/PKCE login with short-lived server-side sessions.

No provider is configured by default. Provider tokens never reach the browser
or logs; approved IdP group IDs map to the existing application action roles.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import runtime_paths
import re
import secrets
import sqlite3
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

import auth

CONFIG_ENV = "OPU_OIDC_CONFIG"
DEFAULT_CONFIG = runtime_paths.state_dir() / "oidc.json"
STATE_DIR = runtime_paths.state_dir() / "company-auth"
SESSION_COOKIE = "opu_company_session"
STATE_COOKIE = "opu_login_state"
MAX_JSON = 1024 * 1024


def configured():
    return bool(os.environ.get(CONFIG_ENV)) or DEFAULT_CONFIG.exists() or DEFAULT_CONFIG.is_symlink()


def require(condition, message, status=503):
    if not condition:
        raise auth.AuthError(message, status=status)


def _url(value, *, loopback=False):
    require(isinstance(value, str), "OIDC URL must be text")
    p = urllib.parse.urlsplit(value)
    require(bool(p.hostname) and not p.username and not p.password and not p.fragment,
            "Unsafe OIDC URL")
    require(p.scheme == "https" or (loopback and p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost", "::1"}),
            "OIDC URLs require HTTPS")
    return p


def config():
    require(configured(), "Company login is not configured")
    path = Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    try:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in {0, os.geteuid()}
                and info.st_mode & 0o022 == 0, "OIDC configuration is not administrator-protected")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            actual = os.fstat(f.fileno())
            require((actual.st_dev, actual.st_ino) == (info.st_dev, info.st_ino), "OIDC configuration changed")
            raw = f.read(65537)
        require(len(raw) <= 65536, "OIDC configuration is too large")
        c = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise auth.AuthError("OIDC configuration is unavailable or invalid", status=503) from exc
    require(isinstance(c, dict), "OIDC configuration must be an object")
    _url(c.get("issuer"))
    require(not urllib.parse.urlsplit(c["issuer"]).query, "Issuer cannot have a query")
    redirect = _url(c.get("redirect_uri"), loopback=c.get("allow_loopback_http") is True)
    require(redirect.path == "/auth/callback" and not redirect.query, "Redirect URI must end in /auth/callback")
    require(isinstance(c.get("client_id"), str) and 0 < len(c["client_id"]) <= 256, "OIDC client_id is required")
    c.setdefault("groups_claim", "groups")
    require(isinstance(c["groups_claim"], str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", c["groups_claim"]), "Invalid OIDC groups claim")
    mappings = c.get("group_roles")
    require(isinstance(mappings, dict) and bool(mappings), "OIDC group-to-role mapping is required")
    allowed = set().union(*auth.ACTION_ROLES.values())
    for group, roles in mappings.items():
        require(isinstance(group, str) and group and isinstance(roles, list) and roles
                and all(isinstance(role, str) and role in allowed for role in roles), "Invalid OIDC group role mapping")
    ttl = c.setdefault("session_seconds", 900)
    require(type(ttl) is int and 60 <= ttl <= 900, "Company sessions must expire within 60-900 seconds")
    secret_env = c.get("client_secret_env")
    require(secret_env is None or (isinstance(secret_env, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", secret_env)),
            "Invalid client secret environment variable")
    method = c.setdefault("token_endpoint_auth_method", "client_secret_basic" if secret_env else "none")
    require(method in {"none", "client_secret_basic", "client_secret_post"}, "Unsupported OIDC client authentication method")
    require((method == "none") == (secret_env is None), "OIDC client authentication and secret configuration differ")
    c["policy_sha256"] = hashlib.sha256(raw).hexdigest()
    return c


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


@contextmanager
def _db():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = STATE_DIR.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "Company session directory is not private")
    path = STATE_DIR / "sessions.sqlite3"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    os.close(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
            and not info.st_mode & 0o077, "Company session store is unsafe")
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, kind TEXT, expires REAL, value TEXT)")
    connection.execute("DELETE FROM records WHERE expires <= ?", (time.time(),))
    connection.commit()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _store(key, kind, expires, value):
    with _db() as db:
        require(db.execute("SELECT COUNT(*) FROM records").fetchone()[0] < 10000, "Company session capacity reached")
        db.execute("INSERT INTO records VALUES (?,?,?,?)", (_hash(key), kind, expires, json.dumps(value)))


def _read(key, kind, *, consume=False):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
        return None
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM records WHERE key=? AND kind=? AND expires>?", (_hash(key), kind, time.time())).fetchone()
        if consume:
            db.execute("DELETE FROM records WHERE key=? AND kind=?", (_hash(key), kind))
        return json.loads(row[0]) if row else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise auth.AuthError("OIDC endpoint redirected unexpectedly", status=503)


def _request(url, form=None, *, basic_auth=None):
    _url(url)
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    if basic_auth:
        pair = ":".join(urllib.parse.quote(value, safe="") for value in basic_auth)
        request.add_header("Authorization", "Basic " + base64.b64encode(pair.encode()).decode())
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(MAX_JSON + 1)
        require(len(raw) <= MAX_JSON, "OIDC response is too large")
        result = json.loads(raw)
        require(isinstance(result, dict), "OIDC response must be an object")
        return result
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Token responses and provider error descriptions can contain secrets.
        raise auth.AuthError("Company identity provider request failed", status=503) from exc


def _discovery(c):
    d = _request(c["issuer"].rstrip("/") + "/.well-known/openid-configuration")
    require(d.get("issuer") == c["issuer"], "OIDC discovery issuer differs")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        _url(d.get(key))
    require("code" in d.get("response_types_supported", []), "OIDC provider must support authorization code")
    return d


def cookie(name, value, c, *, age):
    secure = urllib.parse.urlsplit(c["redirect_uri"]).scheme == "https"
    return f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}" + ("; Secure" if secure else "")


def cookie_value(header, name):
    try:
        parsed = SimpleCookie()
        parsed.load(header or "")
        return parsed[name].value if name in parsed else None
    except Exception:
        return None


def login():
    c = config()
    d = _discovery(c)
    state, verifier, nonce, browser = (secrets.token_urlsafe(32) for _ in range(4))
    _store(state, "login", time.time() + 300, {"verifier": verifier, "nonce": nonce, "browser": _hash(browser),
                                            "policy": c["policy_sha256"]})
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
              "scope": "openid profile", "state": state, "nonce": nonce, "code_challenge": challenge,
              "code_challenge_method": "S256"}
    separator = "&" if urllib.parse.urlsplit(d["authorization_endpoint"]).query else "?"
    return d["authorization_endpoint"] + separator + urllib.parse.urlencode(params), cookie(STATE_COOKIE, browser, c, age=300)


def verify_id_token(token, c, d, nonce):
    try:
        import jwt
        header = jwt.get_unverified_header(token)
        require(header.get("alg") == "RS256" and isinstance(header.get("kid"), str), "Unsupported ID token algorithm", 401)
        keys = _request(d["jwks_uri"]).get("keys", [])
        candidates = [k for k in keys if isinstance(k, dict) and k.get("kid") == header["kid"]
                      and k.get("kty") == "RSA" and k.get("use", "sig") == "sig"
                      and k.get("alg", "RS256") == "RS256" and "verify" in k.get("key_ops", ["verify"])]
        require(len(candidates) == 1, "ID token signing key is unavailable", 401)
        key = jwt.PyJWK.from_dict(candidates[0], algorithm="RS256").key
        require(key.key_size >= 2048, "OIDC signing key is below the minimum size", 401)
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=c["client_id"], issuer=c["issuer"],
                            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]}, leeway=0)
        require(isinstance(claims["sub"], str) and 0 < len(claims["sub"]) <= 1024, "Invalid OIDC subject", 401)
        require(isinstance(claims["nonce"], str) and secrets.compare_digest(claims["nonce"], nonce), "OIDC nonce mismatch", 401)
        audience = claims["aud"]
        if isinstance(audience, list) and len(audience) > 1 or "azp" in claims:
            require(claims.get("azp") == c["client_id"], "ID token authorized party differs", 401)
        return claims
    except ImportError as exc:
        raise auth.AuthError("Install webapp/requirements-sso.txt to enable company login", status=503) from exc
    except auth.AuthError:
        raise
    except Exception as exc:
        raise auth.AuthError("Company ID token verification failed", status=401) from exc


def _roles(c, groups):
    return sorted({role for group in groups for role in c["group_roles"].get(group, [])})


def callback(query, cookie_header):
    c = config()
    require({"state", "code"} <= set(query) <= {"state", "code", "iss", "session_state"}
            and all(len(v) == 1 for v in query.values()), "Invalid login callback", 401)
    require("iss" not in query or query["iss"] == [c["issuer"]], "Authorization response issuer differs", 401)
    state, code = query["state"][0], query["code"][0]
    require(isinstance(code, str) and 0 < len(code) <= 8192, "Invalid authorization code", 401)
    pending = _read(state, "login", consume=True)
    browser = cookie_value(cookie_header, STATE_COOKIE)
    require(pending and browser and secrets.compare_digest(_hash(browser), pending["browser"])
            and pending["policy"] == c["policy_sha256"], "Login expired or browser state changed", 401)
    d = _discovery(c)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": c["redirect_uri"],
            "client_id": c["client_id"], "code_verifier": pending["verifier"]}
    if c.get("client_secret_env"):
        secret = os.environ.get(c["client_secret_env"])
        require(bool(secret), "Configured OIDC client secret is unavailable")
        if c["token_endpoint_auth_method"] == "client_secret_post":
            form["client_secret"] = secret
    methods = d.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
    require(isinstance(methods, list) and all(isinstance(method, str) for method in methods),
            "Provider client authentication metadata is invalid")
    # Some public clients omit the discovery auth-method list; PKCE still binds
    # the code. Explicit conflicting metadata is always rejected.
    require((c["token_endpoint_auth_method"] == "none" and "token_endpoint_auth_methods_supported" not in d)
            or c["token_endpoint_auth_method"] in methods, "Provider does not support configured client authentication")
    if c["token_endpoint_auth_method"] == "client_secret_basic":
        tokens = _request(d["token_endpoint"], form, basic_auth=(c["client_id"], secret))
    else:
        tokens = _request(d["token_endpoint"], form)
    claims = verify_id_token(tokens.get("id_token"), c, d, pending["nonce"])
    groups = claims.get(c["groups_claim"], [])
    require(isinstance(groups, list) and len(groups) <= 1000 and all(isinstance(g, str) for g in groups), "Invalid company group claims", 403)
    require(bool(_roles(c, groups)), "No application role is assigned to your company groups", 403)
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = min(float(claims["exp"]), time.time() + c["session_seconds"])
    principal = {"actor": "sso-" + _hash(c["issuer"] + "\0" + claims["sub"])[:32], "groups": groups,
                 "display_name": str(claims.get("name") or claims["sub"])[:200], "expires_at": expires,
                 "csrf_token": csrf, "issuer": c["issuer"], "client_id": c["client_id"]}
    _store(session, "session", expires, principal)
    return [cookie(SESSION_COOKIE, session, c, age=max(0, int(expires - time.time()))), cookie(STATE_COOKIE, "", c, age=0)]


def authenticate(cookie_header, *, method="GET", csrf=None, origin=None):
    c = config()
    value = cookie_value(cookie_header, SESSION_COOKIE)
    principal = _read(value, "session")
    require(principal and principal["issuer"] == c["issuer"] and principal["client_id"] == c["client_id"],
            "Company session expired; sign in again", 401)
    roles = _roles(c, principal["groups"])
    require(bool(roles), "Company access has been revoked", 403)
    if method not in {"GET", "HEAD"}:
        expected_origin = urllib.parse.urlsplit(c["redirect_uri"])
        require(origin in {None, expected_origin.scheme + "://" + expected_origin.netloc}
                and isinstance(csrf, str) and secrets.compare_digest(csrf, principal["csrf_token"]), "Company session CSRF check failed", 403)
    return {**principal, "roles": roles, "mode": "company", "rbac_enabled": True}


def logout(cookie_header):
    c = config()
    value = cookie_value(cookie_header, SESSION_COOKIE)
    if value:
        _read(value, "session", consume=True)
    return cookie(SESSION_COOKIE, "", c, age=0)


def status():
    if not configured():
        return {"configured": False, "mode": "unconfigured"}
    c = config()
    return {"configured": True, "mode": "oidc", "label": c.get("label", "Company login"),
            "login_url": "/auth/login", "session_seconds": c["session_seconds"]}
