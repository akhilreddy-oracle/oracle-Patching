"""Local development auth for the lab webapp control plane.

S5-lite: a shared API token gates every /api request. Actor names in JSON
bodies remain the SoD identifiers enforced by opu-patch-plan; the token is
what proves the caller is allowed to hit the API at all.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_ENV = "OPU_WEBAPP_TOKEN"
TOKEN_FILE = Path(__file__).resolve().parent / "var" / "api-token"
_CACHED_TOKEN: str | None = None


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.error = "unauthorized"
        self.message = message
        self.status = status

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message}


def _read_token_file() -> str | None:
    if not TOKEN_FILE.is_file():
        return None
    raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return raw or None


def ensure_token() -> str:
    """Return the active API token, creating var/api-token if needed."""
    global _CACHED_TOKEN
    if _CACHED_TOKEN:
        return _CACHED_TOKEN

    env_token = (os.environ.get(TOKEN_ENV) or "").strip()
    if env_token:
        _CACHED_TOKEN = env_token
        return _CACHED_TOKEN

    file_token = _read_token_file()
    if file_token:
        _CACHED_TOKEN = file_token
        return _CACHED_TOKEN

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(generated + "\n", encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    _CACHED_TOKEN = generated
    return _CACHED_TOKEN


def extract_bearer(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def require_api_auth(authorization_header: str | None) -> None:
    expected = ensure_token()
    provided = extract_bearer(authorization_header)
    if not provided or not secrets.compare_digest(provided, expected):
        raise AuthError("Missing or invalid Authorization: Bearer <token>")
