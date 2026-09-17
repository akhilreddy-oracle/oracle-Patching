"""Typed bridge for one sealed, pre-claim standalone host-lock recovery.

The native helper owns process inspection and service operations. A successful
recovery closes only the rejected application launch; its task stays pending.
"""
from __future__ import annotations

import hashlib
import json
import re

import pipeline_runner
import planctl
import remote
import tools_sync

LockError = planctl.PlanError
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_APP_RUN = re.compile(r"[a-f0-9]{12}\Z")
_REMOTE_RUN = re.compile(r"[a-f0-9]{32}\Z")
_AUDIT_ROOT = "/var/lib/oracle-patching-utility/lock-recovery"


def _require(condition, message):
    if not condition:
        raise LockError(message)


def _scope(plan_id, actor, run_id=None, *, record=None, allow_closed=False):
    for name, value in (("plan_id", plan_id), ("actor", actor)):
        _require(isinstance(value, str) and _IDENTIFIER.fullmatch(value), f"invalid {name}")
    run_id = run_id or pipeline_runner.active_run_id(f"plan:{plan_id}:execute")
    _require(isinstance(run_id, str) and _APP_RUN.fullmatch(run_id), "an existing unresolved execution run is required")
    if record is None:
        stored = pipeline_runner.get_run(run_id)
        _require(stored is not None, "execution run does not exist")
        record = stored.to_json()
    closed = allow_closed and record.get("status") == "failed" and (record.get("result") or {}).get("no_task_claim") is True
    _require(record.get("run_id") == run_id and record.get("key") == f"plan:{plan_id}:execute"
             and (record.get("status") in {"unknown", "reconciling"} or closed), "run is not this plan's unresolved execution")
    context = record.get("context") or {}
    task_id = context.get("task_id")
    _require(context.get("plan_id") == plan_id and context.get("detached_execution") is True
             and isinstance(task_id, str) and _IDENTIFIER.fullmatch(task_id), "invalid persisted execution context")
    host = planctl._resolve_node_host(str(context.get("node") or ""))
    _require(all(host.get(field) == context.get(field) for field in ("ssh_alias", "remote_root"))
             and host.get("id") == context.get("host_id"), "host configuration changed since the rejected launch")
    path = context.get("remote_run_dir") or ""
    prefix = planctl._remote_run_dir(host, plan_id, task_id) + "/"
    _require(isinstance(path, str) and path.startswith(prefix)
             and _REMOTE_RUN.fullmatch(path[len(prefix):]), "invalid persisted remote launch path")
    plan = planctl.status(plan_id)
    _require((plan.get("state") == "running" or closed) and plan.get("procedure", {}).get("adapter") == "database_single_instance_opatch",
             "lock recovery requires a running standalone database plan")
    _require(planctl._read_sealed_actor(plan_id, "authorization.json") == actor,
             "actor must match the plan's execution authorizer")
    task = planctl._run(["task-status", "--plan-id", plan_id, "--task-id", task_id])
    _require((task.get("status") == "pending" or closed) and task.get("stage") == "validate", "the rejected validation task must still be pending")
    return {"plan_id": plan_id, "actor": actor, "task_id": task_id, "run_id": path[len(prefix):],
            "app_run_id": run_id, "plan_sha256": plan["plan_sha256"], "host": host, "original": record}


def _argv(scope, operation):
    host = scope["host"]
    return ["/usr/bin/env", f"OPU_PLAN_STATE_DIR={planctl._remote_plan_root(host)}",
            host["remote_root"] + "/bin/opu-database-lock-recover", operation,
            "--plan-id", scope["plan_id"], "--task-id", scope["task_id"],
            "--run-id", scope["run_id"], "--actor", scope["actor"]]


def _verify_report(report, scope, *, completed=False):
    _require(isinstance(report, dict), "native lock report must be an object")
    body = {key: value for key, value in report.items() if key != "record_sha256"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    _require(report.get("record_sha256") == digest, "native lock report integrity failed")
    _require(report.get("status") in {"eligible", "blocked", "recovery_required", "completed"}, "unknown native lock report status")
    _require(all(report.get(key) == scope[key] for key in ("plan_id", "task_id", "run_id", "actor")),
             "native lock report belongs to another execution")
    if report.get("status") == "eligible" or completed:
        _require(report.get("plan_sha256") == scope["plan_sha256"], "native lock report plan seal differs")
        _require(report.get("wrapper", {}).get("exit_code") == 75, "original wrapper was not a verified host-lock rejection")
    if completed:
        health = report.get("service_health") or {}
        _require(report.get("status") == "completed" and report.get("original_task_status") == "pending"
                 and report.get("plan_and_task_unchanged") is True and report.get("lock_inode_preserved") is True
                 and report.get("lock_released") is True and not report.get("blockers"), "native recovery has no verified successful outcome")
        _require(health.get("instance_status") == "OPEN" and health.get("database_role") == "PRIMARY"
                 and health.get("open_mode") == "READ WRITE" and health.get("listener_ready") is True,
                 "database and listener health were not verified after recovery")
        _require(health.get("dbid") and health.get("dbid") == report.get("health_before", {}).get("dbid")
                 and report.get("lock", {}).get("safe") is True and len(report.get("lock", {}).get("identity", [])) == 2,
                 "database identity or original lock identity is unverified")
        lock = report["lock"]
        _require(lock.get("identity_after") == lock["identity"] and lock.get("inode_preserved") is True
                 and lock.get("held_after") is False and lock.get("inherited_holders_after") == [],
                 "native lock release and inode preservation proof is incomplete")
    return report


def inspect(plan_id, actor, run_id=None):
    scope = _scope(plan_id, actor, run_id)
    host = scope["host"]
    tools_sync.ensure_host_tools(host)
    response = remote.run_remote_raw(host["ssh_alias"], _argv(scope, "inspect"), timeout=240, sudo=bool(host.get("sudo")))
    _require(response.returncode in (0, 65) and response.stdout.strip(),
             "native lock inspection could not complete: " + planctl._redacted_diagnostic(response.stderr))
    report = _verify_report(json.loads(response.stdout), scope)
    _require((response.returncode == 0) == (report.get("status") == "eligible"), "inspection status contradicts native exit")
    return {**report, "execution_run_id": scope["app_run_id"]}


def _closed_by_recovery(record, scope, outcome, maintenance_run_id):
    """Accept only this recovery's verified closure of the rejected launch."""
    if not isinstance(record, dict):
        return False
    result, context, error = record.get("result"), record.get("context"), record.get("error") or {}
    if not isinstance(result, dict) or not isinstance(context, dict) or not isinstance(error, dict):
        return False
    if (record.get("run_id") != scope["app_run_id"] or record.get("key") != f"plan:{scope['plan_id']}:execute"
            or record.get("status") != "failed" or result.get("no_task_claim") is not True
            or context.get("lock_recovery_run_id") != maintenance_run_id):
        return False
    report, diagnostic = result.get("lock_recovery"), error.get("result")
    if (report is not None and not isinstance(report, dict)) or (diagnostic is not None and not isinstance(diagnostic, dict)):
        return False
    hashes = [value for value in ((report or {}).get("record_sha256"),
                                  (diagnostic or {}).get("lock_recovery_sha256")) if value is not None]
    # Either persisted form is sufficient; conflicting evidence is not.
    return bool(hashes) and all(value == outcome["record_sha256"] for value in hashes)


def recover(plan_id, actor, run_id=None, *, maintenance_run_id):
    scope = _scope(plan_id, actor, run_id)
    _require(isinstance(maintenance_run_id, str) and _APP_RUN.fullmatch(maintenance_run_id), "managed maintenance run is required")
    current = pipeline_runner.get_run(maintenance_run_id)
    _require(current is not None and current.key == f"plan:{plan_id}:lock-recovery" and current.status == "running",
             "maintenance run ownership is missing")
    original = pipeline_runner.get_run(scope["app_run_id"])
    with original._lock:
        _require(not original.context.get("lock_recovery_run_id"), "a lock recovery was already submitted; inspect its existing outcome")
    # Synchronization cannot launch recovery. Keep failures here retryable;
    # reserve the original launch only immediately before a possible worker.
    tools_sync.ensure_host_tools(scope["host"])
    scope = _scope(plan_id, actor, run_id)
    current = pipeline_runner.get_run(maintenance_run_id)
    _require(current is not None and current.key == f"plan:{plan_id}:lock-recovery" and current.status == "running",
             "maintenance run ownership changed during tool synchronization")
    original = pipeline_runner.get_run(scope["app_run_id"])
    with original._lock:
        _require(not original.context.get("lock_recovery_run_id"), "a lock recovery was already submitted; inspect its existing outcome")
        original.context["lock_recovery_run_id"] = maintenance_run_id
        original._persist()
    host = scope["host"]
    pipeline_runner.set_execution_context(lock_recovery=True, actor=actor, original_run_id=scope["app_run_id"],
                                          original_task_id=scope["task_id"], original_remote_run_id=scope["run_id"],
                                          original_plan_sha256=scope["plan_sha256"])
    rc, stdout, stderr = planctl._run_detached_remote(host, plan_id, "lock-recover-" + scope["task_id"], _argv(scope, "recover"))
    if rc != 0:
        try:
            report = _verify_report(json.loads(stdout), scope)
        except (LockError, ValueError, TypeError):
            report = None
        detail = "; ".join(report.get("blockers") or []) if report else stderr
        raise LockError("managed lock recovery needs inspection", stderr=planctl._redacted_diagnostic(detail), result=report)
    outcome = _completed_audit(scope)
    pipeline_runner.set_execution_context(detached_terminal=True)
    original = pipeline_runner.get_run(scope["app_run_id"])
    original_outcome = original.to_json() if original else None
    if not _closed_by_recovery(original_outcome, scope, outcome, maintenance_run_id):
        try:
            original_outcome = pipeline_runner.reconcile_run(scope["app_run_id"], actor=actor, inspect=reconcile_execution_run,
                                                            note="Verified managed recovery of the inherited service lock; rejected task remains pending")
        except pipeline_runner.RunConflict:
            # The UI can reconcile between our read and the registry lock.
            # Re-read its result; a conflict is never proof of completion.
            original = pipeline_runner.get_run(scope["app_run_id"])
            original_outcome = original.to_json() if original else None
    if not _closed_by_recovery(original_outcome, scope, outcome, maintenance_run_id):
        raise LockError("Host recovery completed, but the original launch still needs reconciliation. Inspect its existing result before continuing.",
                        result={"lock_recovery": outcome, "execution_run_id": scope["app_run_id"]})
    return {**outcome, "execution_run_id": scope["app_run_id"], "maintenance_run_id": maintenance_run_id}


def _completed_audit(scope):
    path = f"{_AUDIT_ROOT}/{scope['plan_id']}/{scope['run_id']}/result.json"
    host = scope["host"]
    data = remote.pull_file(host["ssh_alias"], path, timeout=60, sudo=bool(host.get("sudo")))
    return _verify_report(json.loads(data), scope, completed=True)


def _maintenance_outcome(record):
    context = record.get("context") or {}
    _require(context.get("lock_recovery") is True, "not a managed lock recovery")
    scope = _scope(context.get("plan_id"), context.get("actor"), context.get("original_run_id"), allow_closed=True)
    _require(record.get("key") == f"plan:{scope['plan_id']}:lock-recovery"
             and scope["original"].get("context", {}).get("lock_recovery_run_id") == record.get("run_id")
             and context.get("original_task_id") == scope["task_id"]
             and context.get("original_remote_run_id") == scope["run_id"]
             and context.get("original_plan_sha256") == scope["plan_sha256"], "maintenance scope differs from original execution")
    host = scope["host"]
    _require(all(context.get(key) == host.get(key) for key in ("ssh_alias", "remote_root"))
             and context.get("host_id") == host.get("id"), "maintenance host changed")
    task = "lock-recover-" + scope["task_id"]
    prefix = planctl._remote_run_dir(host, scope["plan_id"], task) + "/"
    path = context.get("remote_run_dir") or ""
    _require(context.get("task_id") == task and isinstance(path, str) and path.startswith(prefix)
             and _REMOTE_RUN.fullmatch(path[len(prefix):]), "invalid maintenance launch path")
    response = remote.run_remote_raw(host["ssh_alias"], ["cat", path + "/rc"], timeout=60, sudo=bool(host.get("sudo")))
    _require(response.returncode == 0 and response.stdout.strip() == "0", "maintenance wrapper has no verified successful exit; do not retry")
    return scope, _completed_audit(scope)


def reconcile_detached_run(record):
    try:
        _require(record.get("reconciliation_actor") == record.get("context", {}).get("actor"),
                 "reconciliation actor differs from sealed recovery actor")
        _scope, report = _maintenance_outcome(record)
        return {"status": "succeeded", "result": report}
    except (LockError, remote.RemoteError, ValueError, OSError, TypeError) as exc:
        return {"status": "unknown", "error": {"message": str(exc)}}


def reconcile_execution_run(record):
    context = record.get("context") or {}
    maintenance_id = context.get("lock_recovery_run_id")
    if not maintenance_id:
        return planctl.reconcile_detached_run(record)
    try:
        _require(isinstance(maintenance_id, str) and _APP_RUN.fullmatch(maintenance_id), "invalid maintenance run reference")
        maintenance = pipeline_runner.get_run(maintenance_id)
        _require(maintenance is not None, "maintenance run record is missing")
        _require(record.get("reconciliation_actor") == maintenance.context.get("actor"), "reconciliation actor differs from sealed recovery actor")
        scope, report = _maintenance_outcome(maintenance.to_json())
        _require(scope["app_run_id"] == record.get("run_id"), "maintenance belongs to another application run")
        return {"status": "failed", "error": {"message": "Original launch exited before claiming validation; managed lock recovery completed. Validation remains pending.",
                                              "result": {"exit_code": 75, "task_status": "pending", "lock_recovery_sha256": report["record_sha256"]}},
                "result": {"reconciled": True, "no_task_claim": True, "lock_recovery": report}}
    except (LockError, remote.RemoteError, ValueError, OSError, TypeError) as exc:
        return {"status": "unknown", "error": {"message": str(exc)}}
