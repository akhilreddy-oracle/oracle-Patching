"""Run one fenced queue claim, renewing its lease while the executor works."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import agent_queue
from adapters import EXECUTOR_PATHS
from diagnostics import redact_text


def run_once(node: str, agent: str, lease: int = 120, token: str | None = None) -> tuple[dict, int]:
    test_mode = os.environ.get("OPU_AGENT_TEST_MODE") == "1"
    if not test_mode and not Path(os.environ.get("OPU_PLAN_STATE_DIR", "")).is_absolute():
        raise agent_queue.QueueError("an absolute OPU_PLAN_STATE_DIR is required before claiming real work", 409)
    job = agent_queue.claim(node, agent, lease_seconds=lease, agent_token=token)
    if job is None:
        return {"status": "idle", "message": "no queued work for node"}, 0
    claim_token = job["claim_token"]
    credentials = {"agent_token": token, "claim_token": claim_token}
    if not test_mode:
        try:
            if not agent_queue.native_ready(job):
                return agent_queue.defer_pending(job["job_id"], agent, "waiting for the next sealed task", **credentials), 75
        except agent_queue.QueueError as exc:
            agent_queue.require_reconciliation(job["job_id"], agent, str(exc), **credentials)
            raise
    adapter = job["adapter"]
    executor = str(Path(__file__).resolve().parent.parent / EXECUTOR_PATHS[adapter])
    override_key = "OPU_AGENT_EXECUTOR_" + adapter.replace(".", "_").replace("-", "_").upper()
    override = os.environ.get(override_key)
    if override and os.environ.get("OPU_AGENT_TEST_MODE") == "1":
        executor = override
    result = None
    if not os.access(executor, os.X_OK):
        if not test_mode:
            return agent_queue.defer_pending(job["job_id"], agent, "executor is not executable", **credentials), 69
        result = {"status": "failed", "reason": "executor is not executable", "exit_code": 69}
    if result is None:
        agent_queue.extend_lease(job["job_id"], agent, lease, **credentials)
        argv = [executor, "execute", "--plan-id", job["plan_id"], "--task-id", job["task_id"],
                "--actor", agent, "--lease-seconds", str(lease)]
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError:
            if not test_mode:
                return agent_queue.defer_pending(job["job_id"], agent, "executor could not be started", **credentials), 69
            raise
        heartbeat_error = None
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=max(1, lease / 3))
                break
            except subprocess.TimeoutExpired:
                if heartbeat_error is None:
                    try:
                        agent_queue.extend_lease(job["job_id"], agent, lease, **credentials)
                    except Exception as exc:
                        # Never kill an Oracle mutation blindly or report success after losing ownership.
                        # Expiry blocks all new work on this node until verified reconciliation.
                        heartbeat_error = exc
        if heartbeat_error is not None:
            raise agent_queue.QueueError("agent lease renewal failed; reconcile the sealed task outcome", 409) from heartbeat_error
        result = {"status": "success" if proc.returncode == 0 else "failed", "executor": executor,
                  "exit_code": proc.returncode, "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest()}
        if proc.returncode:
            result["stderr_tail"] = redact_text(stderr, 2000)
    if not test_mode:
        try:
            task = agent_queue.native_task(job)
            if task.get("status") == "pending":
                return agent_queue.defer_pending(job["job_id"], agent, "executor did not claim the sealed task", **credentials), 75
            if task.get("status") not in {"succeeded", "failed"}:
                raise agent_queue.QueueError("executor outcome is not terminal; reconcile the sealed task", 409)
        except agent_queue.QueueError as exc:
            agent_queue.require_reconciliation(job["job_id"], agent, str(exc), **credentials)
            raise
    done = agent_queue.complete(job["job_id"], agent, result=result, **credentials)
    return done, 0 if result.get("exit_code") == 0 and done["result"].get("status") in {"success", "succeeded"} else 1
