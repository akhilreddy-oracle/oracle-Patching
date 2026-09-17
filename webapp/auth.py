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
import stat
from pathlib import Path
import runtime_paths
from durable import file_lock

TOKEN_ENV = "OPU_WEBAPP_TOKEN"
TOKEN_FILE = runtime_paths.state_dir() / "api-token"
PRINCIPALS_ENV = "OPU_WEBAPP_PRINCIPALS_FILE"
PRINCIPALS_FILE = runtime_paths.state_dir() / "principals.json"
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


def _protected_read(path: Path, *, private: bool = False) -> bytes:
    """Read only an owned, non-linked regular credential/configuration file."""
    try:
        info = path.lstat()
        forbidden = 0o077 if private else 0o022
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & forbidden):
            raise AuthError("Credential configuration has unsafe ownership, links or permissions", status=503)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if (actual.st_dev, actual.st_ino, actual.st_mode, actual.st_nlink) != (info.st_dev, info.st_ino, info.st_mode, info.st_nlink):
                raise AuthError("Credential configuration changed while reading", status=503)
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AuthError("Credential configuration is too large", status=503)
        return raw
    except OSError as exc:
        raise AuthError("Credential configuration is unavailable", status=503) from exc


def _read_token_file() -> str | None:
    if not TOKEN_FILE.exists() and not TOKEN_FILE.is_symlink():
        return None
    try:
        raw = _protected_read(TOKEN_FILE, private=True).decode("utf-8").strip()
    except UnicodeError as exc:
        raise AuthError("API token file is invalid", status=503) from exc
    if not raw:
        raise AuthError("API token file is empty", status=503)
    return raw


def ensure_token() -> str:
    """Return the active API token, creating var/api-token if needed."""
    global _CACHED_TOKEN
    env_token = (os.environ.get(TOKEN_ENV) or "").strip()
    if env_token:
        _CACHED_TOKEN = env_token
        return _CACHED_TOKEN

    # Serialize first creation and never follow or truncate an existing path.
    # Re-read on later requests so replacing a lab token revokes the old one.
    try:
        with file_lock(TOKEN_FILE.parent / ".api-token.lock"):
            file_token = _read_token_file()
            if file_token:
                _CACHED_TOKEN = file_token
                return file_token
            generated = secrets.token_urlsafe(32)
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(generated + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _CACHED_TOKEN = generated
    except (OSError, ValueError, TimeoutError) as exc:
        raise AuthError("API token storage is unavailable or unsafe", status=503) from exc
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
        data = json.loads(_protected_read(path))
    except (OSError, ValueError) as exc:
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
