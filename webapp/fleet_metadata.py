"""Local fleet labels and desired baselines; no connection or execution settings."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

STORE_FILE = Path(__file__).resolve().parent / "var" / "fleet-metadata.json"
FIELDS = {"environment", "desired_patch_baseline"}
_ENVIRONMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}\Z")
_BASELINE = re.compile(r"[1-9][0-9]{0,19}\Z")


class MetadataError(ValueError):
    def __init__(self, message, *, status=400):
        super().__init__(message)
        self.status = status
        self.message = message

    def to_json(self):
        return {"error": "fleet_metadata_conflict" if self.status == 409 else "invalid_fleet_metadata", "message": self.message}


def normalize(field, value):
    if value is None or value == "":
        return None
    if field == "desired_patch_baseline" and type(value) is int:
        value = str(value)
    if not isinstance(value, str):
        raise MetadataError(f"{field} must be text or null to clear it")
    value = value.strip()
    if not value:
        return None
    if field == "environment" and _ENVIRONMENT.fullmatch(value):
        return value
    if field == "desired_patch_baseline" and _BASELINE.fullmatch(value):
        return value
    if field == "environment":
        raise MetadataError("Environment must start with a letter or number and use at most 64 letters, numbers, spaces, dots, underscores, slashes or hyphens")
    raise MetadataError("Desired baseline must be a positive patch ID of at most 20 digits, without leading zeros")


def _safe_descriptor(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise MetadataError("Fleet metadata storage is unsafe; administrator review is required", status=503)
    return fd


def _read():
    if STORE_FILE.parent.is_symlink():
        raise MetadataError("Fleet metadata directory is unsafe", status=503)
    try:
        fd = _safe_descriptor(STORE_FILE, os.O_RDONLY)
    except FileNotFoundError:
        return {"schema_version": 1, "hosts": {}}
    except OSError as exc:
        raise MetadataError("Fleet metadata storage cannot be read safely", status=503) from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversized metadata")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"schema_version", "hosts"} or data["schema_version"] != 1 or not isinstance(data["hosts"], dict):
            raise ValueError("invalid metadata document")
        for host_id, record in data["hosts"].items():
            if not host_id or not isinstance(record, dict) or set(record) != FIELDS | {"revision", "updated_at", "updated_by"}:
                raise ValueError("invalid metadata record")
            if type(record["revision"]) is not int or record["revision"] < 1 or not isinstance(record["updated_at"], str) or not isinstance(record["updated_by"], str):
                raise ValueError("invalid metadata revision")
            for field in FIELDS:
                if normalize(field, record[field]) != record[field]:
                    raise ValueError("noncanonical metadata")
        return data
    except (OSError, ValueError, UnicodeError) as exc:
        raise MetadataError("Fleet metadata is unreadable or invalid; administrator review is required", status=503) from exc


def _effective(host, record):
    values = {}
    for field in FIELDS:
        try:
            values[field] = normalize(field, record[field] if record is not None else host.get(field))
        except MetadataError:
            values[field] = None
    # Include the underlying metadata and revision to reject a stale editor,
    # including edits that changed away and back to the same displayed values.
    source = {"host_id": host["id"], "base": {field: host.get(field) for field in FIELDS}, "override": record}
    version = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"host_id": host["id"], **values, "metadata_version": version}


def snapshot(hosts):
    data = _read()
    return {host_id: _effective(host, data["hosts"].get(host_id)) for host_id, host in hosts.items()}


def update(hosts, host_id, body, *, actor):
    """Set both metadata fields with an optimistic per-host revision check.

    The caller must authenticate and require the manage_fleet admin action.
    An empty string or null explicitly clears a field, including a hosts.json
    default. This function never writes hosts.json or touches a managed host.
    """
    if host_id not in hosts:
        raise MetadataError("Unknown configured host", status=404)
    if not isinstance(body, dict) or set(body) != FIELDS | {"expected_version"}:
        raise MetadataError("Supply only environment, desired_patch_baseline and expected_version")
    if not isinstance(body["expected_version"], str) or not re.fullmatch(r"[a-f0-9]{64}", body["expected_version"]):
        raise MetadataError("Reload the fleet before editing its configuration")
    if not isinstance(actor, str) or not actor.strip():
        raise MetadataError("An authenticated actor is required")
    values = {field: normalize(field, body[field]) for field in FIELDS}
    temp_path = None
    try:
        if STORE_FILE.parent.is_symlink():
            raise MetadataError("Fleet metadata directory is unsafe", status=503)
        STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = _safe_descriptor(STORE_FILE.with_suffix(".lock"), os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _read()
            previous = data["hosts"].get(host_id)
            if _effective(hosts[host_id], previous)["metadata_version"] != body["expected_version"]:
                raise MetadataError("Fleet configuration changed since this form opened. Reload the fleet, review the current values and try again.", status=409)
            record = {**values, "revision": (previous or {}).get("revision", 0) + 1,
                      "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "updated_by": actor.strip()}
            data["hosts"][host_id] = record
            fd, temp_path = tempfile.mkstemp(prefix=".fleet-metadata-", dir=STORE_FILE.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, STORE_FILE)
            temp_path = None
            return _effective(hosts[host_id], record)
    except OSError as exc:
        raise MetadataError("Fleet configuration could not be saved safely", status=503) from exc
    finally:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
