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
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

import evidence
import production
import remote
import testmode_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_TOOL = REPO_ROOT / "bin" / "opu-patch-plan"
PLAN_STATE_DIR = Path(__file__).resolve().parent / "var" / "plans"
TESTMODE_DIR = Path(__file__).resolve().parent / "var" / "testmode"
HOSTS_FILE = Path(__file__).resolve().parent / "hosts.json"
DEFAULT_TIMEOUT_SECONDS = 30
LIVE_EXECUTE_TIMEOUT_SECONDS = 3600

# Matches lib/opu/common.sh opu_validate_identifier.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Local TEST_MODE fixture executors.
EXECUTOR_BY_ADAPTER = {
    "database_single_instance_opatch": REPO_ROOT / "bin" / "opu-database-single-instance-patch",
    "database_single_instance_opatch_rollback": REPO_ROOT / "bin" / "opu-database-single-instance-rollback",
    "database_rolling_opatch": REPO_ROOT / "bin" / "opu-database-rac-node-patch",
    "database_rac_opatch_rollback": REPO_ROOT / "bin" / "opu-database-rac-node-rollback",
    "grid_rolling_opatch": REPO_ROOT / "bin" / "opu-grid-node-patch",
    "grid_rolling_opatch_rollback": REPO_ROOT / "bin" / "opu-grid-node-rollback",
    "grid_rolling_opatchauto": REPO_ROOT / "bin" / "opu-grid-opatchauto-patch",
    "grid_rolling_opatchauto_rollback": REPO_ROOT / "bin" / "opu-grid-opatchauto-rollback",
    "database_ojvm_opatch": REPO_ROOT / "bin" / "opu-database-ojvm-patch",
    "database_ojvm_opatch_rollback": REPO_ROOT / "bin" / "opu-database-ojvm-rollback",
    "database_out_of_place_switch": REPO_ROOT / "bin" / "opu-database-out-of-place-patch",
    "database_out_of_place_switchback": REPO_ROOT / "bin" / "opu-database-out-of-place-switchback",
}

# The RAC and Grid executors bind each sealed task to the node it runs on; in
# TEST_MODE that node identity comes from an env var instead of hostname -s.
TESTMODE_NODE_ENV_BY_ADAPTER = {
    "database_rolling_opatch": "OPU_RAC_DATABASE_TEST_HOSTNAME",
    "database_rac_opatch_rollback": "OPU_RAC_DATABASE_TEST_HOSTNAME",
    "grid_rolling_opatch": "OPU_GRID_NODE_TEST_HOST",
    "grid_rolling_opatch_rollback": "OPU_GRID_NODE_TEST_HOST",
    "grid_rolling_opatchauto": "OPU_GRID_OPATCHAUTO_TEST_HOST",
    "grid_rolling_opatchauto_rollback": "OPU_GRID_OPATCHAUTO_TEST_HOST",
}

# Live SSH executors (relative to host remote_root). Plan state is synced to the
# task's node before each execute and pulled back so multi-node RAC/Grid can
# share controller-mediated state without NFS.
LIVE_EXECUTOR_BY_ADAPTER = {
    "database_single_instance_opatch": "bin/opu-database-single-instance-patch",
    "database_single_instance_opatch_rollback": "bin/opu-database-single-instance-rollback",
    "database_rolling_opatch": "bin/opu-database-rac-node-patch",
    "database_rac_opatch_rollback": "bin/opu-database-rac-node-rollback",
    "grid_rolling_opatch": "bin/opu-grid-node-patch",
    "grid_rolling_opatch_rollback": "bin/opu-grid-node-rollback",
    "grid_rolling_opatchauto": "bin/opu-grid-opatchauto-patch",
    "grid_rolling_opatchauto_rollback": "bin/opu-grid-opatchauto-rollback",
    "database_ojvm_opatch": "bin/opu-database-ojvm-patch",
    "database_ojvm_opatch_rollback": "bin/opu-database-ojvm-rollback",
    "database_out_of_place_switch": "bin/opu-database-out-of-place-patch",
    "database_out_of_place_switchback": "bin/opu-database-out-of-place-switchback",
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
    dataguard_order = evidence.evidence_path(host_id, "dataguard_order")
    if dataguard_order.is_file():
        args += ["--dataguard-order", str(dataguard_order)]
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


def _create_testmode_plan(plan_id: str, requester: str, window_start: str, window_end: str, builder) -> dict:
    """Builds a fresh TEST_MODE fixture and creates a plan directly from its
    evidence. This is a self-contained demo path — the plan targets the
    fixture's fake Oracle/Grid home, not any real host in hosts.json, so it
    can only ever run against the fixture.
    """
    validate_plan_id(plan_id)
    TESTMODE_DIR.mkdir(parents=True, exist_ok=True)
    fixture_dir = (TESTMODE_DIR / plan_id).resolve()
    if TESTMODE_DIR.resolve() not in fixture_dir.parents and fixture_dir != TESTMODE_DIR.resolve():
        raise PlanError(f"plan_id escapes testmode root: {plan_id!r}")
    fx = builder(fixture_dir)
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


def create_testmode_demo(plan_id: str, requester: str, window_start: str, window_end: str) -> dict:
    """Standalone single-instance database TEST_MODE demo plan."""
    return _create_testmode_plan(plan_id, requester, window_start, window_end, testmode_fixtures.build)


def create_testmode_demo_rac(plan_id: str, requester: str, window_start: str, window_end: str) -> dict:
    """Two-node RAC database TEST_MODE demo plan (database_rolling_opatch)."""
    return _create_testmode_plan(plan_id, requester, window_start, window_end, testmode_fixtures.build_rac)


def create_testmode_demo_grid(plan_id: str, requester: str, window_start: str, window_end: str) -> dict:
    """Two-node Grid Infrastructure TEST_MODE demo plan (grid_rolling_opatch)."""
    return _create_testmode_plan(plan_id, requester, window_start, window_end, testmode_fixtures.build_grid)


def _fixture_dir_for_plan(plan: dict, plan_id: str) -> Path | None:
    """Return TEST_MODE fixture dir when the plan targets webapp/var/testmode."""
    target = plan.get("target") or {}
    # Database plans seal oracle_home; Grid plans seal only grid_home. Both
    # fixture layouts keep the sealed home exactly two levels below the
    # fixture root.
    sealed_home = target.get("oracle_home") or target.get("grid_home")
    if not sealed_home:
        return None
    fixture_dir = Path(sealed_home).resolve().parent.parent
    root = TESTMODE_DIR.resolve()
    if root not in fixture_dir.parents and fixture_dir != root:
        return None
    if not fixture_dir.is_dir():
        return None
    return fixture_dir


def _short_host(name: str) -> str:
    return str(name or "").split(".", 1)[0].lower()


def _load_hosts() -> dict[str, dict]:
    data = json.loads(HOSTS_FILE.read_text())
    return {host["id"]: host for host in data["hosts"]}


def _iter_host_nodes() -> list[tuple[dict, dict]]:
    """Yield (host, node) pairs from hosts.json."""
    pairs = []
    for host in _load_hosts().values():
        nodes = host.get("nodes") or [{"name": host.get("id"), "ssh_alias": host.get("ssh_alias")}]
        for node in nodes:
            pairs.append((host, node))
    return pairs


def _resolve_node_host(node_name: str) -> dict:
    """Map a sealed plan/task node name to a hosts.json entry with ssh_alias."""
    want = _short_host(node_name)
    if not want:
        raise PlanError("task/plan node name is empty")
    for host, node in _iter_host_nodes():
        name = _short_host(node.get("name") or "")
        # Explicit null means "not wired for SSH" (fail closed). Missing key
        # inherits the host-level ssh_alias.
        if "ssh_alias" in node:
            alias = node.get("ssh_alias")
        else:
            alias = host.get("ssh_alias")
        if name == want:
            if not alias:
                raise PlanError(
                    f"hosts.json node {node.get('name')!r} has no ssh_alias; "
                    "multi-node live execute requires SSH to every plan node"
                )
            resolved = dict(host)
            resolved["ssh_alias"] = alias
            resolved["node_name"] = name
            return resolved
    # Fall back to host id match (standalone estates).
    for host in _load_hosts().values():
        if _short_host(host.get("id") or "") == want and host.get("ssh_alias"):
            resolved = dict(host)
            resolved["node_name"] = want
            return resolved
    raise PlanError(f"No hosts.json SSH mapping for node {node_name!r}")


def preflight_live_plan_nodes(plan: dict) -> None:
    """Refuse live execute unless every sealed plan node has an SSH alias."""
    nodes = plan.get("nodes") or []
    if not nodes:
        raise PlanError("plan has no sealed nodes for live execute")
    missing = []
    for node in nodes:
        try:
            _resolve_node_host(str(node))
        except PlanError as exc:
            missing.append(f"{node}: {exc.message}")
    if missing:
        raise PlanError("Live execute preflight failed:\n- " + "\n- ".join(missing))


def _task_execution_node(plan: dict, task: dict) -> str:
    """Pick the hostname the executor must run on for this sealed task."""
    node = task.get("node")
    if node and _short_host(node) not in {"", "local", "cluster"}:
        return str(node)
    target = plan.get("target") or {}
    coordinator = target.get("coordinator_node")
    if coordinator:
        return str(coordinator)
    nodes = plan.get("nodes") or []
    if nodes:
        return str(nodes[0])
    raise PlanError(f"task {task.get('task_id')} has no resolvable execution node")


def _resolve_live_host_for_task(plan: dict, task: dict) -> dict:
    preflight_live_plan_nodes(plan)
    return _resolve_node_host(_task_execution_node(plan, task))


def _remote_plan_root(host: dict) -> str:
    return f"{host['remote_root'].rstrip('/')}/var/webapp-plans"


def _sync_plan_to_host(host: dict, plan_id: str) -> str:
    local_plan = PLAN_STATE_DIR / "plans" / plan_id
    if not local_plan.is_dir():
        raise PlanError(f"local plan directory missing for {plan_id}")
    remote_root = _remote_plan_root(host)
    remote.run_remote(host["ssh_alias"], ["mkdir", "-p", f"{remote_root}/plans"], timeout=60)
    with tempfile.NamedTemporaryFile(suffix=".tar") as handle:
        with tarfile.open(fileobj=handle, mode="w") as archive:
            archive.add(local_plan, arcname=plan_id)
        handle.flush()
        handle.seek(0)
        remote_tar = f"/tmp/opu-plan-{plan_id}.tar"
        remote.push_file(host["ssh_alias"], remote_tar, Path(handle.name).read_bytes(), timeout=120)
    remote.run_remote(
        host["ssh_alias"],
        ["tar", "-xf", remote_tar, "-C", f"{remote_root}/plans"],
        timeout=120,
    )
    remote.run_remote(host["ssh_alias"], ["rm", "-f", remote_tar], timeout=30)
    return remote_root


def _sync_plan_from_host(host: dict, plan_id: str, remote_root: str) -> None:
    remote_tar = f"/tmp/opu-plan-{plan_id}-back.tar"
    remote.run_remote(
        host["ssh_alias"],
        ["tar", "-cf", remote_tar, "-C", f"{remote_root}/plans", plan_id],
        timeout=120,
    )
    # Pull via ssh cat (push_file is upload-only).
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
        host["ssh_alias"], f"cat {shlex.quote(remote_tar)}",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise PlanError(f"timed out pulling plan state from {host['ssh_alias']}") from exc
    if result.returncode != 0 or not result.stdout:
        raise PlanError(
            f"failed to pull plan state from {host['ssh_alias']}",
            stderr=result.stderr.decode("utf-8", errors="replace"),
        )
    local_plans = PLAN_STATE_DIR / "plans"
    local_plans.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar") as handle:
        handle.write(result.stdout)
        handle.flush()
        with tarfile.open(handle.name, mode="r") as archive:
            archive.extractall(local_plans)
    remote.run_remote(host["ssh_alias"], ["rm", "-f", remote_tar], timeout=30)


def _execute_testmode(plan_id: str, task: dict, actor: str, fixture_dir: Path) -> dict:
    adapter = task.get("adapter")
    executor = EXECUTOR_BY_ADAPTER.get(adapter)
    if executor is None:
        raise PlanError(f"No TEST_MODE executor is wired up for adapter: {adapter}")
    fx_env = testmode_fixtures.env_for(fixture_dir)
    exec_env = os.environ.copy()
    exec_env["OPU_PLAN_STATE_DIR"] = str(PLAN_STATE_DIR)
    exec_env.update(fx_env)
    node_env = TESTMODE_NODE_ENV_BY_ADAPTER.get(adapter)
    if node_env is not None:
        node = str(task.get("node") or "")
        if not node:
            raise PlanError(f"task {task.get('task_id')} has no sealed node for TEST_MODE execute")
        exec_env[node_env] = node
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
    return _parse_executor_result(task["task_id"], result.returncode, result.stdout, result.stderr)


def _execute_live(plan_id: str, plan: dict, task: dict, actor: str) -> dict:
    production.require_live_mutation_allowed()
    adapter = task.get("adapter")
    rel_executor = LIVE_EXECUTOR_BY_ADAPTER.get(adapter)
    if rel_executor is None:
        raise PlanError(
            f"Live execute is not wired for adapter {adapter!r}; "
            "supported adapters: " + ", ".join(sorted(LIVE_EXECUTOR_BY_ADAPTER))
        )
    host = _resolve_live_host_for_task(plan, task)
    remote_root = _sync_plan_to_host(host, plan_id)
    remote_executor = f"{host['remote_root'].rstrip('/')}/{rel_executor}"
    remote_argv = [
        "env", f"OPU_PLAN_STATE_DIR={remote_root}", remote_executor,
        "execute", "--plan-id", plan_id, "--task-id", task["task_id"],
        "--actor", actor, "--lease-seconds", "3600",
    ]
    try:
        result = remote.run_remote_raw(
            host["ssh_alias"],
            remote_argv,
            timeout=LIVE_EXECUTE_TIMEOUT_SECONDS,
            sudo=bool(host.get("sudo")),
        )
    except remote.RemoteError as exc:
        try:
            _sync_plan_from_host(host, plan_id, remote_root)
        except Exception:  # noqa: BLE001
            pass
        raise PlanError(exc.message, stderr=exc.stderr) from exc

    try:
        _sync_plan_from_host(host, plan_id, remote_root)
    except Exception as sync_exc:  # noqa: BLE001
        raise PlanError(f"remote execute finished but plan state sync failed: {sync_exc}") from sync_exc

    return _parse_executor_result(task["task_id"], result.returncode, result.stdout, result.stderr)

def _parse_executor_result(task_id: str, returncode: int, stdout: str, stderr: str) -> dict:
    payload = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise PlanError(
                f"executor produced unparsable output: {exc}",
                stderr=stdout[-2000:],
            ) from exc
    if returncode != 0:
        raise PlanError(
            f"executor for task {task_id} exited {returncode}",
            stderr=stderr.strip(),
            result=payload,
        )
    if payload is None:
        raise PlanError(f"executor exited 0 with no output for task {task_id}", stderr=stderr.strip())
    return payload


def execute_next_task(plan_id: str, actor: str) -> dict | None:
    """Run the next pending task via TEST_MODE fixture or live SSH executor."""
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

    fixture_dir = _fixture_dir_for_plan(plan, plan_id)
    if fixture_dir is not None:
        return _execute_testmode(plan_id, task, actor, fixture_dir)
    return _execute_live(plan_id, plan, task, actor)


def execute_remaining_tasks(plan_id: str, actor: str, *, max_tasks: int = 200) -> dict:
    """Execute pending tasks serially until the plan leaves running or max_tasks."""
    validate_plan_id(plan_id)
    if max_tasks < 1 or max_tasks > 500:
        raise PlanError("max_tasks must be between 1 and 500")
    results: list[dict] = []
    stopped_reason = "succeeded_or_idle"
    for _ in range(max_tasks):
        plan = status(plan_id)
        state = plan.get("state")
        if state == "succeeded":
            stopped_reason = "succeeded"
            break
        if state != "running":
            stopped_reason = f"plan_state_{state}"
            break
        result = execute_next_task(plan_id, actor)
        if result is None:
            stopped_reason = "no_pending_task"
            break
        results.append(result)
        # Stop after a failed/blocked task so operators can intervene.
        task_status = (result.get("status") if isinstance(result, dict) else None) or ""
        if str(task_status).lower() in {"failed", "blocked", "error"}:
            stopped_reason = f"task_{task_status}"
            break
    final = status(plan_id)
    return {
        "plan_id": plan_id,
        "executed_count": len(results),
        "stopped_reason": stopped_reason,
        "plan_state": final.get("state"),
        "task_results": results,
    }


def publish_agent_queue(plan_id: str) -> list[dict]:
    """Publish pending sealed tasks into the lab pull-agent queue."""
    import agent_queue

    validate_plan_id(plan_id)
    plan = status(plan_id)
    tasks = list_tasks(plan_id)
    return agent_queue.publish_plan_tasks(plan, tasks)


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
