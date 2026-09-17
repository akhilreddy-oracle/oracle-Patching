"""Wraps opu-patch-plan: create, approve, authorize, status, dispatch, next.

Runs locally (OPU_PLAN_STATE_DIR points at webapp/var/plans), which is
sufficient while the plan's target is a single reachable host/node. Real
multi-node RAC/Grid dispatch requires this state directory to be reachable
and identical from every node the plan will execute on — see Phase 4's
pre-flight check before enabling dispatch for such a plan.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
import runtime_paths

import evidence
import production
import remote
import testmode_fixtures
import tools_sync
import pipeline_runner
from durable import write_json
from adapters import EXECUTOR_PATHS

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_TOOL = REPO_ROOT / "bin" / "opu-patch-plan"
PLAN_STATE_DIR = runtime_paths.state_dir() / "plans"
TESTMODE_DIR = runtime_paths.state_dir() / "testmode"
HOSTS_FILE = runtime_paths.hosts_file()
DEFAULT_TIMEOUT_SECONDS = 30
LIVE_EXECUTE_TIMEOUT_SECONDS = 3600

# Matches lib/opu/common.sh opu_validate_identifier.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Local TEST_MODE and SSH executors share the package adapter registry.
EXECUTOR_BY_ADAPTER = {adapter: REPO_ROOT / path for adapter, path in EXECUTOR_PATHS.items()}

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

# Relative paths for live SSH execution, shared with pull agents.
LIVE_EXECUTOR_BY_ADAPTER = dict(EXECUTOR_PATHS)

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


def _redacted_diagnostic(text: str) -> str:
    # Redact before truncating so a long credential cannot lose its identifying
    # prefix at the tail boundary.
    diagnostic = re.sub(r"(?i)(\bBearer\s+)\S+", r"\1[REDACTED]", text)
    diagnostic = re.sub(
        r"(?i)(\b(?:password|passwd|pwd|token|secret|api[_-]?key)\b[\"']?\s*(?:[:=]\s*|\s+))"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[REDACTED]", diagnostic,
    )
    return diagnostic.strip()[-4000:]


def _unverified_remote_terminal(returncode: int, verified, stderr: str) -> PlanError:
    """Retain bounded diagnostics without treating an unclaimed task as finished."""
    task_status = verified.get("status") if isinstance(verified, dict) else None
    if task_status not in {"pending", "running", "unknown", "succeeded", "failed"}:
        task_status = "unknown"
    return PlanError(
        "remote exit exists but sealed task has no verified terminal result",
        stderr=_redacted_diagnostic(stderr),
        result={"exit_code": returncode, "task_status": task_status},
    )


def validate_plan_id(plan_id: str) -> str:
    if not isinstance(plan_id, str) or not _ID_RE.fullmatch(plan_id):
        raise PlanError(f"plan_id contains unsupported characters: {plan_id!r}")
    return plan_id


def _record_host(plan_id: str, host_id: str | None) -> None:
    validate_plan_id(plan_id)
    if host_id is not None:
        evidence.validate_host_id(host_id)
    write_json(PLAN_STATE_DIR / "webapp-metadata" / f"{plan_id}.json", {"host_id": host_id})


def _with_host_identity(plan: dict) -> dict:
    """Expose target attribution independently of a user-chosen plan name."""
    plan_id = validate_plan_id(plan.get("plan_id"))
    metadata = PLAN_STATE_DIR / "webapp-metadata" / f"{plan_id}.json"
    host_id = None
    if metadata.is_file() and not metadata.is_symlink():
        try:
            host_id = json.loads(metadata.read_text()).get("host_id")
            if host_id is not None:
                evidence.validate_host_id(host_id)
        except (ValueError, TypeError, AttributeError, OSError):
            host_id = None
    else:
        # Historical plans already bind the canonical per-host readiness path.
        # Resolve only this structural identity; never infer from plan_id prefixes.
        doc = (plan.get("source_documents") or {}).get("readiness") or {}
        source = doc.get("path")
        if isinstance(source, str):
            try:
                relative = Path(source).resolve().relative_to(evidence.VAR_DIR.resolve())
                if len(relative.parts) == 3 and relative.parts[1:] == ("evidence", "readiness.json"):
                    host_id = evidence.validate_host_id(relative.parts[0])
            except (ValueError, TypeError):
                pass
    return {**plan, "host_id": host_id}


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
    # Honor sealed policy.require_backup=false: omit recovery-evidence even if a
    # leftover recovery.json exists under the host evidence tree.
    policy_path = evidence.evidence_path(host_id, "policy")
    require_backup = True
    if policy_path.is_file():
        try:
            policy = json.loads(policy_path.read_text())
            require_backup = bool((policy.get("recovery") or {}).get("require_backup", True))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            require_backup = True
    recovery = evidence.evidence_path(host_id, "recovery")
    if require_backup and recovery.is_file():
        args += ["--recovery-evidence", str(recovery)]
    dataguard_order = evidence.evidence_path(host_id, "dataguard_order")
    if dataguard_order.is_file():
        args += ["--dataguard-order", str(dataguard_order)]
    result = _run(args)
    _record_host(plan_id, host_id)
    return _with_host_identity(result or status(plan_id))


def create_rollback(plan_id: str, requester: str, source_plan_id: str, window_start: str, window_end: str) -> dict:
    validate_plan_id(plan_id)
    validate_plan_id(source_plan_id)
    args = [
        "create-rollback", "--plan-id", plan_id, "--requester", requester,
        "--source-plan-id", source_plan_id, "--window-start", window_start, "--window-end", window_end,
    ]
    source = status(source_plan_id)
    def record_identity(result):
        _record_host(plan_id, source.get("host_id"))
        return _with_host_identity(result or status(plan_id))
    # TEST_MODE fixtures keep evidence/logs/artifact on the control plane.
    if _fixture_dir_for_plan(source, source_plan_id) is not None:
        return record_identity(_run(args))
    # Live apply plans seal remote absolute evidence_path, executor log paths,
    # and artifact.path. create-rollback must run on a node that still has those
    # files (plus mirrored sealed source documents).
    try:
        tasks = list_tasks(source_plan_id)
        task = next(
            (
                t for t in tasks
                if str(t.get("status") or "") == "succeeded"
                and str(t.get("stage") or "").endswith("final_validate")
            ),
            None,
        )
        if task is None and tasks:
            task = tasks[-1]
        if task is None:
            raise PlanError(f"source plan {source_plan_id} has no tasks for live create-rollback")
        host = _resolve_live_host_for_task(source, task)
    except PlanError:
        return record_identity(_run(args))
    return record_identity(_create_rollback_live(plan_id, requester, source_plan_id, window_start, window_end, host))


def _create_rollback_live(
    plan_id: str,
    requester: str,
    source_plan_id: str,
    window_start: str,
    window_end: str,
    host: dict,
) -> dict:
    """Run create-rollback on the live host, then pull the new plan locally."""
    local_new = PLAN_STATE_DIR / "plans" / plan_id
    if local_new.exists():
        raise PlanError(f"plan already exists locally: {plan_id}")
    remote_root = _sync_plan_to_host(host, source_plan_id)
    remote_tool = f"{host['remote_root'].rstrip('/')}/bin/opu-patch-plan"
    remote_argv = [
        "env", f"OPU_PLAN_STATE_DIR={remote_root}", remote_tool,
        "create-rollback",
        "--plan-id", plan_id,
        "--requester", requester,
        "--source-plan-id", source_plan_id,
        "--window-start", window_start,
        "--window-end", window_end,
    ]
    try:
        result = remote.run_remote_raw(
            host["ssh_alias"],
            remote_argv,
            timeout=DEFAULT_TIMEOUT_SECONDS * 4,
            sudo=bool(host.get("sudo")),
        )
    except remote.RemoteError as exc:
        raise PlanError(exc.message, stderr=exc.stderr) from exc
    if result.returncode != 0:
        raise PlanError(
            f"opu-patch-plan exited {result.returncode}",
            stderr=(result.stderr.strip() or result.stdout.strip())[-2000:],
        )
    try:
        _sync_plan_from_host(host, plan_id, remote_root)
    except Exception as sync_exc:  # noqa: BLE001
        raise PlanError(
            f"remote create-rollback finished but plan state sync failed: {sync_exc}"
        ) from sync_exc
    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PlanError(
                f"opu-patch-plan produced unparsable output: {exc}",
                stderr=result.stdout[-2000:],
            ) from exc
    return status(plan_id)


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


def retry_task(plan_id: str, task_id: str, actor: str) -> dict:
    """Re-open a plan paused by this task's failure so it can be executed again."""
    validate_plan_id(plan_id)
    if not task_id or not _ID_RE.match(task_id):
        raise PlanError(f"task_id contains unsupported characters: {task_id!r}")
    plan = status(plan_id)
    task = next((t for t in list_tasks(plan_id) if t.get("task_id") == task_id), None)
    if task is None:
        raise PlanError(f"task {task_id} not found on plan {plan_id}")
    result = _run([
        "retry-task", "--plan-id", plan_id, "--task-id", task_id, "--actor", actor,
    ])
    # Executors allocate an attempt generation; previous evidence is immutable.
    return result if isinstance(result, dict) else status(plan_id)


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
    plan = _with_host_identity(_run(["status", "--plan-id", plan_id]))
    key = f"plan:{plan_id}:execute"
    # These registry reads do not take the registry file lock: status is also
    # called while the existing reconciliation endpoint holds that lock.
    run_id = pipeline_runner.active_run_id(key)
    record = pipeline_runner.get_run(run_id) if run_id else None
    run = record.to_json() if record is not None else None
    if run and run.get("key") == key and run.get("status") in {"unknown", "reconciling"}:
        error = run.get("error") or {}
        diagnostic = {}
        if isinstance(error, dict):
            diagnostic = {name: _redacted_diagnostic(error[name]) for name in ("error", "message", "stderr") if isinstance(error.get(name), str)}
            result = error.get("result")
            if isinstance(result, dict):
                values = {}
                if isinstance(result.get("exit_code"), int) and not isinstance(result["exit_code"], bool):
                    values["exit_code"] = result["exit_code"]
                if isinstance(result.get("task_status"), str):
                    values["task_status"] = _redacted_diagnostic(result["task_status"])[:64]
                if values:
                    diagnostic["result"] = values
        plan["unresolved_run"] = {
            "run_id": run["run_id"], "status": run["status"],
            "context": run.get("context") or {}, "error": diagnostic,
        }
    return plan


def _read_sealed_actor(plan_id: str, name: str) -> str | None:
    path = PLAN_STATE_DIR / "plans" / plan_id / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get("actor") or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def sod_summary(plan_id: str, plan: dict | None = None) -> dict:
    """Who has acted on this plan and who is therefore barred from each step.

    Mirrors opu-patch-plan: approver != requester; operator (authorize) !=
    requester and != approver; dispatch only by the authorizing operator;
    rollback approver/operator != every source-apply worker.
    """
    validate_plan_id(plan_id)
    plan = plan if plan is not None else status(plan_id)
    requester = plan.get("requester") or None
    approver = _read_sealed_actor(plan_id, "approval.json")
    operator = _read_sealed_actor(plan_id, "authorization.json")
    source_workers = list((plan.get("source_apply") or {}).get("actors") or [])
    barred_approve = [a for a in [requester, *source_workers] if a]
    barred_authorize = [a for a in [requester, approver, *source_workers] if a]
    return {
        "requester": requester,
        "approver": approver,
        "operator": operator,
        "source_apply_actors": source_workers,
        "barred": {
            "approve": sorted(set(barred_approve)),
            "authorize": sorted(set(barred_authorize)),
            # dispatch must be the authorizing operator; expose as allowed list.
        },
        "allowed": {"dispatch": [operator] if operator else []},
    }


# States in which opu-patch-plan still re-validates window, readiness expiry and
# sealed source documents before it will move the plan forward.
_PRE_EXECUTION_STATES = ("awaiting_approval", "approved", "execution_authorized")


def _parse_utc(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def viability(plan_id: str, plan: dict | None = None, now: datetime | None = None) -> dict:
    """Can this plan still move forward? Mirrors the gates opu-patch-plan enforces
    at approve/authorize/dispatch (window open, readiness unexpired, every
    sealed source document present and unchanged) so the UI can explain a
    dead plan up front instead of surfacing the first raw exit-66 error.
    """
    validate_plan_id(plan_id)
    plan = plan if plan is not None else status(plan_id)
    now = now or datetime.now(timezone.utc)
    state = plan.get("state")
    blockers: list[str] = []

    window = plan.get("maintenance_window") or {}
    start = _parse_utc(window.get("start"))
    end = _parse_utc(window.get("end"))
    if start and end:
        window_state = "upcoming" if now < start else "closed" if now > end else "open"
    else:
        window_state = "unknown"
    if window_state == "closed":
        blockers.append(f"Maintenance window closed at {window.get('end')}; authorize/dispatch are no longer possible.")

    valid_until = _parse_utc((plan.get("planning_evidence") or {}).get("readiness_valid_until"))
    readiness_expired = bool(valid_until and now > valid_until)
    if readiness_expired:
        blockers.append(
            f"Sealed readiness evidence expired at {(plan.get('planning_evidence') or {}).get('readiness_valid_until')}; "
            "re-run readiness evaluation for the host."
        )

    documents: list[dict] = []
    bound: list[tuple[str, dict]] = [
        (name, doc) for name, doc in (plan.get("source_documents") or {}).items() if isinstance(doc, dict)
    ]
    for index, doc in enumerate(plan.get("snapshot_evidence") or []):
        if isinstance(doc, dict):
            bound.append((f"snapshot[{index}]", doc))
    for name, doc in bound:
        path = doc.get("path")
        expected = doc.get("sha256")
        entry = {"name": name, "path": path, "status": "ok"}
        if not isinstance(path, str) or not path.startswith("/"):
            entry["status"] = "unbound"
        else:
            file = Path(path)
            if not file.is_file() or file.is_symlink():
                entry["status"] = "missing"
            elif expected and hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                entry["status"] = "changed"
        documents.append(entry)
    bad = [d for d in documents if d["status"] in ("missing", "changed")]
    if bad:
        names = ", ".join(f"{d['name']} ({d['status']})" for d in bad)
        blockers.append(
            f"Sealed evidence no longer matches this plan: {names}. Rediscovery or a re-run of the readiness pipeline "
            "invalidates every plan sealed against the earlier documents."
        )

    actionable = state in _PRE_EXECUTION_STATES and not blockers
    return {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": window.get("start"), "end": window.get("end"), "state": window_state},
        "readiness_valid_until": (plan.get("planning_evidence") or {}).get("readiness_valid_until"),
        "readiness_expired": readiness_expired,
        "documents": documents,
        "blockers": blockers,
        "pre_execution": state in _PRE_EXECUTION_STATES,
        "actionable": actionable,
        "host_id": _host_id_for_plan(plan),
    }


def _host_id_for_plan(plan: dict) -> str | None:
    """Host the plan was sealed for, recovered from the evidence paths (…/var/hosts/<id>/evidence/…)."""
    for doc in [*(plan.get("snapshot_evidence") or []), *((plan.get("source_documents") or {}).values())]:
        path = doc.get("path") if isinstance(doc, dict) else None
        if isinstance(path, str):
            parts = Path(path).parts
            if "hosts" in parts:
                idx = parts.index("hosts")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    return None


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
    runtime_paths.require_fixtures_allowed()
    fixture_dir = (TESTMODE_DIR / plan_id).resolve()
    if TESTMODE_DIR.resolve() not in fixture_dir.parents and fixture_dir != TESTMODE_DIR.resolve():
        raise PlanError(f"plan_id escapes testmode root: {plan_id!r}")
    if fixture_dir.exists() or (PLAN_STATE_DIR / "plans" / plan_id).exists():
        raise PlanError(f"plan or fixture already exists: {plan_id}")
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
    result = _run(args)
    _record_host(plan_id, None)
    return _with_host_identity(result or status(plan_id))


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
    import host_config
    try:
        return host_config.load(HOSTS_FILE)
    except host_config.HostConfigError as exc:
        raise PlanError(str(exc)) from exc


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
    want = str(node_name or "").lower()
    if not want:
        raise PlanError("task/plan node name is empty")
    pairs = _iter_host_nodes()
    host_pairs = [(host, {"name": host["id"], "ssh_alias": host.get("ssh_alias")})
                  for host in {host["id"]: host for host, _ in pairs}.values()]
    # Prefer the complete configured identity. A short DNS alias is usable
    # only when unique; never let configuration ordering choose the target.
    matches = [(host, node) for host, node in pairs if str(node.get("name") or "").lower() == want]
    if not matches:
        matches = [(host, node) for host, node in host_pairs if str(node["name"]).lower() == want]
    if not matches:
        matches = [(host, node) for host, node in pairs
                   if _short_host(node.get("name")) == _short_host(want)
                   and ("." not in want or "." not in str(node.get("name") or ""))]
    if not matches:
        matches = [(host, node) for host, node in host_pairs
                   if _short_host(node["name"]) == _short_host(want)
                   and ("." not in want or "." not in str(node["name"]))]
    if len(matches) > 1:
        raise PlanError(f"Ambiguous hosts.json SSH mapping for node {node_name!r}; use its complete configured identity")
    if not matches:
        raise PlanError(f"No hosts.json SSH mapping for node {node_name!r}")
    host, node = matches[0]
    # Explicit null means "not wired for SSH"; only a missing key inherits.
    alias = node.get("ssh_alias") if "ssh_alias" in node else host.get("ssh_alias")
    if not alias:
        raise PlanError(f"hosts.json node {node.get('name')!r} has no ssh_alias; "
                        "multi-node live execute requires SSH to every plan node")
    return {**host, "ssh_alias": alias, "node_name": str(node["name"]).lower()}


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
    if node and str(node).lower() not in {"", "local", "cluster"}:
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


def _sealed_input_paths(plan_id: str) -> list[str]:
    """Absolute local paths the remote executor must re-hash during precheck."""
    plan_file = PLAN_STATE_DIR / "plans" / plan_id / "plan.json"
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read sealed plan for input sync: {exc}") from exc

    paths: list[str] = []
    for doc in (plan.get("source_documents") or {}).values():
        if isinstance(doc, dict) and isinstance(doc.get("path"), str):
            paths.append(doc["path"])
    for snap in plan.get("snapshot_evidence") or []:
        if isinstance(snap, dict) and isinstance(snap.get("path"), str):
            paths.append(snap["path"])
    recovery = plan.get("recovery") or {}
    if isinstance(recovery.get("manifest_path"), str):
        paths.append(recovery["manifest_path"])
    # Preserve order but drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _sync_sealed_inputs_to_host(host: dict, plan_id: str) -> None:
    """Mirror sealed source documents onto the execute host at their sealed paths.

    Plans bind control-plane absolute paths. Live execute runs on the target, so
    those files must exist there with identical content for verify_source_documents.
    """
    sudo = bool(host.get("sudo"))
    for path in _sealed_input_paths(plan_id):
        local = Path(path)
        if not local.is_file() or local.is_symlink():
            raise PlanError(f"sealed source document missing locally for live sync: {path}")
        try:
            remote.push_file(host["ssh_alias"], path, local.read_bytes(), timeout=120, sudo=sudo)
        except remote.RemoteError as exc:
            raise PlanError(
                f"failed to sync sealed source document to {host['ssh_alias']}: {path}",
                stderr=exc.stderr,
            ) from exc


def _temporary_remote_archive(host: dict) -> str:
    """Reserve private transfer storage without opening predictable /tmp files."""
    response = remote.run_remote_shell(host["ssh_alias"],
        "umask 077; mktemp -d /tmp/opu-plan-transfer.XXXXXXXXXXXX",
        timeout=60, sudo=bool(host.get("sudo")))
    directory = response.stdout.strip()
    if response.returncode != 0 or not re.fullmatch(r"/tmp/opu-plan-transfer\.[A-Za-z0-9]{12}", directory):
        raise PlanError("could not allocate a private remote plan transfer directory", stderr=response.stderr)
    return directory


def _remove_remote_archive(host: dict, directory: str) -> None:
    # Remove only the known file and its empty private directory. A failed
    # cleanup must not hide a verified result or the original transfer error.
    try:
        remote.run_remote_checked(host["ssh_alias"], ["rm", "-f", "--", directory + "/plan.tar"],
                                  timeout=30, sudo=bool(host.get("sudo")))
        remote.run_remote_checked(host["ssh_alias"], ["rmdir", "--", directory],
                                  timeout=30, sudo=bool(host.get("sudo")))
    except remote.RemoteError:
        pass


def _sync_plan_to_host(host: dict, plan_id: str) -> str:
    local_plan = PLAN_STATE_DIR / "plans" / plan_id
    if not local_plan.is_dir():
        raise PlanError(f"local plan directory missing for {plan_id}")
    remote_root = _remote_plan_root(host)
    sudo = bool(host.get("sudo"))
    # Executors on the host must match this checkout before any task runs.
    tools_sync.ensure_tools(host["ssh_alias"], str(host.get("remote_root") or ""), sudo)
    # mkdir/tar/rm succeed with empty stdout — do not use run_remote (requires output).
    # Use sudo when the host executor runs as root: prior task files are root-owned.
    remote.run_remote_checked(
        host["ssh_alias"], ["mkdir", "-p", f"{remote_root}/plans"], timeout=60, sudo=sudo,
    )
    directory = _temporary_remote_archive(host)
    remote_tar = directory + "/plan.tar"
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar") as handle:
            with tarfile.open(fileobj=handle, mode="w") as archive:
                archive.add(local_plan, arcname=plan_id,
                            filter=lambda member: None if Path(member.name).name == ".task-lock" else member)
            handle.flush()
            handle.seek(0)
            remote.push_file(host["ssh_alias"], remote_tar, Path(handle.name).read_bytes(), timeout=120, sudo=sudo)
        remote.run_remote_checked(
            host["ssh_alias"], ["tar", "-xf", remote_tar, "-C", f"{remote_root}/plans"],
            timeout=120, sudo=sudo,
        )
    finally:
        _remove_remote_archive(host, directory)
    _sync_sealed_inputs_to_host(host, plan_id)
    return remote_root


def _sync_plan_from_host(host: dict, plan_id: str, remote_root: str) -> None:
    sudo = bool(host.get("sudo"))
    directory = _temporary_remote_archive(host)
    remote_tar = directory + "/plan.tar"
    try:
        # Executor writes task JSON with mode 0600 root:root; tar and pull
        # use the same privilege as the private directory reservation.
        remote.run_remote_checked(
            host["ssh_alias"], ["tar", "--exclude", f"{plan_id}/.task-lock", "-cf", remote_tar, "-C", f"{remote_root}/plans", plan_id],
            timeout=120, sudo=sudo,
        )
        payload = remote.pull_file(host["ssh_alias"], remote_tar, timeout=120, sudo=sudo)
    except remote.RemoteError as exc:
        raise PlanError(
            f"failed to pull plan state from {host['ssh_alias']}",
            stderr=exc.stderr,
        ) from exc
    finally:
        _remove_remote_archive(host, directory)
    local_plans = PLAN_STATE_DIR / "plans"
    local_plans.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar") as handle:
        handle.write(payload)
        handle.flush()
        with tarfile.open(handle.name, mode="r") as archive:
            members = archive.getmembers()
            root = local_plans.resolve()
            for member in members:
                parts = Path(member.name).parts
                target = (local_plans / member.name).resolve()
                if not parts or parts[0] != plan_id or ".." in parts or ".task-lock" in parts or root not in target.parents or not (member.isfile() or member.isdir()):
                    raise PlanError("remote plan archive contains an unsafe or unrelated member")
            archive.extractall(local_plans, members=members)


def _execute_testmode(plan_id: str, task: dict, actor: str, fixture_dir: Path) -> dict:
    adapter = task.get("adapter")
    executor = EXECUTOR_BY_ADAPTER.get(adapter)
    if executor is None:
        raise PlanError(f"No TEST_MODE executor is wired up for adapter: {adapter}")
    runtime_paths.require_fixtures_allowed()
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
    pipeline_runner.record_event('task_selected', 'Preparing the next sealed task', task_id=task['task_id'], stage=task.get('stage'), node=task.get('node'))
    remote_root = _sync_plan_to_host(host, plan_id)
    remote_executor = f"{host['remote_root'].rstrip('/')}/{rel_executor}"
    remote_argv = [
        "env", f"OPU_PLAN_STATE_DIR={remote_root}", "OPU_PLAN_WORKER_SNAPSHOT=1", remote_executor,
        "execute", "--plan-id", plan_id, "--task-id", task["task_id"],
        "--actor", actor, "--lease-seconds", "3600",
    ]
    pipeline_runner.set_execution_context(task_definition_sha256=task.get('task_definition_sha256'),
                                          task_retry_count=task.get('retry_count', 0))
    returncode, stdout, stderr = _run_detached_remote(host, plan_id, task["task_id"], remote_argv)

    try:
        _sync_plan_from_host(host, plan_id, remote_root)
    except Exception as sync_exc:  # noqa: BLE001
        raise PlanError(f"remote execute finished but plan state sync failed: {sync_exc}") from sync_exc

    verified = _run(["task-status", "--plan-id", plan_id, "--task-id", task["task_id"]])
    if not isinstance(verified, dict) or verified.get("status") not in {"succeeded", "failed"}:
        raise _unverified_remote_terminal(returncode, verified, stderr)
    if (returncode == 0) != (verified["status"] == "succeeded"):
        raise PlanError("remote exit contradicts the verified terminal task result")
    if status(plan_id).get("state") == "succeeded":
        _run(["reconcile", "--plan-id", plan_id, "--actor", actor])
    pipeline_runner.set_execution_context(detached_terminal=True)
    pipeline_runner.record_event('task_verified', 'Native task result and evidence verified', task_id=task['task_id'], status=verified['status'])
    return _parse_executor_result(task["task_id"], returncode, stdout, stderr)


LIVE_POLL_INTERVAL_SECONDS = 10
LIVE_POLL_SSH_TIMEOUT_SECONDS = 60
LIVE_POLL_MAX_CONSECUTIVE_SSH_FAILURES = 6


def _remote_run_dir(host: dict, plan_id: str, task_id: str) -> str:
    return f"{host['remote_root'].rstrip('/')}/var/webapp-runs/{plan_id}/{task_id}"


def _run_detached_remote(host: dict, plan_id: str, task_id: str, remote_argv: list[str]) -> tuple[int, str, str]:
    """Launch the executor detached on the host and poll for completion.

    A single long SSH session is fragile for 20-60 minute OPatch/datapatch
    runs: idle NAT timeouts drop it (ssh exit 255) and the SIGHUP can kill the
    executor mid-mutation. Instead the executor runs under setsid/nohup with
    its stdout/stderr/rc captured in a run directory, and the control plane
    polls with short sessions that tolerate transient SSH failures.
    """
    import time

    ssh_alias = host["ssh_alias"]
    sudo = bool(host.get("sudo"))
    # Never overwrite a previous launch's pid/rc/log files when retrying.
    run_dir = f"{_remote_run_dir(host, plan_id, task_id)}/{uuid.uuid4().hex}"
    q_run = shlex.quote(run_dir)
    q_cmd = " ".join(shlex.quote(a) for a in remote_argv)
    # The wrapper records its own PID (setsid may fork), and rc is written
    # last, so its presence means stdout/stderr are complete.
    wrapper = f"umask 077; echo $$ >pid.tmp && mv pid.tmp pid || exit 1; {q_cmd} >stdout 2>stderr </dev/null; echo $? >rc.tmp && mv rc.tmp rc"
    q_parent = shlex.quote(str(Path(run_dir).parent))
    launch = (
        f"set -eu; umask 077; mkdir -p {q_parent}; mkdir {q_run}; cd {q_run}; "
        f"if command -v setsid >/dev/null 2>&1; then SETSID=setsid; else SETSID=; fi; "
        f"nohup $SETSID bash -c {shlex.quote(wrapper)} >/dev/null 2>&1 </dev/null & "
        f"for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s {q_run}/pid ] && break; sleep 1; done; cat {q_run}/pid"
    )
    pipeline_runner.set_execution_context(
        detached_execution=True, detached_terminal=False, plan_id=plan_id,
        task_id=task_id, node=host.get("node_name") or host.get("id"),
        host_id=host.get("id"), ssh_alias=ssh_alias,
        remote_root=host["remote_root"], remote_run_dir=run_dir,
    )
    pipeline_runner.record_event('remote_launch', 'Launching the sealed worker', task_id=task_id, node=host.get('node_name') or host.get('id'))
    try:
        launched = remote.run_remote_shell(ssh_alias, launch, timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=sudo)
    except remote.RemoteError as exc:
        raise PlanError(f"failed to launch executor on {ssh_alias}: {exc.message}", stderr=exc.stderr) from exc
    if launched.returncode != 0 or not launched.stdout.strip():
        raise PlanError(
            f"failed to launch executor on {ssh_alias} (ssh exit {launched.returncode})",
            stderr=launched.stderr.strip(),
        )

    # Poll: print rc when done, else report whether the wrapper is still alive.
    poll = (
        f"cd {q_run} 2>/dev/null || {{ echo MISSING; exit 0; }}; "
        f"if [ -f rc ]; then echo RC; cat rc; "
        f"elif kill -0 \"$(cat pid 2>/dev/null)\" 2>/dev/null; then echo RUNNING; "
        f"else echo DEAD; fi"
    )
    deadline = time.monotonic() + LIVE_EXECUTE_TIMEOUT_SECONDS
    ssh_failures = 0
    while True:
        time.sleep(LIVE_POLL_INTERVAL_SECONDS)
        try:
            polled = remote.run_remote_shell(ssh_alias, poll, timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=sudo)
            if polled.returncode != 0:
                raise remote.RemoteError("ssh_poll_failed", f"poll exited {polled.returncode}", stderr=polled.stderr)
        except remote.RemoteError as exc:
            pipeline_runner.controller_poll('contact_lost')
            ssh_failures += 1
            if ssh_failures >= LIVE_POLL_MAX_CONSECUTIVE_SSH_FAILURES:
                raise PlanError(
                    f"lost contact with {ssh_alias} while task {task_id} was executing; "
                    f"the remote executor may still be running — re-check plan state before retrying",
                    stderr=exc.stderr,
                ) from exc
            continue
        ssh_failures = 0
        lines = polled.stdout.strip().splitlines()
        state = lines[0] if lines else ""
        pipeline_runner.controller_poll(state.lower() or 'unknown')
        if state == "RC":
            try:
                returncode = int((lines[1] if len(lines) > 1 else "").strip() or "1")
            except ValueError:
                returncode = 1
            break
        if state == "RUNNING":
            if time.monotonic() > deadline:
                raise PlanError(
                    f"executor for task {task_id} still running on {ssh_alias} after {LIVE_EXECUTE_TIMEOUT_SECONDS}s; "
                    "leaving it running — re-check plan state before retrying"
                )
            continue
        if state == "MISSING":
            raise PlanError(f"remote run directory vanished on {ssh_alias}: {run_dir}")
        # DEAD: wrapper gone without writing rc (host reboot, OOM, manual kill).
        raise PlanError(
            f"executor for task {task_id} on {ssh_alias} terminated without an exit status; "
            "inspect the sealed task evidence before retrying"
        )

    def _read(name: str) -> str:
        try:
            return remote.pull_file(ssh_alias, f"{run_dir}/{name}", timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=sudo).decode(
                "utf-8", errors="replace"
            )
        except remote.RemoteError:
            # pull_file treats an empty file as failure; empty is legitimate here.
            return ""

    return returncode, _read("stdout"), _read("stderr")


def reconcile_detached_run(record: dict) -> dict:
    """Inspect an existing launch and import its result without launching work."""
    context = record.get("context") or {}
    try:
        plan_id = validate_plan_id(context.get("plan_id"))
        task_id = str(context.get("task_id") or "")
        if not _ID_RE.fullmatch(task_id):
            raise PlanError("invalid persisted task identity")
        host = _resolve_node_host(str(context.get("node") or ""))
        if host.get("ssh_alias") != context.get("ssh_alias") or host.get("remote_root") != context.get("remote_root") or host.get("id") != context.get("host_id"):
            raise PlanError("host configuration changed; cannot safely reconcile the persisted launch")
        run_dir = str(context.get("remote_run_dir") or "")
        prefix = _remote_run_dir(host, plan_id, task_id) + "/"
        if not run_dir.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{32}", run_dir[len(prefix):]):
            raise PlanError("invalid persisted remote run directory")
        poll = (
            f"cd {shlex.quote(run_dir)} 2>/dev/null || {{ echo MISSING; exit 0; }}; "
            "if [ -f rc ]; then echo RC; cat rc; "
            "elif kill -0 \"$(cat pid 2>/dev/null)\" 2>/dev/null; then echo RUNNING; "
            "else echo DEAD; fi"
        )
        response = remote.run_remote_shell(host["ssh_alias"], poll, timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=bool(host.get("sudo")))
        lines = response.stdout.strip().splitlines()
        if response.returncode != 0 or len(lines) < 2 or lines[0] != "RC":
            return {"status": "unknown", "error": {"message": "Remote execution is active or has no verified terminal result", "remote_state": lines[0] if lines else "UNREACHABLE"}}
        returncode = int(lines[1])
        def read_log(name):
            result = remote.run_remote_raw(host["ssh_alias"], ["cat", f"{run_dir}/{name}"], timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=bool(host.get("sudo")))
            if result.returncode != 0:
                raise PlanError(f"cannot read terminal executor {name}")
            return result.stdout
        stdout, stderr = read_log("stdout"), read_log("stderr")
        _sync_plan_from_host(host, plan_id, _remote_plan_root(host))
        verified = _run(["task-status", "--plan-id", plan_id, "--task-id", task_id])
        if not isinstance(verified, dict) or verified.get("status") not in {"succeeded", "failed"}:
            return {"status": "unknown", "error": _unverified_remote_terminal(returncode, verified, stderr).to_json()}
        if (returncode == 0) != (verified["status"] == "succeeded"):
            raise PlanError("remote exit contradicts verified task custody")
        if status(plan_id).get("state") == "succeeded":
            reconciliation_actor = record.get("reconciliation_actor")
            if not isinstance(reconciliation_actor, str) or not _ID_RE.fullmatch(reconciliation_actor):
                raise PlanError("authenticated reconciliation actor is required")
            _run(["reconcile", "--plan-id", plan_id, "--actor", reconciliation_actor])
        try:
            result = _parse_executor_result(task_id, returncode, stdout, stderr)
        except PlanError as exc:
            return {"status": "failed", "error": exc.to_json()}
        return {"status": "succeeded", "result": {"task_result": result, "reconciled": True, "plan_id": plan_id, "task_id": task_id}}
    except (PlanError, remote.RemoteError, ValueError, OSError) as exc:
        return {"status": "unknown", "error": {"message": str(exc)}}

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
    else:
        stopped_reason = "max_tasks_reached"
    final = status(plan_id)
    if final.get("state") == "succeeded":
        stopped_reason = "succeeded"
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
    if _fixture_dir_for_plan(plan, plan_id) is not None:
        runtime_paths.require_fixtures_allowed()
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
