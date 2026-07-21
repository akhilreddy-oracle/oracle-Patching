"""Wraps opu-patch-plan: create, approve, authorize, status, dispatch, next.

Runs locally (OPU_PLAN_STATE_DIR points at webapp/var/plans), which is
sufficient while the plan's target is a single reachable host/node. Real
multi-node RAC/Grid dispatch requires this state directory to be reachable
and identical from every node the plan will execute on — see Phase 4's
pre-flight check before enabling dispatch for such a plan.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import evidence

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_TOOL = REPO_ROOT / "bin" / "opu-patch-plan"
PLAN_STATE_DIR = Path(__file__).resolve().parent / "var" / "plans"
DEFAULT_TIMEOUT_SECONDS = 30


class PlanError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.error = "plan_tool_failed"
        self.message = message
        self.stderr = stderr

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message, "stderr": self.stderr}


def _env() -> dict:
    env = os.environ.copy()
    PLAN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    env["OPU_PLAN_STATE_DIR"] = str(PLAN_STATE_DIR)
    return env


def _run(args: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict | None:
    argv = [str(PLAN_TOOL), *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        raise PlanError(f"opu-patch-plan timed out after {timeout}s") from None

    # opu-patch-plan uses nonzero exit for real rejections (self-approval,
    # window closed, stale readiness, etc.) but still emits structured JSON
    # explaining why — that is the useful part, not a crash. Conversely,
    # approve/authorize/dispatch succeed silently (exit 0, no stdout) — only
    # create/create-rollback/status/next print a document.
    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PlanError(f"opu-patch-plan produced unparsable output: {exc}", stderr=result.stdout[-2000:]) from exc

    if result.returncode == 0:
        return None

    raise PlanError(f"opu-patch-plan exited {result.returncode} with no output", stderr=result.stderr.strip())


def create(plan_id: str, requester: str, host_id: str, window_start: str, window_end: str) -> dict:
    args = [
        "create", "--plan-id", plan_id, "--requester", requester,
        "--readiness", str(evidence.evidence_path(host_id, "readiness")),
        "--reconciliation", str(evidence.evidence_path(host_id, "reconciliation")),
        "--artifact-manifest", str(evidence.evidence_path(host_id, "artifact")),
        "--procedure-validation", str(evidence.evidence_path(host_id, "procedure")),
        "--compatibility", str(evidence.evidence_path(host_id, "compatibility_reconciliation")),
        "--policy", str(evidence.evidence_path(host_id, "policy")),
        "--window-start", window_start,
        "--window-end", window_end,
    ]
    recovery = evidence.evidence_path(host_id, "recovery")
    if recovery.is_file():
        args += ["--recovery-evidence", str(recovery)]
    return _run(args)


def create_rollback(plan_id: str, requester: str, source_plan_id: str, window_start: str, window_end: str) -> dict:
    return _run([
        "create-rollback", "--plan-id", plan_id, "--requester", requester,
        "--source-plan-id", source_plan_id, "--window-start", window_start, "--window-end", window_end,
    ])


def approve(plan_id: str, actor: str, approval_ticket: str) -> dict:
    _run(["approve", "--plan-id", plan_id, "--actor", actor, "--approval-ticket", approval_ticket])
    return status(plan_id)


def authorize(plan_id: str, actor: str) -> dict:
    _run(["authorize", "--plan-id", plan_id, "--actor", actor])
    return status(plan_id)


def dispatch(plan_id: str, actor: str) -> dict:
    _run(["dispatch", "--plan-id", plan_id, "--actor", actor])
    return status(plan_id)


def next_task(plan_id: str) -> dict | None:
    """Returns the next pending task descriptor, or None when there isn't one (exit 66)."""
    argv = [str(PLAN_TOOL), "next", "--plan-id", plan_id]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, env=_env())
    except subprocess.TimeoutExpired:
        raise PlanError(f"opu-patch-plan next timed out after {DEFAULT_TIMEOUT_SECONDS}s") from None
    if result.returncode == 66:
        return None
    if not result.stdout.strip():
        raise PlanError(f"opu-patch-plan next exited {result.returncode} with no output", stderr=result.stderr.strip())
    return json.loads(result.stdout)


def status(plan_id: str) -> dict:
    return _run(["status", "--plan-id", plan_id])


def list_tasks(plan_id: str) -> list[dict]:
    """Reads tasks/*.json directly — opu-patch-plan has no list-tasks subcommand."""
    tasks_dir = PLAN_STATE_DIR / "plans" / plan_id / "tasks"
    if not tasks_dir.is_dir():
        return []
    tasks = []
    for entry in sorted(tasks_dir.glob("*.json")):
        try:
            tasks.append(json.loads(entry.read_text()))
        except json.JSONDecodeError:
            tasks.append({"task_id": entry.stem, "status": "unreadable"})
    return tasks


def list_plans() -> list[dict]:
    plans_dir = PLAN_STATE_DIR / "plans"
    if not plans_dir.is_dir():
        return []
    summaries = []
    for entry in sorted(plans_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            summaries.append(status(entry.name))
        except PlanError as exc:
            summaries.append({"plan_id": entry.name, "state": "unreadable", "error": exc.to_json()})
    return summaries
