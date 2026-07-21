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

VAR_DIR = Path(__file__).resolve().parent / "var" / "hosts"

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


def write_evidence(host_id: str, name: str, payload: dict) -> Path:
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
