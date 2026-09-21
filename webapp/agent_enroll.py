"""Lab agent enrollment registry (filesystem, hashed tokens).

Gives pull agents a verifiable identity before they may claim or complete
queue jobs. Tokens are minted once, returned in plaintext exactly once, and
stored only as SHA-256 digests. This is a lab-level identity layer, not mTLS.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import stat
from pathlib import Path
import runtime_paths
from functools import wraps
from durable import file_lock, write_json

REGISTRY_FILE_ENV = "OPU_AGENT_REGISTRY_FILE"
DEFAULT_REGISTRY_FILE = runtime_paths.state_dir() / "agent-registry" / "agents.json"

# Matches lib/opu/common.sh opu_validate_identifier.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EnrollError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.error = "agent_enroll_error"
        self.message = message
        self.status = status

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message}


def registry_path() -> Path:
    override = (os.environ.get(REGISTRY_FILE_ENV) or "").strip()
    return Path(override) if override else DEFAULT_REGISTRY_FILE


def _validate_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not _ID_RE.match(value):
        raise EnrollError(f"invalid {label}: {value!r}")
    return value


def _load() -> dict:
    path = registry_path()
    if path.is_symlink():
        raise EnrollError(f"registry file must not be a symlink: {path}", status=500)
    if not path.exists():
        return {"schema_version": "1.0", "agents": {}}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                raise ValueError("unsafe registry file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversized registry")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise EnrollError(f"registry file is unreadable: {path}: {exc}", status=500)
    if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
        raise EnrollError(f"registry file is malformed: {path}", status=500)
    for agent_id, entry in data["agents"].items():
        if (not isinstance(entry, dict) or not _ID_RE.fullmatch(agent_id)
                or entry.get("agent_id") != agent_id or not isinstance(entry.get("node"), str)
                or not _ID_RE.fullmatch(entry["node"])
                or not isinstance(entry.get("token_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", entry["token_sha256"])
                or type(entry.get("revoked")) is not bool):
            raise EnrollError("agent registry contains a malformed identity", status=500)
    return data


def _save(data: dict) -> None:
    path = registry_path()
    if path.is_symlink():
        raise EnrollError(f"registry file must not be a symlink: {path}", status=500)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def _public_view(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "token_sha256"}


def _locked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with file_lock(registry_path().with_suffix(".lock")):
            return fn(*args, **kwargs)
    return wrapped


@_locked
def enroll(node: str, agent_id: str) -> dict:
    node = _validate_id(node, "node")
    agent_id = _validate_id(agent_id, "agent_id")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    data = _load()
    data["agents"][agent_id] = {
        "agent_id": agent_id,
        "node": node,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "enrolled_at": now,
        "revoked": False,
    }
    _save(data)
    return {"agent_id": agent_id, "node": node, "agent_token": token, "enrolled_at": now}


def verify(agent_id: str, agent_token: str, node: str | None = None) -> bool:
    if not agent_id or not agent_token:
        return False
    try:
        data = _load()
    except EnrollError:
        return False
    entry = data["agents"].get(str(agent_id).strip())
    if not entry or entry.get("revoked"):
        return False
    if node is not None and entry.get("node") != node:
        return False
    expected = str(entry.get("token_sha256") or "")
    provided = hashlib.sha256(str(agent_token).encode("utf-8")).hexdigest()
    return bool(expected) and hmac.compare_digest(provided, expected)


@_locked
def revoke(agent_id: str) -> dict:
    agent_id = _validate_id(agent_id, "agent_id")
    data = _load()
    entry = data["agents"].get(agent_id)
    if not entry:
        raise EnrollError(f"unknown agent_id: {agent_id}", status=404)
    entry["revoked"] = True
    entry["revoked_at"] = int(time.time())
    _save(data)
    return _public_view(entry)


def list_agents() -> list[dict]:
    data = _load()
    return [_public_view(entry) for _, entry in sorted(data["agents"].items())]
