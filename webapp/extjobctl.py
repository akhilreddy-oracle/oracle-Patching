"""Read-only extjob inspection against a controller-registered backup reference.

The managed run reserves the plan's execution key while tools are synchronized
and native evidence is collected. Neither a matching report nor this reference
authorizes installing bytes or changing executable ownership/permissions.
"""
from __future__ import annotations

import hashlib
import json
import os
import runtime_paths
from pathlib import Path, PurePosixPath
import re
import stat

import pipeline_runner
import planctl
import remote
import tools_sync

ExtjobError = planctl.PlanError
REFERENCE_DIR = runtime_paths.state_dir() / "extjob-references"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_RUN_ID = re.compile(r"[a-f0-9]{12}\Z")
_FINAL_TASK = "005-final-validate-local"


def _require(condition, message):
    if not condition:
        raise ExtjobError(message)


def validate_input(plan_id, actor):
    for name, value in (("plan_id", plan_id), ("actor", actor)):
        _require(isinstance(value, str) and _IDENTIFIER.fullmatch(value), f"invalid {name}")


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate JSON field")
            result[key] = value
        return result

    def invalid(_value):
        raise ExtjobError("non-finite JSON value")

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError) as exc:
        raise ExtjobError("invalid inspection JSON") from exc
    _require(isinstance(result, dict), "inspection JSON must be an object")
    return result


def _digest(value):
    return hashlib.sha256(json.dumps({k: v for k, v in value.items() if k != "record_sha256"},
                                    sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _stable(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _protected(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (kind(info.st_mode) and info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022
            and (directory or info.st_nlink == 1))


def _reference(plan_id):
    """The API cannot choose this file or register/replace its contents."""
    directory = fd = None
    try:
        directory = os.open(REFERENCE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before_dir = os.fstat(directory)
        _require(_protected(before_dir, directory=True), "reference directory is not controller-protected")
        name = plan_id + ".json"
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        _require(_protected(before) and before.st_size <= 65536, "reference must be a protected regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(65537)
        _require(len(data) <= 65536 and _stable(before) == _stable(os.fstat(fd))
                 == _stable(os.stat(name, dir_fd=directory, follow_symlinks=False)), "reference changed while reading")
        _require(_stable(before_dir) == _stable(os.stat(REFERENCE_DIR, follow_symlinks=False)),
                 "reference directory changed while reading")
        reference = _json(data)
    except OSError as exc:
        raise ExtjobError("controller-registered extjob reference is unavailable") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)
    _require(set(reference) == {"plan_sha256", "database", "archive_path", "archive_sha256"},
             "reference has unexpected or missing fields")
    for field in ("plan_sha256", "archive_sha256"):
        _require(isinstance(reference[field], str) and _SHA256.fullmatch(reference[field]), "invalid reference SHA256")
    return reference


def _owner(plan_id, inspection_run_id):
    _require(isinstance(inspection_run_id, str) and _RUN_ID.fullmatch(inspection_run_id), "managed inspection run is required")
    key = f"plan:{plan_id}:execute"
    record = pipeline_runner.get_run(inspection_run_id)
    _require(record is not None, "managed inspection run is missing")
    data = record.to_json()
    _require(data.get("run_id") == inspection_run_id and data.get("kind") == "extjob_inspect"
             and data.get("key") == key and data.get("status") == "running"
             and not data.get("context", {}).get("detached_execution")
             and pipeline_runner.active_run_id(key) == inspection_run_id,
             "inspection does not own this plan's execution reservation")


def _scope(plan_id, actor, inspection_run_id):
    validate_input(plan_id, actor)
    _owner(plan_id, inspection_run_id)
    plan = planctl._run(["status", "--plan-id", plan_id])
    _require(isinstance(plan, dict) and plan.get("plan_id") == plan_id and plan.get("state") in {"running", "paused"}
             and plan.get("intent") == "patch_apply"
             and plan.get("procedure", {}).get("adapter") == "database_single_instance_opatch",
             "inspection requires a sealed running or paused standalone apply plan")
    plan_sha = plan.get("plan_sha256")
    _require(isinstance(plan_sha, str) and _SHA256.fullmatch(plan_sha), "plan seal is missing")
    nodes, target = plan.get("nodes"), plan.get("target")
    _require(isinstance(nodes, list) and len(nodes) == 1 and isinstance(nodes[0], str)
             and _IDENTIFIER.fullmatch(nodes[0]) and isinstance(target, dict), "plan must have one sealed target node")
    for field in ("database_unique_name", "owner"):
        _require(isinstance(target.get(field), str) and _IDENTIFIER.fullmatch(target[field]), "invalid sealed target identifier")
    home = target.get("oracle_home")
    _require(isinstance(home, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", home)
             and ".." not in PurePosixPath(home).parts and str(PurePosixPath(home)) == home, "invalid sealed Oracle home")
    try:
        authorization_bytes = (planctl.PLAN_STATE_DIR / "plans" / plan_id / "authorization.json").read_bytes()
    except OSError as exc:
        raise ExtjobError("sealed execution authorization is unavailable") from exc
    authorization = _json(authorization_bytes)
    _require(authorization.get("record_sha256") == _digest(authorization)
             and authorization.get("plan_id") == plan_id and authorization.get("plan_sha256") == plan_sha
             and authorization.get("decision") == "execution_authorized" and authorization.get("actor") == actor,
             "actor must match the sealed plan authorizer")
    apply_task = "002-apply-" + nodes[0]
    final_status = "failed" if plan["state"] == "paused" else "pending"
    expected_tasks = (("001-precheck-" + nodes[0], "precheck", "succeeded"),
                      (apply_task, "apply", "succeeded"), ("003-validate-" + nodes[0], "validate", "succeeded"),
                      ("004-datapatch-local", "datapatch", "succeeded"), (_FINAL_TASK, "final_validate", final_status))
    task_dir = planctl.PLAN_STATE_DIR / "plans" / plan_id / "tasks"
    _require(not task_dir.is_symlink() and {path.stem for path in task_dir.glob("*.json")} == {task[0] for task in expected_tasks},
             "inspection requires exactly the standalone task chain with no unresolved extra work")
    tasks = {}
    for task_id, stage, status in expected_tasks:
        task = planctl._run(["task-status", "--plan-id", plan_id, "--task-id", task_id])
        _require(isinstance(task, dict) and task.get("plan_id") == plan_id and task.get("plan_sha256") == plan_sha
                 and task.get("task_id") == task_id and task.get("stage") == stage and task.get("status") == status,
                 "inspection requires successful preceding tasks and pending/running-plan or failed/paused-plan final validation")
        tasks[task_id] = task
    apply_sha = tasks[apply_task].get("evidence_sha256")
    _require(isinstance(apply_sha, str) and _SHA256.fullmatch(apply_sha), "native apply evidence seal is missing")
    sql_sha = tasks["004-datapatch-local"].get("evidence_sha256")
    _require(isinstance(sql_sha, str) and _SHA256.fullmatch(sql_sha), "datapatch evidence seal is missing")
    final_result_sha = tasks[_FINAL_TASK].get("task_result_sha256") if final_status == "failed" else None
    _require(final_status != "failed" or isinstance(final_result_sha, str) and _SHA256.fullmatch(final_result_sha),
             "failed final validation result seal is missing")
    reference = _reference(plan_id)
    _require(reference["plan_sha256"] == plan_sha and reference["database"] == target["database_unique_name"],
             "registered reference differs from the sealed plan target")
    archive = reference["archive_path"]
    _require(isinstance(archive, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", archive)
             and ".." not in PurePosixPath(archive).parts and str(PurePosixPath(archive)) == archive,
             "invalid registered archive path")
    prefix = PurePosixPath("/u02/opu-backup") / target["database_unique_name"]
    relative = PurePosixPath(archive).parts[len(prefix.parts):]
    _require(PurePosixPath(archive).is_relative_to(prefix) and len(relative) >= 3
             and relative[-2:] == ("oracle-home", PurePosixPath(home).name + ".tar.gz"),
             "registered archive is outside the fixed target home backup layout")
    return {"plan_id": plan_id, "actor": actor, "plan_sha256": plan_sha, "target": target, "reference": reference,
            "host": planctl._resolve_node_host(nodes[0]), "apply_sha256": apply_sha,
            "datapatch_sha256": sql_sha, "plan_state": plan["state"], "final_task_status": final_status,
            "final_task_result_sha256": final_result_sha,
            "authorization_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
            "authorization_record_sha256": authorization["record_sha256"]}


def _file_proof(value, path, *, current=False):
    _require(isinstance(value, dict) and value.get("path" if current else "name") == path
             and isinstance(value.get("sha256"), str) and _SHA256.fullmatch(value["sha256"])
             and type(value.get("size")) is int and 0 < value["size"] <= 16 * 1024 * 1024
             and all(type(value.get(field)) is int and value[field] >= 0 for field in ("uid", "gid"))
             and isinstance(value.get("mode"), str) and re.fullmatch(r"[0-7]{4}", value["mode"])
             and value.get("regular") is True, "native extjob file proof is incomplete")
    if current:
        _require(value.get("same_open_descriptor_verified") is True and value.get("nlink") == 1
                 and isinstance(value.get("identity"), list) and len(value["identity"]) == 2
                 and all(type(v) is int and v >= 0 for v in value["identity"]), "current extjob identity is unverified")
    else:
        _require(value.get("unique") is True, "archive extjob member is not unique")


def _verify_report(report, scope, returncode):
    _require(isinstance(report, dict) and report.get("record_sha256") == _digest(report), "native extjob report integrity failed")
    inspected = report.get("status") == "inspected"
    _require((returncode == 0 and inspected) or (returncode == 65 and report.get("status") == "blocked"),
             "native inspection status contradicts exit code")
    reference = scope["reference"]
    _require(report.get("schema_version") == "1.0" and report.get("read_only") is True
             and report.get("mutation_authorized") is False and report.get("plan_id") == scope["plan_id"]
             and report.get("actor") == scope["actor"]
             and report.get("requested_archive") == {"path": reference["archive_path"], "sha256": reference["archive_sha256"]},
             "native inspection request scope differs")
    # Early native refusals may lack target/authority proof. They are diagnostics
    # only; every field actually returned must still bind to this request.
    for field in ("plan_sha256", "target", "authority", "archive", "current"):
        _require(not inspected or field in report, "native inspection scope proof is incomplete")
    if "plan_sha256" in report:
        _require(report["plan_sha256"] == scope["plan_sha256"], "native plan seal differs")
    if "target" in report:
        target = report["target"]
        _require(isinstance(target, dict) and all(target.get(k) == scope["target"].get(k)
                 for k in ("database_unique_name", "oracle_home", "owner")), "native target differs")
        for field in ("oracle_sid", "listener"):
            if field in scope["target"]:
                _require(target.get(field) == scope["target"][field], "native target service differs")
    if "authority" in report:
        authority = report["authority"]
        _require(isinstance(authority, dict) and authority.get("final_task_id") == _FINAL_TASK
                 and authority.get("native_apply_evidence_sha256") == scope["apply_sha256"]
                 and authority.get("native_datapatch_evidence_sha256") == scope["datapatch_sha256"]
                 and all(authority.get(k) == scope[k] for k in ("plan_state", "final_task_status", "final_task_result_sha256"))
                 and all(authority.get(k) == scope[k] for k in ("authorization_sha256", "authorization_record_sha256"))
                 and isinstance(authority.get("native_target_binding_record_sha256"), str)
                 and _SHA256.fullmatch(authority["native_target_binding_record_sha256"]), "native authorization proof differs")
    if "archive" in report:
        archive = report["archive"]
        _require(isinstance(archive, dict) and archive.get("path") == reference["archive_path"]
                 and archive.get("expected_sha256") == reference["archive_sha256"]
                 and ("sha256" not in archive or archive["sha256"] == reference["archive_sha256"]), "native archive binding differs")
    if "current" in report:
        _file_proof(report["current"], scope["target"]["oracle_home"] + "/bin/extjob", current=True)
    if inspected:
        _require(report.get("collector") == {"name": "oracle.extjob.provenance", "version": "1"}
                 and report.get("reference_kind") == "retained_backup_audit_digest", "unknown provenance collector")
        archive = report["archive"]
        _require(archive.get("verified") is True and archive.get("sha256") == reference["archive_sha256"]
                 and archive.get("same_open_descriptor_verified") is True, "archive verification is incomplete")
        member, current = report.get("member"), report["current"]
        _file_proof(member, PurePosixPath(scope["target"]["oracle_home"]).name + "/bin/extjob")
        _require(report.get("bytes_match") is (member["sha256"] == current["sha256"] and member["size"] == current["size"])
                 and report.get("reference_root_setuid") is (member["uid"] == 0 and member["mode"] == "4750"),
                 "native provenance conclusions contradict evidence")
    else:
        _require(isinstance(report.get("error"), str) and report["error"]
                 and report.get("bytes_match") is None and report.get("reference_root_setuid") is None
                 and ("archive" not in report or report["archive"].get("verified") is False),
                 "blocked inspection must not claim verified provenance")
    return report


def inspect(plan_id, actor, *, inspection_run_id):
    scope = _scope(plan_id, actor, inspection_run_id)
    host, reference = scope["host"], scope["reference"]
    runtimes = tools_sync.ensure_host_tools(host)
    _require(_scope(plan_id, actor, inspection_run_id) == scope, "inspection scope changed during tool synchronization")
    argv = ["/usr/bin/env", f"OPU_PLAN_STATE_DIR={planctl._remote_plan_root(host)}",
            tools_sync.tool_path(host, runtimes, "bin/opu-extjob-provenance-inspect"),
            "--plan-id", plan_id, "--actor", actor,
            "--archive", reference["archive_path"], "--archive-sha256", reference["archive_sha256"]]
    response = remote.run_remote_raw(host["ssh_alias"], argv, timeout=900, sudo=bool(host.get("sudo")))
    _require(response.returncode in (0, 65) and response.stdout.strip() and len(response.stdout) <= 1024 * 1024,
             "native extjob inspection could not complete: " + planctl._redacted_diagnostic(response.stderr))
    report = _verify_report(_json(response.stdout), scope, response.returncode)
    _require(_scope(plan_id, actor, inspection_run_id) == scope, "inspection scope changed during native collection")
    return report
