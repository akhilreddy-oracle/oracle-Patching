"""Per-host evidence file storage for the readiness pipeline.

Evidence documents (discovery snapshot, reconciliation, artifact manifest,
procedure validation, compatibility, policy, readiness result) are cached on
the backend's local disk per host, exactly as the CLI tools produced them —
never edited or re-derived here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from contextlib import contextmanager
from durable import file_lock
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
    if not isinstance(host_id, str) or not _ID_RE.fullmatch(host_id):
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
    validate_evidence_name(name)
    return evidence_dir(host_id) / f"{name}.json"


def validate_evidence_name(name: str) -> str:
    if not isinstance(name, str) or not _ID_RE.fullmatch(name):
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


def read_regular_bytes(path: Path, maximum: int = 4 * 1024 * 1024) -> bytes:
    """Capture bounded evidence without following links or accepting a moving file."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError("source is not a bounded independent regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(maximum + 1)
        after, named = os.fstat(fd), path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_nlink, item.st_mode)
        if identity(before) != identity(after) or identity(named) != identity(before):
            raise ValueError("source changed while being verified")
        if len(data) > maximum:
            raise ValueError("source exceeds the evidence size limit")
        return data
    finally:
        os.close(fd)


@contextmanager
def host_lock(host_id: str):
    """Exclude managed evidence writers while a plan binds and seals its inputs."""
    validate_host_id(host_id)
    acquired = False
    try:
        with file_lock(VAR_DIR / ".locks" / f"{host_id}.lock", timeout=0):
            acquired = True
            yield
    except TimeoutError:
        if acquired:
            raise
        raise EvidenceError("Host evidence is in use; wait for the current operation and review again") from None


def creation_binding(host_id: str, host: dict, patch_id: str, database: str) -> str:
    """Digest the exact configuration and saved documents shown in a proposal.

    Callers that seal a plan hold host_lock through the native create operation.
    This is shared with the assistant so the two contracts cannot drift.
    """
    state = {"host": host, "evidence": {key: read_evidence(host_id, key) for key in (
        "snapshot", "snapshot_nodes", "artifact", "procedure_input", "procedure", "policy", "readiness", "recovery",
        "recovery_selection", "compatibility_reconciliation", "reconciliation")}}
    procedure = state["evidence"]["procedure_input"]
    target = procedure.get("target") if isinstance(procedure, dict) else None
    if (not isinstance(procedure, dict) or not isinstance(target, dict)
            or procedure.get("patch_id") != patch_id or target.get("database_unique_name") != database):
        raise EvidenceError("Requested patch/database does not match the saved procedure; review requirements again")
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


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

    An index is a completeness claim: every entry must resolve. A damaged or
    incomplete index must never silently become a smaller node set or a legacy
    primary-only snapshot.
    """
    index_path = evidence_path(host_id, "snapshot_nodes")
    if index_path.exists() or index_path.is_symlink():
        if not index_path.is_file() or index_path.is_symlink():
            raise EvidenceError("Discovery node index is unsafe; refresh discovery")
        try:
            index = json.loads(index_path.read_text())
        except (OSError, ValueError):
            raise EvidenceError("Discovery node index is unreadable; refresh discovery") from None
        nodes = index.get("nodes") if isinstance(index, dict) else None
        if not isinstance(nodes, list) or not nodes:
            raise EvidenceError("Discovery node index is invalid; refresh discovery")
        paths: list[Path] = []
        seen_names, seen_files = set(), set()
        for entry in nodes:
            if not isinstance(entry, dict):
                raise EvidenceError("Discovery node index has an invalid entry; refresh discovery")
            name = entry.get("name") or entry.get("evidence")
            if not isinstance(name, str) or not name or name.casefold() in seen_names:
                raise EvidenceError("Discovery node identity is missing or duplicated; refresh discovery")
            evidence_name = entry.get("evidence") or node_snapshot_evidence_name(str(name))
            path = evidence_path(host_id, evidence_name)
            if evidence_name in seen_files or not path.is_file() or path.is_symlink():
                raise EvidenceError(f"Discovery snapshot for {name} is missing, duplicated or unsafe; refresh discovery")
            seen_names.add(name.casefold())
            seen_files.add(evidence_name)
            paths.append(path)
        return paths
    legacy = evidence_path(host_id, "snapshot")
    return [legacy] if legacy.is_file() and not legacy.is_symlink() else []
