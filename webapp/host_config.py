"""Validate inventory identities before any caller chooses an SSH target."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re


class HostConfigError(ValueError):
    pass


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise HostConfigError(f"Invalid configured {label}")
    return value.casefold()


def _alias(value):
    if value is not None and (not isinstance(value, str) or not value or value.startswith("-")
                              or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HostConfigError("Configured SSH alias must be a hostname or alias, not an option")


def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), list):
        raise HostConfigError("Host inventory must contain a hosts array")
    result, identities = {}, set()
    for host in data["hosts"]:
        if not isinstance(host, dict):
            raise HostConfigError("Host entries must be objects")
        identity = _identifier(host.get("id"), "host ID")
        if identity in identities:
            raise HostConfigError("Duplicate host identity in inventory")
        identities.add(identity)
        if "sudo" in host and type(host["sudo"]) is not bool:
            raise HostConfigError("Configured sudo must be a boolean")
        _alias(host.get("ssh_alias"))
        if "remote_root" in host:
            root = host["remote_root"]
            if (not isinstance(root, str) or not root.startswith("/") or root.startswith("//")
                    or root.rstrip("/") in {"", "/"}
                    or any(ord(c) < 32 or ord(c) == 127 for c in root)
                    or root.rstrip("/") != str(PurePosixPath(root)) or ".." in PurePosixPath(root).parts):
                raise HostConfigError("Configured remote_root must be a safe absolute directory")
        nodes = host.get("nodes")
        if nodes is not None:
            if not isinstance(nodes, list):
                raise HostConfigError("Configured nodes must be an array")
            names = set()
            for node in nodes:
                if not isinstance(node, dict):
                    raise HostConfigError("Configured node entries must be objects")
                name = _identifier(node.get("name"), "node name")
                if name in names:
                    raise HostConfigError("Duplicate node identity in host inventory")
                names.add(name)
                _alias(node.get("ssh_alias"))
        result[host["id"]] = host
    return result


def load(path):
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        if isinstance(exc, HostConfigError):
            raise
        raise HostConfigError("Host inventory is unreadable or invalid JSON") from exc
