"""Local development auth for the lab webapp control plane.

S5-lite Bearer token gates every /api request. Optional principals file adds
role-based RBAC (requester/approver/operator/viewer) without replacing
opu-patch-plan SoD: actor strings in JSON remain the SoD identities.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

TOKEN_ENV = "OPU_WEBAPP_TOKEN"
TOKEN_FILE = Path(__file__).resolve().parent / "var" / "api-token"
PRINCIPALS_ENV = "OPU_WEBAPP_PRINCIPALS_FILE"
PRINCIPALS_FILE = Path(__file__).resolve().parent / "var" / "principals.json"
RBAC_ENV = "OPU_WEBAPP_RBAC"
_CACHED_TOKEN: str | None = None

# Action -> required role (any one). When RBAC is off, only the Bearer token applies.
ACTION_ROLES: dict[str, set[str]] = {
    "read": {"viewer", "requester", "approver", "operator", "admin"},
    "create": {"requester", "admin"},
    "approve": {"approver", "admin"},
    "authorize": {"operator", "admin"},
    "dispatch": {"operator", "admin"},
    "execute": {"operator", "admin"},
    "agent": {"operator", "admin"},
}


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


def rbac_enabled() -> bool:
    flag = (os.environ.get(RBAC_ENV) or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    # Auto-enable when a principals file with entries exists.
    return bool(load_principals())


def principals_path() -> Path:
    override = (os.environ.get(PRINCIPALS_ENV) or "").strip()
    return Path(override) if override else PRINCIPALS_FILE


def load_principals() -> dict[str, set[str]]:
    path = principals_path()
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, set[str]] = {}
    for entry in data.get("principals") or []:
        actor = str(entry.get("actor") or "").strip()
        roles = {str(r).strip() for r in (entry.get("roles") or []) if str(r).strip()}
        if actor and roles:
            out[actor] = roles
    return out


def require_role(actor: str | None, action: str) -> None:
    """Enforce optional RBAC for a named actor and logical action."""
    if not rbac_enabled():
        return
    needed = ACTION_ROLES.get(action)
    if not needed:
        raise AuthError(f"unknown RBAC action: {action}", status=500)
    if not actor or not str(actor).strip():
        raise AuthError("actor is required when RBAC is enabled", status=403)
    principals = load_principals()
    roles = principals.get(str(actor).strip())
    if not roles:
        raise AuthError(f"actor {actor!r} is not a configured principal", status=403)
    if roles.isdisjoint(needed):
        raise AuthError(
            f"actor {actor!r} lacks role for action {action} (has {sorted(roles)}, needs one of {sorted(needed)})",
            status=403,
        )


def whoami(actor: str | None = None) -> dict:
    principals = load_principals()
    roles = sorted(principals.get(actor or "", set())) if actor else []
    return {
        "rbac_enabled": rbac_enabled(),
        "actor": actor,
        "roles": roles,
        "principals_file": str(principals_path()),
        "actions": {k: sorted(v) for k, v in ACTION_ROLES.items()},
    }
