"""Lab pull-agent work queue (filesystem durable store).

Aligns with the architecture doc's pull model without requiring PostgreSQL yet.
Controllers publish sealed plan tasks; agents claim by node, renew leases, and
complete with evidence paths. Live mutation still goes through plan executors.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import re
import subprocess
import json
import os
import stat
import time
from pathlib import Path
import runtime_paths

from durable import file_lock, write_json
from adapters import EXECUTOR_PATHS

QUEUE_DIR_ENV = "OPU_AGENT_QUEUE_DIR"
DEFAULT_QUEUE_DIR = runtime_paths.state_dir() / "agent-queue"
ENROLLMENT_REQUIRED_ENV = "OPU_AGENT_ENROLLMENT_REQUIRED"


class QueueError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.error = "agent_queue_error"
        self.message = message
        self.status = status

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message}


def queue_dir() -> Path:
    override = (os.environ.get(QUEUE_DIR_ENV) or "").strip()
    path = Path(override) if override else DEFAULT_QUEUE_DIR
    if not path.is_absolute() or path.is_symlink() or (path / "jobs").is_symlink():
        raise QueueError("queue directories must be absolute and not symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    (path / "jobs").mkdir(exist_ok=True, mode=0o700)
    return path


def _job_path(job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,249}", job_id) or ".." in job_id:
        raise QueueError(f"invalid job_id: {job_id!r}")
    return queue_dir() / "jobs" / f"{job_id}.json"


def _write_job(path: Path, payload: dict) -> None:
    write_json(path, payload)


def _read_job(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                raise ValueError("unsafe job record")
            raw = source.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("oversized job record")
        job = json.loads(raw)
        if not isinstance(job, dict) or job.get("job_id") != path.stem:
            raise ValueError("invalid job binding")
        if not isinstance(job.get("status"), str) or job["status"] not in {"queued", "claimed", "running", "completed", "reconciliation_required"}:
            raise ValueError("invalid job state")
        for field in ("plan_id", "task_id", "node"):
            if not isinstance(job.get(field), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", job[field]):
                raise ValueError("invalid job identity")
        if job["job_id"] != f"{job['plan_id']}__{job['task_id']}":
            raise ValueError("invalid job scope")
        if not isinstance(job.get("adapter"), str) or job["adapter"] not in EXECUTOR_PATHS:
            raise ValueError("invalid job adapter")
        for field in ("attempt", "claim_generation", "created_at_epoch", "updated_at_epoch"):
            if type(job.get(field)) is not int or job[field] < 0:
                raise ValueError("invalid job generation or time")
        if (job.get("lease_expires_epoch") is not None
                and (type(job["lease_expires_epoch"]) is not int or job["lease_expires_epoch"] < 0)):
            raise ValueError("invalid job lease")
        if not isinstance(job.get("payload"), dict) or not isinstance(job["payload"].get("task", {}), dict):
            raise ValueError("invalid job payload")
        if job.get("result") is not None and not isinstance(job["result"], dict):
            raise ValueError("invalid job result")
        result_status = (job.get("result") or {}).get("status")
        if result_status is not None and not isinstance(result_status, str):
            raise ValueError("invalid job result status")
        if job["status"] in {"claimed", "running"} and (
                not isinstance(job.get("claimed_by"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", job["claimed_by"])
                or type(job.get("lease_expires_epoch")) is not int
                or not isinstance(job.get("claim_token_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", job["claim_token_sha256"])):
            raise ValueError("invalid job ownership")
        return job
    except FileNotFoundError as exc:
        raise QueueError("job is missing", 404) from exc
    except (ValueError, OSError, TypeError, RecursionError) as exc:
        raise QueueError("queue record is unreadable", 409) from exc


def _require_enrollment(agent_id: str, agent_token: str | None, node: str) -> None:
    if (os.environ.get(ENROLLMENT_REQUIRED_ENV) or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import agent_enroll

    if not agent_token or not agent_enroll.verify(agent_id, agent_token, node=node):
        raise QueueError("agent enrollment verification failed", status=403)


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise QueueError(f"invalid {label}")


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "claim_token_sha256"}


def publish_task(*, plan_id: str, task_id: str, node: str, adapter: str, payload: dict | None = None) -> dict:
    for value, label in ((plan_id, "plan_id"), (task_id, "task_id"), (node, "node")):
        _identifier(value, label)
    if not isinstance(adapter, str) or adapter not in EXECUTOR_PATHS:
        raise QueueError(f"unsupported executor adapter: {adapter}")
    payload = {} if payload is None else payload
    if not isinstance(payload, dict) or not isinstance(payload.get("task", {}), dict):
        raise QueueError("payload and payload.task must be objects")
    attempt = (payload.get("task") or {}).get("retry_count", 0)
    if type(attempt) is not int or attempt < 0:
        raise QueueError("invalid task retry generation")
    job_id = f"{plan_id}__{task_id}"
    path = _job_path(job_id)
    with file_lock(queue_dir() / ".queue.lock"):
        old = _read_job(path) if path.exists() else None
        if old:
            if old["node"] != node or old["adapter"] != adapter or old["plan_id"] != plan_id or old["task_id"] != task_id:
                raise QueueError("existing job cannot be rebound", 409)
            if old.get("attempt", 0) == attempt:
                return _public(old)
            if old["status"] != "completed" or attempt != old.get("attempt", 0) + 1:
                raise QueueError("reconcile previous attempt before publishing a retry", 409)
            archive = queue_dir() / "history" / job_id / f"attempt-{old.get('attempt', 0)}.json"
            if archive.exists():
                raise QueueError("queue attempt history already exists", 409)
            _write_job(archive, old)
        now = int(time.time())
        job = {"schema_version": "1.0", "job_id": job_id, "plan_id": plan_id, "task_id": task_id,
               "node": node, "adapter": adapter, "attempt": attempt, "status": "queued",
               "claim_generation": old.get("claim_generation", 0) if old else 0,
               "claimed_by": None, "lease_expires_epoch": None,
               "created_at_epoch": now, "updated_at_epoch": now, "payload": payload}
        _write_job(path, job)
        return _public(job)


def list_jobs(status: str | None = None) -> list[dict]:
    jobs = [_read_job(path) for path in sorted((queue_dir() / "jobs").glob("*.json"))]
    return [_public(job) for job in jobs if not status or job["status"] == status]


def assert_no_unresolved_plan_tasks(plan_id: str) -> None:
    """Fence HTTP state import against published pull-agent work.

    The controller holds its per-plan transport lock around this check and the
    entire HTTP operation, and around queue publication (transport -> queue).
    A queued job is already a reservation: a worker may claim it immediately.
    """
    _identifier(plan_id, "plan_id")
    with file_lock(queue_dir() / ".queue.lock"):
        jobs = [_read_job(path) for path in sorted((queue_dir() / "jobs").glob("*.json"))]
        for job in jobs:
            if job["plan_id"] != plan_id:
                continue
            result = job.get("result") or {}
            task = result.get("task")
            terminal = result.get("status") in ("success", "succeeded", "failed")
            verified = (isinstance(task, dict) and result.get("source") == "sealed_plan_task"
                        and task.get("plan_id") == job["plan_id"] and task.get("task_id") == job["task_id"]
                        and task.get("adapter") == job["adapter"]
                        and type(task.get("retry_count", 0)) is int
                        and task.get("retry_count", 0) == job["attempt"]
                        and task.get("status") in ("succeeded", "failed")
                        and (task["status"] == "failed") == (result.get("status") == "failed"))
            fixture = os.environ.get("OPU_AGENT_TEST_MODE") == "1"
            if job["status"] != "completed" or not terminal or not (verified or fixture):
                raise QueueError("Pull-agent work reserves this plan; reconcile its queue attempt before HTTP execution", 409)


def _expire(job: dict, now: int) -> bool:
    if job["status"] in {"claimed", "running"} and int(job.get("lease_expires_epoch") or 0) <= now:
        job.update(status="reconciliation_required", updated_at_epoch=now,
                   reason="agent lease expired; reconcile the sealed task outcome")
        _write_job(_job_path(job["job_id"]), job)
        return True
    return False


def claim(node: str, agent_id: str, lease_seconds: int = 120, agent_token: str | None = None) -> dict | None:
    _identifier(node, "node"); _identifier(agent_id, "agent_id")
    if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
        raise QueueError("lease_seconds must be between 30 and 3600")
    _require_enrollment(agent_id, agent_token, node)
    with file_lock(queue_dir() / ".queue.lock"):
        now = int(time.time())
        jobs = [_read_job(p) for p in sorted((queue_dir() / "jobs").glob("*.json"))]
        for job in jobs:
            _expire(job, now)
        if any(j["node"] == node and j["status"] in {"claimed", "running", "reconciliation_required"} for j in jobs):
            return None
        for job in jobs:
            if job["node"] != node or job["status"] != "queued":
                continue
            # Native plans execute one ordered task at a time, including across
            # RAC nodes. A refused future task must never consume its attempt.
            peers = [j for j in jobs if j["plan_id"] == job["plan_id"] and j["job_id"] != job["job_id"]]
            if any(j["status"] in {"claimed", "running", "reconciliation_required"} for j in peers):
                continue
            if any(j["task_id"] < job["task_id"] and
                   (j["status"] != "completed" or (j.get("result") or {}).get("status") not in {"success", "succeeded"})
                   for j in peers):
                continue
            token = secrets.token_urlsafe(32)
            job.update(status="claimed", claimed_by=agent_id, lease_expires_epoch=now + lease_seconds,
                       updated_at_epoch=now, claim_generation=job.get("claim_generation", 0) + 1,
                       claim_token_sha256=hashlib.sha256(token.encode()).hexdigest())
            _write_job(_job_path(job["job_id"]), job)
            return {**_public(job), "claim_token": token}
    return None


def _owned(job: dict, agent_id: str, agent_token: str | None, claim_token: str | None) -> None:
    _require_enrollment(agent_id, agent_token, job["node"])
    expected = str(job.get("claim_token_sha256") or "")
    if (job.get("claimed_by") != agent_id or not isinstance(claim_token, str) or not expected
            or not hmac.compare_digest(hashlib.sha256(claim_token.encode()).hexdigest(), expected)):
        raise QueueError("job claim does not belong to this worker generation", 409)
    if _expire(job, int(time.time())) or job["status"] not in {"claimed", "running"}:
        raise QueueError("job lease is not active; reconcile before continuing", 409)


def extend_lease(job_id: str, agent_id: str, seconds: int, agent_token: str | None = None, claim_token: str | None = None) -> dict:
    if type(seconds) is not int or not 30 <= seconds <= 3600:
        raise QueueError("seconds must be between 30 and 3600")
    with file_lock(queue_dir() / ".queue.lock"):
        path = _job_path(job_id); job = _read_job(path)
        _owned(job, agent_id, agent_token, claim_token)
        now = int(time.time())
        job.update(status="running", lease_expires_epoch=now + seconds, updated_at_epoch=now)
        _write_job(path, job)
        return _public(job)


def _native_json(command: str, job: dict) -> dict:
    state = os.environ.get("OPU_PLAN_STATE_DIR", "")
    if not state or not Path(state).is_absolute():
        raise QueueError("an absolute OPU_PLAN_STATE_DIR is required for authoritative task verification", 409)
    tool = Path(__file__).resolve().parent.parent / "bin" / "opu-patch-plan"
    argv = [str(tool), command, "--plan-id", job["plan_id"]]
    if command == "task-status":
        argv += ["--task-id", job["task_id"]]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        task = json.loads(proc.stdout) if proc.returncode == 0 else None
        if not isinstance(task, dict):
            raise ValueError("no verified task")
        return task
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise QueueError("sealed task state is unavailable; reconcile before execution", 409) from exc


def native_task(job: dict) -> dict:
    task = _native_json("task-status", job)
    if (task.get("plan_id") != job["plan_id"] or task.get("task_id") != job["task_id"]
            or task.get("adapter") != job["adapter"] or type(task.get("retry_count", 0)) is not int
            or task.get("retry_count", 0) != job.get("attempt", 0)):
        raise QueueError("sealed task does not match this queue attempt", 409)
    return task


def native_ready(job: dict) -> bool:
    if native_task(job).get("status") != "pending":
        raise QueueError("sealed task is not pending; reconcile its current outcome", 409)
    try:
        next_task = _native_json("next", job)
    except QueueError:
        return False
    return (next_task.get("plan_id") == job["plan_id"] and next_task.get("task_id") == job["task_id"]
            and next_task.get("retry_count", 0) == job.get("attempt", 0))


def defer_pending(job_id: str, agent_id: str, reason: str, agent_token: str | None = None, claim_token: str | None = None) -> dict:
    """Release only a verified, same-generation task that has not started."""
    with file_lock(queue_dir() / ".queue.lock"):
        path = _job_path(job_id); job = _read_job(path)
        _owned(job, agent_id, agent_token, claim_token)
    # The native verifier may wait for a plan lock. Do not block other nodes'
    # queue heartbeats during that wait; recheck the fencing token afterwards.
    if native_task(job).get("status") != "pending":
        raise QueueError("only a verified pending task can be deferred", 409)
    with file_lock(queue_dir() / ".queue.lock"):
        job = _read_job(path)
        _owned(job, agent_id, agent_token, claim_token)
        job.pop("claim_token_sha256", None)
        job.update(status="queued", claimed_by=None, lease_expires_epoch=None,
                   updated_at_epoch=int(time.time()), reason=reason)
        _write_job(path, job)
        return _public(job)


def require_reconciliation(job_id: str, agent_id: str, reason: str, agent_token: str | None = None, claim_token: str | None = None) -> dict:
    with file_lock(queue_dir() / ".queue.lock"):
        path = _job_path(job_id); job = _read_job(path)
        _owned(job, agent_id, agent_token, claim_token)
        job.pop("claim_token_sha256", None)
        job.update(status="reconciliation_required", lease_expires_epoch=None,
                   updated_at_epoch=int(time.time()), reason=reason)
        _write_job(path, job)
        return _public(job)


def complete(job_id: str, agent_id: str, result: dict | None = None, agent_token: str | None = None, claim_token: str | None = None) -> dict:
    if result is not None and not isinstance(result, dict):
        raise QueueError("result must be an object")
    with file_lock(queue_dir() / ".queue.lock"):
        path = _job_path(job_id); job = _read_job(path)
        _owned(job, agent_id, agent_token, claim_token)
    if os.environ.get("OPU_AGENT_TEST_MODE") != "1":
        task = native_task(job)
        if task.get("status") not in {"succeeded", "failed"}:
            raise QueueError("sealed task has no verified terminal outcome", 409)
        result = {**(result or {}), "status": "success" if task["status"] == "succeeded" else "failed",
                  "source": "sealed_plan_task", "task": task}
    with file_lock(queue_dir() / ".queue.lock"):
        job = _read_job(path)
        _owned(job, agent_id, agent_token, claim_token)
        job.update(status="completed", updated_at_epoch=int(time.time()), result=result or {}, lease_expires_epoch=None)
        _write_job(path, job)
        return _public(job)


def reconcile(job_id: str, actor: str) -> dict:
    _identifier(actor, "actor")
    path = _job_path(job_id)
    with file_lock(queue_dir() / ".queue.lock"):
        job = _read_job(path)
        _expire(job, int(time.time()))
        if job["status"] != "reconciliation_required":
            raise QueueError("job does not require reconciliation", 409)
        generation, attempt = job.get("claim_generation", 0), job.get("attempt", 0)
    task = native_task(job)
    if task.get("status") not in {"succeeded", "failed"}:
        raise QueueError("sealed task has no verified terminal outcome for this attempt", 409)
    with file_lock(queue_dir() / ".queue.lock"):
        job = _read_job(path)
        if (job["status"] != "reconciliation_required" or job.get("claim_generation", 0) != generation
                or job.get("attempt", 0) != attempt):
            raise QueueError("queue attempt changed while verifying reconciliation", 409)
        job.update(status="completed", lease_expires_epoch=None, updated_at_epoch=int(time.time()),
                   reconciled_by=actor, result={"status": task["status"], "source": "sealed_plan_task", "task": task})
        _write_job(path, job)
        return _public(job)


def publish_plan_tasks(plan: dict, tasks: list[dict]) -> list[dict]:
    coordinator = (plan.get("target") or {}).get("coordinator_node") or (plan.get("nodes") or ["local"])[0]
    return [publish_task(plan_id=plan.get("plan_id") or "", task_id=str(t.get("task_id") or ""),
                         node=str(coordinator if not t.get("node") or t["node"] == "local" else t["node"]),
                         adapter=str(t.get("adapter") or ""), payload={"stage": t.get("stage"), "task": t})
            for t in tasks if t.get("status") in {None, "pending", "ready", "queued"}]
