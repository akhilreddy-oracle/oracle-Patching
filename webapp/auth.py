"""Per-principal Bearer authentication and route roles, plus explicit lab mode.

Configured identities use separately minted token digests. HTTP actor fields
are assertions that must match that authenticated identity; the plan CLI still
enforces separation of duties on those actor names.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import datetime as dt
import time
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
    "manage_fleet": {"admin"},
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


def require_api_auth(authorization_header: str | None) -> str | None:
    """Authenticate the credential, returning its principal (None in lab mode).

    In RBAC mode the shared development token is deliberately not accepted.
    Each principal must have a unique SHA-256 digest of a separately minted
    random token in its ``token_sha256`` field.
    """
    provided = extract_bearer(authorization_header)
    if rbac_enabled():
        entries = _load_entries(required=True)
        digest = hashlib.sha256((provided or "").encode()).hexdigest()
        matches = [entry["actor"] for entry in entries if not entry.get("disabled")
                   and (entry.get("expires_epoch") is None or entry["expires_epoch"] > time.time())
                   if entry.get("token_sha256") and secrets.compare_digest(digest, entry["token_sha256"])]
        if provided and len(matches) == 1:
            return matches[0]
        raise AuthError("Missing or invalid principal credential")
    expected = ensure_token()
    if not provided or not secrets.compare_digest(provided, expected):
        raise AuthError("Missing or invalid Authorization: Bearer <token>")
    return None


def rbac_enabled() -> bool:
    if (os.environ.get("OPU_OIDC_CONFIG") or (TOKEN_FILE.parent / "oidc.json").exists()
            or (TOKEN_FILE.parent / "oidc.json").is_symlink()):
        return True
    if (os.environ.get("OPU_PRODUCTION_MODE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    flag = (os.environ.get(RBAC_ENV) or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    # Configuration presence, not successful parsing, selects secure mode.
    # A missing explicit override or corrupt existing registry must fail closed.
    return bool((os.environ.get(PRINCIPALS_ENV) or "").strip()) or principals_path().exists() or principals_path().is_symlink()


def principals_path() -> Path:
    override = (os.environ.get(PRINCIPALS_ENV) or "").strip()
    return Path(override) if override else PRINCIPALS_FILE


def _load_entries(*, required: bool = False) -> list[dict]:
    path = principals_path()
    if not path.is_file() or path.is_symlink():
        if required or rbac_enabled():
            raise AuthError("Configured principal registry is missing or unsafe", status=503)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError("Configured principal registry is unreadable", status=503) from exc
    if not isinstance(data, dict) or not isinstance(data.get("principals"), list) or not data["principals"]:
        raise AuthError("Principal registry must contain a nonempty principals array", status=503)
    entries = []
    actors, digests = set(), set()
    allowed_roles = set().union(*ACTION_ROLES.values())
    for entry in data["principals"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("actor"), str):
            raise AuthError("Malformed principal entry", status=503)
        actor = entry["actor"].strip()
        roles = entry.get("roles")
        digest = entry.get("token_sha256")
        if not actor or actor in actors or not isinstance(roles, list) or not roles or any(not isinstance(r, str) or r not in allowed_roles for r in roles):
            raise AuthError("Invalid or duplicate principal identity/roles", status=503)
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or digest in digests):
            raise AuthError("Principal token digests must be unique SHA-256 values", status=503)
        if required and not digest:
            raise AuthError("Every authenticated principal requires token_sha256", status=503)
        actors.add(actor)
        if digest:
            digests.add(digest)
        expires = entry.get("expires_at")
        expires_epoch = None
        if expires is not None:
            try:
                parsed = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                expires_epoch = parsed.timestamp()
            except (ValueError, TypeError, AttributeError) as exc:
                raise AuthError("Principal expiry must be a timestamp with timezone", status=503) from exc
        disabled = entry.get("disabled", False)
        if type(disabled) is not bool:
            raise AuthError("Principal disabled must be boolean", status=503)
        entries.append({"actor": actor, "roles": roles, "token_sha256": digest,
                        "expires_epoch": expires_epoch, "disabled": disabled})
    return entries


def load_principals() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for entry in _load_entries():
        if entry.get("disabled") or (entry.get("expires_epoch") is not None and entry["expires_epoch"] <= time.time()):
            continue
        actor = str(entry.get("actor") or "").strip()
        roles = {str(r).strip() for r in (entry.get("roles") or []) if str(r).strip()}
        if actor and roles:
            out[actor] = roles
    return out


def validate_configuration() -> None:
    if rbac_enabled():
        _load_entries(required=True)


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
        "mode": "principal" if rbac_enabled() else "lab",
        "roles": roles,
        "principals_file": str(principals_path()),
        "actions": {k: sorted(v) for k, v in ACTION_ROLES.items()},
    }
