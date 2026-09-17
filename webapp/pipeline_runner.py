"""Async run engine: tracks long-running backend operations by run_id.

Every pipeline step, plan call, and (in a later phase) task-executor
invocation is dispatched through here rather than blocking an HTTP request —
some of these calls (live opatch prereq checks, RMAN backups, real patch
apply) can run for a long time. The frontend starts a run (202 + run_id) and
polls GET /api/runs/{run_id} for status/log/result.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
import runtime_paths

import notifications
from diagnostics import redact_text, redacted
from durable import file_lock, write_json

RUNS_DIR = runtime_paths.state_dir() / "runs"
RUNS: dict[str, "RunRecord"] = {}
_REGISTRY_LOCK = threading.Lock()
# key → active run_id (so 409 responses can hand the caller the in-flight run)
_ACTIVE_KEYS: dict[str, str] = {}
_RUN_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_PROCESS_ID = uuid.uuid4().hex
_CURRENT = threading.local()
_UNRESOLVED = {"queued", "running", "unknown", "reconciling"}


class RunConflict(Exception):
    """Raised when a run is already active for a given dedupe key."""

    def __init__(self, message: str, run_id: str | None = None):
        super().__init__(message)
        self.run_id = run_id


class RunRecord:
    def __init__(self, run_id: str, kind: str, key: str):
        self.run_id = run_id
        self.kind = kind
        self.key = key
        self.status = "queued"  # queued|running|succeeded|failed
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.log_lines: list[str] = []
        self.result = None
        self.error = None
        self.owner = {"pid": os.getpid(), "instance": _PROCESS_ID}
        self.context: dict = {}
        self.reconciliation: dict | None = None
        self.timeline: list[dict] = [{'sequence': 1, 'at': self.created_at, 'event': 'queued', 'message': 'Operation queued'}]
        self.observation: dict | None = None
        self.controller_poll: dict | None = None
        self._lock = threading.RLock()

    def log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(redact_text(line, 4000))
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        self._persist()

    def event(self, event: str, message: str, **details) -> None:
        with self._lock:
            sequence = max((item.get('sequence', 0) for item in self.timeline if isinstance(item.get('sequence', 0), int)), default=0) + 1
            self.timeline.append({'sequence': sequence, 'at': time.time(), 'event': event,
                                  'message': redact_text(message, 500), **redacted(details)})
            self.timeline = self.timeline[-300:]
        self._persist()

    def to_json(self) -> dict:
        with self._lock:
            started = self.started_at or self.created_at
            finished = self.finished_at or time.time()
            elapsed = max(0, int(finished - started)) if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else None
            return {
                "run_id": self.run_id,
                "kind": self.kind,
                "key": self.key,
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "log_tail": list(self.log_lines[-100:]),
                "result": self.result,
                "error": self.error,
                "owner": self.owner,
                "context": dict(self.context),
                "reconciliation": self.reconciliation,
                "timeline": list(self.timeline),
                "observation": redacted(self.observation),
                "controller_poll": self.controller_poll,
                "elapsed_seconds": elapsed,
            }

    def _persist(self) -> None:
        with self._lock:
            run_dir = RUNS_DIR / self.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json(run_dir / "run.json", json.loads(json.dumps(self.to_json(), default=str)))


def set_execution_context(**values) -> None:
    """Persist detached execution coordinates before crossing the SSH boundary."""
    record = getattr(_CURRENT, "record", None)
    if record is not None:
        with record._lock:
            record.context.update(values)
            record._persist()


def current_run_id() -> str | None:
    """Return the native worker's identity without exposing its mutable record."""
    record = getattr(_CURRENT, "record", None)
    return record.run_id if record is not None else None


def record_event(event: str, message: str, **details) -> None:
    record = getattr(_CURRENT, 'record', None)
    if record is not None:
        record.event(event, message, **details)


def controller_poll(state: str) -> None:
    """A controller SSH observation is not a native worker heartbeat."""
    record = getattr(_CURRENT, 'record', None)
    if record is not None:
        with record._lock:
            record.controller_poll = {'observed_at': time.time(), 'state': state, 'source': 'controller_ssh_poll'}
        record._persist()


def _disk_active(key: str) -> str | None:
    for path in sorted(RUNS_DIR.glob("*/run.json")):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("key"), str):
                raise ValueError("missing run key")
        except (OSError, ValueError) as exc:
            raise RunConflict(f"Persisted run {path.parent.name} is unreadable; repair/reconcile its state before launching", path.parent.name) from exc
        if data["key"] == key and data.get("status") in _UNRESOLVED:
            return path.parent.name
    return None


def active_run_id(key: str) -> str | None:
    """Return the in-flight run_id for key, if any."""
    with _REGISTRY_LOCK:
        return _ACTIVE_KEYS.get(key) or _disk_active(key)


def start_run(kind: str, key: str, fn) -> RunRecord:
    """Run fn(record) in a background thread. fn returns the result dict or raises.

    key dedupes concurrent runs (e.g. "host:oracle-test-rac:pipeline:reconcile")
    so a double-click can't launch two overlapping SSH sessions for the same
    target — raises RunConflict instead (with the active run_id when known).
    """
    run_id = uuid.uuid4().hex[:12]
    record = RunRecord(run_id, kind, key)
    with file_lock(RUNS_DIR / ".registry.lock"), _REGISTRY_LOCK:
        existing = _ACTIVE_KEYS.get(key) or _disk_active(key)
        if existing is not None:
            raise RunConflict(f"A run is already active for {key}", run_id=existing)
        record._persist()  # Durable ownership must precede launching any worker.
        _ACTIVE_KEYS[key] = run_id
        RUNS[run_id] = record

    def worker() -> None:
        _CURRENT.record = record
        try:
            with record._lock:
                record.status = "running"
                record.started_at = time.time()
            record._persist()
            record.event('started', 'Controller started the operation')
            result = fn(record)
            with record._lock:
                record.result = result
                record.status = "succeeded"
            record.event('succeeded', 'Controller operation completed')
        except Exception as exc:  # noqa: BLE001 - surfaced via the run record, never swallowed
            to_json = getattr(exc, "to_json", None)
            error = to_json() if callable(to_json) else {"message": str(exc)}
            with record._lock:
                record.error = error
                record.status = "unknown" if record.context.get("detached_execution") and not record.context.get("detached_terminal") else "failed"
            record.event(record.status, (record.error.get('message') if isinstance(record.error, dict) else '') or 'Operation needs inspection')
        finally:
            with record._lock:
                record.finished_at = time.time()
            try:
                record._persist()
            except OSError:
                # Leave the durable queued/running owner in place on disk failure.
                with record._lock:
                    record.status = "unknown"
            with _REGISTRY_LOCK:
                if record.status not in _UNRESOLVED and _ACTIVE_KEYS.get(key) == run_id:
                    _ACTIVE_KEYS.pop(key, None)
            _CURRENT.record = None
            if record.status == "failed":
                # After _persist: the failed state is durable before anything
                # external hears about it, and emit() never raises.
                notifications.emit("run.failed", {
                    "run_id": run_id,
                    "kind": kind,
                    "key": key,
                    "error": record.error,
                })

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        record.status = "failed"
        record.error = {"message": "Worker thread could not be started"}
        record._persist()
        with _REGISTRY_LOCK:
            _ACTIVE_KEYS.pop(key, None)
        raise
    return record


def _load_persisted(run_id: str) -> RunRecord | None:
    if not _RUN_ID_RE.match(run_id):
        return None
    path = RUNS_DIR / run_id / "run.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    record = RunRecord(run_id, data.get("kind") or "unknown", data.get("key") or "")
    record.status = data.get("status") or "failed"
    record.created_at = data.get("created_at") or time.time()
    record.started_at = data.get("started_at")
    record.finished_at = data.get("finished_at")
    record.log_lines = [redact_text(line, 4000) for line in data.get('log_tail') or []] if isinstance(data.get('log_tail'), list) else []
    record.result = data.get("result")
    record.error = data.get("error")
    record.owner = data.get("owner") if isinstance(data.get('owner'), dict) else {}
    record.context = data.get("context") if isinstance(data.get('context'), dict) else {}
    record.reconciliation = data.get("reconciliation")
    record.timeline = [redacted(event) for event in data.get('timeline') or [] if isinstance(event, dict) and isinstance(event.get('at'), (int, float))] if isinstance(data.get('timeline'), list) else []
    record.observation = data.get('observation') if isinstance(data.get('observation'), dict) else None
    record.controller_poll = data.get('controller_poll') if isinstance(data.get('controller_poll'), dict) else None
    if record.status in _UNRESOLVED and record.owner.get("instance") != _PROCESS_ID:
        record.status = "unknown"
        record.error = {"message": "Controller ownership was lost; reconcile the execution before relaunching"}
    return record


def _owner_alive(owner: dict) -> bool:
    try:
        pid = int(owner.get("pid") or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True


def reconcile_run(run_id: str, *, actor: str | None, inspect, confirm_no_active_execution: bool = False, note: str | None = None) -> dict:
    """Resolve an unknown result by inspecting remote evidence, never rerunning it.

    Non-detached interrupted work requires an explicit operator acknowledgement
    and audit note after its old controller has exited. Detached executions can
    only be released by a verified terminal outcome from the managed host.
    """
    if not actor or not isinstance(actor, str):
        raise ValueError("An authenticated operator actor is required")
    with file_lock(RUNS_DIR / ".registry.lock"):
        record = get_run(run_id)
        if record is None:
            raise ValueError("Unknown run_id")
        if record.status not in {"unknown", "reconciling"}:
            raise RunConflict("Only runs with unknown ownership/outcome can be reconciled", run_id)
        if record.context.get("detached_execution"):
            outcome = inspect({**record.to_json(), "reconciliation_actor": actor})
            if outcome.get("status") not in {"succeeded", "failed", "unknown"}:
                raise ValueError("Invalid reconciliation result")
        else:
            if not confirm_no_active_execution or not isinstance(note, str) or not note.strip() or _owner_alive(record.owner):
                raise RunConflict("Verify no execution remains active after the old controller exits, then provide confirm_no_active_execution and an audit note", run_id)
            outcome = {"status": "failed", "error": {"message": "Interrupted work closed after operator verification", "note": note.strip()}}
        with record._lock:
            record.status = outcome["status"]
            record.result = outcome.get("result")
            record.error = outcome.get("error")
            record.finished_at = time.time() if record.status != "unknown" else None
            record.reconciliation = {"actor": actor, "at": time.time(), "note": note, "status": record.status}
            record._persist()
        record.event('reconciled', 'Operator inspected the existing execution outcome', outcome=record.status, actor=actor)
        if record.status not in _UNRESOLVED:
            with _REGISTRY_LOCK:
                if _ACTIVE_KEYS.get(record.key) == run_id:
                    _ACTIVE_KEYS.pop(record.key, None)
        return record.to_json()


def status_counts() -> dict[str, int]:
    """Run counts by status across in-memory and persisted records."""
    counts: dict[str, int] = {}
    with _REGISTRY_LOCK:
        records = list(RUNS.values())
    seen = set()
    for record in records:
        seen.add(record.run_id)
        counts[record.status] = counts.get(record.status, 0) + 1
    if RUNS_DIR.is_dir():
        for path in RUNS_DIR.glob("*/run.json"):
            run_id = path.parent.name
            if run_id in seen:
                continue
            record = _load_persisted(run_id)
            if record is None:
                counts["unreadable"] = counts.get("unreadable", 0) + 1
                continue
            seen.add(run_id)
            status = record.status
            counts[status] = counts.get(status, 0) + 1
    return counts


def get_run(run_id: str) -> RunRecord | None:
    with _REGISTRY_LOCK:
        record = RUNS.get(run_id)
        if record is not None:
            return record
        loaded = _load_persisted(run_id)
        if loaded is not None:
            RUNS[run_id] = loaded
        return loaded


def list_runs(key_prefix: str | None = None) -> list[dict]:
    """Newest-first durable records, retaining unknown ownership semantics."""
    with _REGISTRY_LOCK:
        ids = set(RUNS)
    ids.update(path.parent.name for path in RUNS_DIR.glob('*/run.json') if _RUN_ID_RE.fullmatch(path.parent.name))
    records = []
    for run_id in ids:
        record = get_run(run_id)
        if record is not None and (key_prefix is None or record.key.startswith(key_prefix)):
            records.append(record.to_json())
    return sorted(records, key=lambda item: item.get('created_at') or 0, reverse=True)
