"""Lab pull-agent work queue (filesystem durable store).

Aligns with the architecture doc's pull model without requiring PostgreSQL yet.
Controllers publish sealed plan tasks; agents claim by node, renew leases, and
complete with evidence paths. Live mutation still goes through plan executors.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

QUEUE_DIR_ENV = "OPU_AGENT_QUEUE_DIR"
DEFAULT_QUEUE_DIR = Path(__file__).resolve().parent / "var" / "agent-queue"
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
    path.mkdir(parents=True, exist_ok=True)
    (path / "jobs").mkdir(exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise QueueError(f"invalid job_id: {job_id!r}")
    return queue_dir() / "jobs" / f"{job_id}.json"


def _write_job(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_job(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_enrollment(agent_id: str, agent_token: str | None) -> None:
    if (os.environ.get(ENROLLMENT_REQUIRED_ENV) or "").strip() != "1":
        return
    import agent_enroll

    if not agent_token or not agent_enroll.verify(agent_id, agent_token):
        raise QueueError("agent enrollment verification failed", status=403)


def publish_task(
    *,
    plan_id: str,
    task_id: str,
    node: str,
    adapter: str,
    payload: dict | None = None,
) -> dict:
    job_id = f"{plan_id}__{task_id}"
    path = _job_path(job_id)
    if path.is_file():
        existing = _read_job(path)
        if existing.get("status") in {"queued", "claimed", "running"}:
            return existing
    now = int(time.time())
    job = {
        "schema_version": "1.0",
        "job_id": job_id,
        "plan_id": plan_id,
        "task_id": task_id,
        "node": node,
        "adapter": adapter,
        "status": "queued",
        "claimed_by": None,
        "lease_expires_epoch": None,
        "created_at_epoch": now,
        "updated_at_epoch": now,
        "payload": payload or {},
    }
    _write_job(path, job)
    return job


def list_jobs(status: str | None = None) -> list[dict]:
    jobs = []
    for path in sorted((queue_dir() / "jobs").glob("*.json")):
        job = _read_job(path)
        if status and job.get("status") != status:
            continue
        jobs.append(job)
    return jobs


def claim(
    node: str,
    agent_id: str,
    lease_seconds: int = 120,
    agent_token: str | None = None,
) -> dict | None:
    if not node or not agent_id:
        raise QueueError("node and agent_id are required")
    if lease_seconds < 30 or lease_seconds > 3600:
        raise QueueError("lease_seconds must be between 30 and 3600")
    _require_enrollment(agent_id, agent_token)
    now = int(time.time())
    for job in list_jobs():
        if job.get("node") != node:
            continue
        status = job.get("status")
        expiry = int(job.get("lease_expires_epoch") or 0)
        if status == "queued" or (status in {"claimed", "running"} and expiry <= now):
            job["status"] = "claimed"
            job["claimed_by"] = agent_id
            job["lease_expires_epoch"] = now + lease_seconds
            job["updated_at_epoch"] = now
            _write_job(_job_path(job["job_id"]), job)
            return job
    return None


def extend_lease(job_id: str, agent_id: str, seconds: int) -> dict:
    if not agent_id:
        raise QueueError("agent_id is required")
    if seconds < 30 or seconds > 86400:
        raise QueueError("seconds must be between 30 and 86400")
    path = _job_path(job_id)
    if not path.is_file():
        raise QueueError(f"unknown job_id: {job_id}", status=404)
    job = _read_job(path)
    if job.get("claimed_by") != agent_id:
        raise QueueError("job is not claimed by this agent", status=409)
    if job.get("status") not in {"claimed", "running"}:
        raise QueueError(f"job lease is not extendable in status {job.get('status')!r}", status=409)
    now = int(time.time())
    job["lease_expires_epoch"] = now + int(seconds)
    job["updated_at_epoch"] = now
    _write_job(path, job)
    return job


def complete(
    job_id: str,
    agent_id: str,
    result: dict | None = None,
    agent_token: str | None = None,
) -> dict:
    _require_enrollment(agent_id, agent_token)
    path = _job_path(job_id)
    if not path.is_file():
        raise QueueError(f"unknown job_id: {job_id}", status=404)
    job = _read_job(path)
    if job.get("claimed_by") != agent_id:
        raise QueueError("job is not claimed by this agent", status=409)
    now = int(time.time())
    expiry = int(job.get("lease_expires_epoch") or 0)
    if expiry and expiry < now:
        raise QueueError("job lease expired; reclaim before completing", status=409)
    job["status"] = "completed"
    job["updated_at_epoch"] = now
    job["result"] = result or {}
    job["lease_expires_epoch"] = None
    _write_job(path, job)
    return job


def publish_plan_tasks(plan: dict, tasks: list[dict]) -> list[dict]:
    """Publish pending/running-eligible tasks from a sealed plan into the queue."""
    plan_id = plan.get("plan_id") or ""
    published = []
    for task in tasks:
        status = task.get("status")
        if status not in {None, "pending", "ready", "queued"}:
            continue
        published.append(
            publish_task(
                plan_id=plan_id,
                task_id=str(task.get("task_id") or ""),
                node=str(task.get("node") or (plan.get("nodes") or ["local"])[0]),
                adapter=str(task.get("adapter") or ""),
                payload={"stage": task.get("stage"), "task": task},
            )
        )
    return published
