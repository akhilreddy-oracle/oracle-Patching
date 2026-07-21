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
import re
import subprocess
from pathlib import Path

import evidence
import testmode_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_TOOL = REPO_ROOT / "bin" / "opu-patch-plan"
PLAN_STATE_DIR = Path(__file__).resolve().parent / "var" / "plans"
TESTMODE_DIR = Path(__file__).resolve().parent / "var" / "testmode"
DEFAULT_TIMEOUT_SECONDS = 30

# Matches lib/opu/common.sh opu_validate_identifier.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Only adapters with a verified TEST_MODE fixture are wired up. Real
# (non-fixture) execution against a live host is not implemented — every
# plan runnable through execute_next_task() is a synthetic demo plan.
EXECUTOR_BY_ADAPTER = {
    "database_single_instance_opatch": REPO_ROOT / "bin" / "opu-database-single-instance-patch",
    "database_single_instance_opatch_rollback": REPO_ROOT / "bin" / "opu-database-single-instance-rollback",
}


class PlanError(Exception):
    def __init__(self, message: str, stderr: str = "", result=None):
        super().__init__(message)
        self.error = "plan_tool_failed"
        self.message = message
        self.stderr = stderr
        self.result = result

    def to_json(self) -> dict:
        payload = {"error": self.error, "message": self.message, "stderr": self.stderr}
        if self.result is not None:
            payload["result"] = self.result
        return payload


def validate_plan_id(plan_id: str) -> str:
    if not plan_id or not _ID_RE.match(plan_id):
        raise PlanError(f"plan_id contains unsupported characters: {plan_id!r}")
    return plan_id


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

    # Failures go to stderr via opu_error; never treat nonzero exit as success
    # even if stdout happens to contain JSON.
    if result.returncode != 0:
        raise PlanError(
            f"opu-patch-plan exited {result.returncode}",
            stderr=result.stderr.strip() or result.stdout.strip()[-2000:],
        )

    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PlanError(
                f"opu-patch-plan produced unparsable output: {exc}",
                stderr=result.stdout[-2000:],
            ) from exc
    return None


def create(plan_id: str, requester: str, host_id: str, window_start: str, window_end: str) -> dict:
    validate_plan_id(plan_id)
    evidence.validate_host_id(host_id)
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
    validate_plan_id(plan_id)
    validate_plan_id(source_plan_id)
    return _run([
        "create-rollback", "--plan-id", plan_id, "--requester", requester,
        "--source-plan-id", source_plan_id, "--window-start", window_start, "--window-end", window_end,
    ])


def approve(plan_id: str, actor: str, approval_ticket: str) -> dict:
    validate_plan_id(plan_id)
    _run(["approve", "--plan-id", plan_id, "--actor", actor, "--approval-ticket", approval_ticket])
    return status(plan_id)


def authorize(plan_id: str, actor: str) -> dict:
    validate_plan_id(plan_id)
    _run(["authorize", "--plan-id", plan_id, "--actor", actor])
    return status(plan_id)


def dispatch(plan_id: str, actor: str) -> dict:
    validate_plan_id(plan_id)
    _run(["dispatch", "--plan-id", plan_id, "--actor", actor])
    return status(plan_id)


def next_task(plan_id: str) -> dict | None:
    """Returns the next pending task descriptor, or None when there isn't one (exit 66)."""
    validate_plan_id(plan_id)
    argv = [str(PLAN_TOOL), "next", "--plan-id", plan_id]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, env=_env())
    except subprocess.TimeoutExpired:
        raise PlanError(f"opu-patch-plan next timed out after {DEFAULT_TIMEOUT_SECONDS}s") from None
    if result.returncode == 66:
        return None
    if result.returncode != 0:
        raise PlanError(
            f"opu-patch-plan next exited {result.returncode}",
            stderr=result.stderr.strip(),
        )
    if not result.stdout.strip():
        raise PlanError(f"opu-patch-plan next exited {result.returncode} with no output", stderr=result.stderr.strip())
    return json.loads(result.stdout)


def status(plan_id: str) -> dict:
    validate_plan_id(plan_id)
    return _run(["status", "--plan-id", plan_id])


def list_tasks(plan_id: str) -> list[dict]:
    """Reads tasks/*.json directly — opu-patch-plan has no list-tasks subcommand."""
    validate_plan_id(plan_id)
    root = (PLAN_STATE_DIR / "plans").resolve()
    tasks_dir = (PLAN_STATE_DIR / "plans" / plan_id / "tasks").resolve()
    if root not in tasks_dir.parents and tasks_dir != root:
        raise PlanError(f"plan_id escapes plan state root: {plan_id!r}")
    if not tasks_dir.is_dir():
        return []
    tasks = []
    for entry in sorted(tasks_dir.glob("*.json")):
        try:
            tasks.append(json.loads(entry.read_text()))
        except json.JSONDecodeError:
            tasks.append({"task_id": entry.stem, "status": "unreadable"})
    return tasks


def create_testmode_demo(plan_id: str, requester: str, window_start: str, window_end: str) -> dict:
    """Builds a fresh single-instance TEST_MODE fixture and creates a plan
    directly from its evidence. This is a self-contained demo path — the
    plan targets the fixture's fake Oracle home, not any real host in
    hosts.json, so it can only ever run against the fixture.
    """
    validate_plan_id(plan_id)
    TESTMODE_DIR.mkdir(parents=True, exist_ok=True)
    fixture_dir = (TESTMODE_DIR / plan_id).resolve()
    if TESTMODE_DIR.resolve() not in fixture_dir.parents and fixture_dir != TESTMODE_DIR.resolve():
        raise PlanError(f"plan_id escapes testmode root: {plan_id!r}")
    fx = testmode_fixtures.build(fixture_dir)
    ev = fx["evidence"]
    args = [
        "create", "--plan-id", plan_id, "--requester", requester,
        "--readiness", str(ev["readiness"]), "--reconciliation", str(ev["reconciliation"]),
        "--artifact-manifest", str(ev["artifact"]), "--procedure-validation", str(ev["procedure"]),
        "--compatibility", str(ev["compatibility"]), "--policy", str(ev["policy"]),
        "--recovery-evidence", str(ev["recovery"]),
        "--window-start", window_start, "--window-end", window_end,
    ]
    return _run(args)


def _fixture_dir_for_plan(plan: dict, plan_id: str) -> Path:
    """Resolve and bound the TEST_MODE fixture directory under TESTMODE_DIR."""
    oracle_home = (plan.get("target") or {}).get("oracle_home")
    if not oracle_home:
        raise PlanError(f"plan {plan_id} has no target.oracle_home to locate its TEST_MODE fixture")
    fixture_dir = Path(oracle_home).resolve().parent.parent
    root = TESTMODE_DIR.resolve()
    if root not in fixture_dir.parents and fixture_dir != root:
        raise PlanError(
            f"plan {plan_id} oracle_home is outside the TEST_MODE fixture root "
            f"({fixture_dir} not under {root})"
        )
    if not fixture_dir.is_dir():
        raise PlanError(f"No TEST_MODE fixture found at {fixture_dir} for plan {plan_id}")
    return fixture_dir


def execute_next_task(plan_id: str, actor: str) -> dict | None:
    """Runs the plan's next pending task through its real TEST_MODE fixture
    executor. Returns None when there is no pending task. Raises PlanError
    if the plan's adapter has no verified fixture wired up yet, or the
    fixture directory (created by create_testmode_demo) is missing.
    """
    validate_plan_id(plan_id)
    plan = status(plan_id)
    plan_state = plan.get("state")
    if plan_state == "succeeded":
        return None
    if plan_state != "running":
        raise PlanError(f"plan {plan_id} is {plan_state}, not running — nothing to execute")

    task = next_task(plan_id)
    if task is None:
        return None

    adapter = task.get("adapter")
    executor = EXECUTOR_BY_ADAPTER.get(adapter)
    if executor is None:
        raise PlanError(f"No TEST_MODE executor is wired up for adapter: {adapter}")

    # Fixture is derived from sealed target.oracle_home
    # (<fixture_dir>/oracle/dbhome_1) and must stay under TESTMODE_DIR.
    fixture_dir = _fixture_dir_for_plan(plan, plan_id)
    fx_env = testmode_fixtures.env_for(fixture_dir)

    exec_env = os.environ.copy()
    exec_env["OPU_PLAN_STATE_DIR"] = str(PLAN_STATE_DIR)
    exec_env.update(fx_env)

    argv = [
        str(executor), "execute",
        "--plan-id", plan_id,
        "--task-id", task["task_id"],
        "--actor", actor,
        "--lease-seconds", "60",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=exec_env)
    except subprocess.TimeoutExpired:
        raise PlanError(f"executor for task {task['task_id']} timed out") from None

    payload = None
    if result.stdout.strip():
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PlanError(
                f"executor produced unparsable output: {exc}",
                stderr=result.stdout[-2000:],
            ) from exc

    if result.returncode != 0:
        raise PlanError(
            f"executor for task {task['task_id']} exited {result.returncode}",
            stderr=result.stderr.strip(),
            result=payload,
        )
    if payload is None:
        raise PlanError(
            f"executor exited 0 with no output for task {task['task_id']}",
            stderr=result.stderr.strip(),
        )
    return payload


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
