"""Per-host evidence file storage for the readiness pipeline.

Evidence documents (discovery snapshot, reconciliation, artifact manifest,
procedure validation, compatibility, policy, readiness result) are cached on
the backend's local disk per host, exactly as the CLI tools produced them —
never edited or re-derived here.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
import runtime_paths

VAR_DIR = runtime_paths.state_dir() / "hosts"

# Matches lib/opu/common.sh opu_validate_identifier — rejects path separators.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EvidenceError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.error = "invalid_host_id"
        self.message = message

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message}


def validate_host_id(host_id: str) -> str:
    if not host_id or not _ID_RE.match(host_id):
        raise EvidenceError(f"host_id contains unsupported characters: {host_id!r}")
    return host_id


def evidence_dir(host_id: str) -> Path:
    validate_host_id(host_id)
    root = VAR_DIR.resolve()
    d = (VAR_DIR / host_id / "evidence").resolve()
    if root not in d.parents and d != root:
        raise EvidenceError(f"host_id escapes evidence root: {host_id!r}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def evidence_path(host_id: str, name: str) -> Path:
    return evidence_dir(host_id) / f"{name}.json"


def validate_evidence_name(name: str) -> str:
    if not name or not _ID_RE.match(name):
        raise EvidenceError(f"evidence name contains unsupported characters: {name!r}")
    return name


def node_snapshot_evidence_name(node_name: str) -> str:
    """Per-node topology snapshot evidence key (RAC needs one file per active node)."""
    short = (node_name or "").split(".", 1)[0]
    validate_evidence_name(short)
    return f"snapshot_{short}"


def write_evidence(host_id: str, name: str, payload: dict) -> Path:
    validate_evidence_name(name)
    path = evidence_path(host_id, name)
    # Atomic replace so concurrent readers never see a partial JSON document.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def read_evidence(host_id: str, name: str) -> dict | None:
    path = evidence_path(host_id, name)
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def clear_evidence(host_id: str, name: str) -> bool:
    """Remove a cached evidence document so pipeline_state no longer shows it done."""
    validate_evidence_name(name)
    path = evidence_path(host_id, name)
    if not path.is_file() or path.is_symlink():
        return False
    path.unlink()
    return True


def list_snapshot_paths(host_id: str) -> list[Path]:
    """Return topology snapshot paths for reconcile/readiness (one per discovered node).

    Prefers the multi-node index written by discovery. Falls back to the legacy
    single ``snapshot.json`` when no index exists.
    """
    index = read_evidence(host_id, "snapshot_nodes")
    nodes = (index or {}).get("nodes") if isinstance(index, dict) else None
    if isinstance(nodes, list) and nodes:
        paths: list[Path] = []
        for entry in nodes:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("evidence")
            if not name:
                continue
            evidence_name = entry.get("evidence") or node_snapshot_evidence_name(str(name))
            path = evidence_path(host_id, str(evidence_name))
            if path.is_file():
                paths.append(path)
        if paths:
            return paths
    legacy = evidence_path(host_id, "snapshot")
    return [legacy] if legacy.is_file() else []
