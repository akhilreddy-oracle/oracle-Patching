"""Async run engine: tracks long-running backend operations by run_id.

Every pipeline step, plan call, and (in a later phase) task-executor
invocation is dispatched through here rather than blocking an HTTP request —
some of these calls (live opatch prereq checks, RMAN backups, real patch
apply) can run for a long time. The frontend starts a run (202 + run_id) and
polls GET /api/runs/{run_id} for status/log/result.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent / "var" / "runs"
RUNS: dict[str, "RunRecord"] = {}
_REGISTRY_LOCK = threading.Lock()
_ACTIVE_KEYS: set[str] = set()
_RUN_ID_RE = re.compile(r"^[a-f0-9]{12}$")


class RunConflict(Exception):
    """Raised when a run is already active for a given dedupe key."""


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
        self._lock = threading.Lock()

    def log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        self._persist()

    def to_json(self) -> dict:
        with self._lock:
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
            }

    def _persist(self) -> None:
        try:
            run_dir = RUNS_DIR / self.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(json.dumps(self.to_json(), indent=2, default=str))
        except OSError:
            pass  # best-effort durability; in-memory RUNS stays authoritative


def start_run(kind: str, key: str, fn) -> RunRecord:
    """Run fn(record) in a background thread. fn returns the result dict or raises.

    key dedupes concurrent runs (e.g. "host:oracle-test-rac:pipeline:reconcile")
    so a double-click can't launch two overlapping SSH sessions for the same
    target — raises RunConflict instead.
    """
    with _REGISTRY_LOCK:
        if key in _ACTIVE_KEYS:
            raise RunConflict(f"A run is already active for {key}")
        _ACTIVE_KEYS.add(key)

    run_id = uuid.uuid4().hex[:12]
    record = RunRecord(run_id, kind, key)
    RUNS[run_id] = record

    def worker() -> None:
        with record._lock:
            record.status = "running"
            record.started_at = time.time()
        record._persist()
        try:
            result = fn(record)
            with record._lock:
                record.result = result
                record.status = "succeeded"
        except Exception as exc:  # noqa: BLE001 - surfaced via the run record, never swallowed
            to_json = getattr(exc, "to_json", None)
            error = to_json() if callable(to_json) else {"message": str(exc)}
            with record._lock:
                record.error = error
                record.status = "failed"
        finally:
            with record._lock:
                record.finished_at = time.time()
            record._persist()
            with _REGISTRY_LOCK:
                _ACTIVE_KEYS.discard(key)

    threading.Thread(target=worker, daemon=True).start()
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
    record = RunRecord(run_id, data.get("kind") or "unknown", data.get("key") or "")
    record.status = data.get("status") or "failed"
    record.created_at = data.get("created_at") or time.time()
    record.started_at = data.get("started_at")
    record.finished_at = data.get("finished_at")
    record.log_lines = list(data.get("log_tail") or [])
    record.result = data.get("result")
    record.error = data.get("error")
    return record


def get_run(run_id: str) -> RunRecord | None:
    with _REGISTRY_LOCK:
        record = RUNS.get(run_id)
        if record is not None:
            return record
        loaded = _load_persisted(run_id)
        if loaded is not None:
            RUNS[run_id] = loaded
        return loaded
