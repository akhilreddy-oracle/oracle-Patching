"""Per-host evidence file storage for the readiness pipeline.

Evidence documents (discovery snapshot, reconciliation, artifact manifest,
procedure validation, compatibility, policy, readiness result) are cached on
the backend's local disk per host, exactly as the CLI tools produced them —
never edited or re-derived here.
"""
from __future__ import annotations

import json
from pathlib import Path

VAR_DIR = Path(__file__).resolve().parent / "var" / "hosts"


def evidence_dir(host_id: str) -> Path:
    d = VAR_DIR / host_id / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def evidence_path(host_id: str, name: str) -> Path:
    return evidence_dir(host_id) / f"{name}.json"


def write_evidence(host_id: str, name: str, payload: dict) -> Path:
    path = evidence_path(host_id, name)
    path.write_text(json.dumps(payload, indent=2))
    return path


def read_evidence(host_id: str, name: str) -> dict | None:
    path = evidence_path(host_id, name)
    if not path.is_file():
        return None
    return json.loads(path.read_text())
