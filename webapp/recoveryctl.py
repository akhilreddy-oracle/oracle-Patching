"""Native recovery preparation on configured hosts, plus isolated demo fixtures.

Live requests retain immutable inputs and sealed evidence on the managed host.
The local store contains transport metadata and byte-for-byte evidence copies;
it never changes the native request, approvals, or recovery records.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import runtime_paths
import re
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import recovery_fixtures
import evidence
import pipeline_runner
import production
import remote
import tools_sync
from durable import file_lock, write_json

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "bin" / "opu-database-recovery-prepare"
# Must live under /tmp, /private/tmp, /var/folders, or /private/var/folders —
# opu-database-recovery-prepare's constrained_test_mode() (a real safety
# belt, not a convenience check) refuses to activate TEST_MODE for any
# OPU_RECOVERY_PREP_STATE_DIR outside those prefixes, precisely so a fixture
# path can never look like a real deployment location. Confirmed by hitting
# this directly: bin/opu-database-recovery-prepare:33-39.
RECOVERY_DIR = Path("/tmp/opu-webapp-recovery-fixtures")
LIVE_DIR = runtime_paths.state_dir() / "recovery-live"
HOSTS_FILE = runtime_paths.hosts_file()
REMOTE_STATE_DIR = "/var/lib/oracle-patching-utility/recovery-preparations"
LIVE_EXECUTE_TIMEOUT_SECONDS = 86400
LIVE_POLL_INTERVAL_SECONDS = 5
LIVE_POLL_SSH_TIMEOUT_SECONDS = 30
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
EXECUTE_TIMEOUT_SECONDS = 300  # execute does real work even against the fixture
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SUPPORTED_ADAPTER = "standalone_primary_noarchivelog_spfile"
RESTORE_VALIDATION = "RMAN RESTORE DATABASE VALIDATE checks backup readability; it does not restore a separate database"


def capability() -> dict:
    """Describe implementation availability; target admission remains native."""
    result = {"supported_adapter": SUPPORTED_ADAPTER,
              "validation_level": "fixture_tested", "restore_validation": RESTORE_VALIDATION}
    try:
        production.require_live_mutation_allowed()
    except production.ProductionError as exc:
        return {**result, "live_available": False, "live_reason": exc.message}
    if not TOOL.is_file():
        return {**result, "live_available": False, "live_reason": "Recovery preparation executable is missing"}
    return {**result, "live_available": True, "live_reason": None}


def _target_capability(snapshot, database, maximum_age, *, now=None, window_start=None) -> dict:
    """Screen saved observations for request creation, never authorize a backup.

    Discovery cannot prove SPFILE, backup capacity or current live identity.
    Those requirements remain unknown until the native analysis passes, and
    native execution probes again before stopping any services.
    """
    now = now or datetime.now(timezone.utc)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    requirements = []

    def check(key, label, observed, required, status, action, stage="discovery"):
        requirements.append({"id": key, "label": label, "observed": observed,
                             "required": required, "status": status, "next_action": action,
                             "stage": stage})

    collector = snapshot.get("collector")
    contract_ok = snapshot.get("schema_version") == "1.0" and isinstance(collector, dict) and collector.get("name") == "oracle.topology.discover"
    check("discovery", "Discovery contract", "Oracle topology v1" if contract_ok else "Missing or invalid",
          "Oracle topology v1", "passed" if contract_ok else "unknown", "Refresh Discover for this host")
    cluster = snapshot.get("cluster") if isinstance(snapshot.get("cluster"), dict) else {}
    nodes = cluster.get("nodes")
    standalone = cluster.get("status") == "unavailable" and isinstance(nodes, list) and not nodes and cluster.get("grid_home") in (None, "")
    topology_known = isinstance(nodes, list) and isinstance(cluster.get("status"), str) and cluster.get("status") in {"unavailable", "detected", "complete", "collected"}
    check("topology", "Topology", f"CRS: {cluster.get('status') or 'unknown'}; nodes: {len(nodes) if isinstance(nodes, list) else 'unknown'}",
          "Standalone, no Grid home or cluster nodes", "passed" if standalone else "blocked" if topology_known else "unknown",
          "Use a supported standalone database; clustered recovery preparation is unavailable" if topology_known and not standalone else "Refresh Discover for this host")
    databases = snapshot.get("databases") if isinstance(snapshot.get("databases"), list) else []
    matches = [db for db in databases if isinstance(db, dict) and db.get("db_unique_name") == database and isinstance(database, str)]
    unique = len(matches) == 1 and bool(_ID_RE.fullmatch(database or ""))
    check("database", "Database identity", database or "Not discovered", "One unambiguous discovered database",
          "passed" if unique else "unknown", "Refresh Discover and select one database")
    selected = matches[0] if unique else {}
    runtime = selected.get("runtime") if isinstance(selected.get("runtime"), dict) else {}
    for key, label, expected in (("status", "Runtime evidence", "complete"), ("database_role", "Database role", "PRIMARY"),
                                  ("open_mode", "Open mode", "READ WRITE"), ("instance_state", "Instance state", "OPEN"),
                                  ("log_mode", "Log mode", "NOARCHIVELOG"), ("cdb", "CDB status", "NO")):
        actual = runtime.get(key)
        known = isinstance(actual, str) and bool(actual.strip()) and actual.lower() not in {"unknown", "unavailable", "uncollected"}
        action = "Refresh Discover for this database"
        if known and actual != expected:
            action = ("Select a NOARCHIVELOG test database; this adapter does not support ARCHIVELOG backup preparation"
                      if key == "log_mode" else
                      "Select a non-CDB database; multitenant recovery preparation is not supported"
                      if key == "cdb" else "Review the database state with its operator, then refresh Discover")
        check(key, label, actual if known else "Unknown", expected,
              "passed" if actual == expected else "blocked" if known else "unknown", action)
    home = selected.get("oracle_home")
    homes = snapshot.get("oracle_homes") if isinstance(snapshot.get("oracle_homes"), list) else []
    owners = [item.get("owner") for item in homes if isinstance(item, dict) and item.get("path") == home]
    try:
        _absolute_remote_path(home, "discovered Oracle home")
        _identifier(runtime.get("instance"), "Oracle SID")
        if len(owners) != 1:
            raise RecoveryError("Ambiguous owner")
        _identifier(owners[0], "Oracle home owner")
        target_ok = True
    except RecoveryError:
        target_ok = False
    check("oracle_target", "Oracle home, owner and SID", f"{home or 'unknown'}; {owners[0] if len(owners) == 1 else 'unknown'}; {runtime.get('instance') or 'unknown'}",
          "One valid home, owner and running SID", "passed" if target_ok else "unknown", "Refresh Discover and resolve the missing Oracle target identity")
    collected_at, age = snapshot.get("collected_at"), None
    freshness_status = "unknown"
    valid_limit = type(maximum_age) is int and 60 <= maximum_age <= 86400
    try:
        collected = datetime.strptime(collected_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (now - collected).total_seconds()
        start_age = (datetime.strptime(window_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - collected).total_seconds() if window_start else age
        if valid_limit:
            freshness_status = "passed" if 0 <= age <= maximum_age and start_age <= maximum_age else "blocked"
    except (TypeError, ValueError):
        pass
    check("freshness", "Discovery freshness", f"{age:.1f} seconds old ({collected_at})" if age is not None else "Unknown collection time",
          f"0–{maximum_age} seconds, fresh through window start" if valid_limit else "A policy age limit between 60 and 86400 seconds",
          freshness_status, "Refresh discovery close to the maintenance window using Discover" if valid_limit else "Correct the readiness policy snapshot age limit")
    check("spfile", "SPFILE", "Unknown: discovery does not collect SPFILE use", "Database is using an SPFILE", "unknown",
          "Create the request and run Analyze recovery; approval remains blocked until the native probe passes", "native_analysis")
    check("live_preflight", "Live identity, capacity and backup location", "Not analyzed", "Passed native recovery analysis", "unknown",
          "Run Analyze recovery before approval; execution repeats live checks", "native_analysis")
    blockers = [item for item in requirements if item["stage"] == "discovery" and item["status"] != "passed"]
    can_create = not blockers
    status = "needs_native_analysis" if can_create else "blocked" if any(item["status"] == "blocked" for item in blockers) else "unknown"
    return {"database": database, "status": status, "can_create": can_create, "supported_adapter": SUPPORTED_ADAPTER,
            "requirements": requirements, "blockers": blockers,
            "next_action": "Create a request for native analysis; this is not approval to execute" if can_create else blockers[0]["next_action"]}


def target_capabilities(host_id: str) -> dict:
    """Read-only, host-scoped creation guidance from cached discovery evidence."""
    evidence.validate_host_id(host_id)
    root = evidence.VAR_DIR.resolve()
    directory = (root / host_id / "evidence").resolve()
    if root not in directory.parents:
        raise evidence.EvidenceError("Host evidence directory escapes its configured root")

    def read(name):
        path = directory / f"{name}.json"
        try:
            return json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else None
        except (OSError, ValueError):
            return None

    snapshot, policy = read("snapshot"), read("policy")
    maximum_age = policy.get("maximum_snapshot_age_seconds") if isinstance(policy, dict) else 1800
    databases = snapshot.get("databases") if isinstance(snapshot, dict) else None
    names = sorted({item["db_unique_name"] for item in databases if isinstance(item, dict) and isinstance(item.get("db_unique_name"), str) and _ID_RE.fullmatch(item["db_unique_name"])}) if isinstance(databases, list) else []
    return {"target_capabilities": [_target_capability(snapshot, name, maximum_age) for name in names or [None]],
            "target_capability_context": {"host_id": host_id, "source": "saved_discovery", "maximum_snapshot_age_seconds": maximum_age,
                                          "policy_source": "saved_policy" if isinstance(policy, dict) else "default_policy",
                                          "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}


class RecoveryError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.error = "recovery_tool_failed"
        self.message = message
        self.stderr = stderr

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message, "stderr": self.stderr}


def _fixture_path(request_id: str) -> Path:
    if not isinstance(request_id, str) or not _ID_RE.fullmatch(request_id):
        raise RecoveryError("request_id contains unsupported characters")
    root = RECOVERY_DIR.resolve()
    candidate = RECOVERY_DIR / request_id
    if candidate.is_symlink() or candidate.resolve().parent != root:
        raise RecoveryError("request_id escapes the recovery fixture root")
    return candidate


def _env_for(request_id: str) -> dict:
    fixture_dir = _fixture_path(request_id)
    env = os.environ.copy()
    env.update(recovery_fixtures.env_for(fixture_dir))
    return env


def _run(request_id: str, args: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict | None:
    if args and args[0] not in {"status", "list", "report"}:
        runtime_paths.require_fixtures_allowed()
    argv = [str(TOOL), *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_env_for(request_id))
    except subprocess.TimeoutExpired:
        raise RecoveryError(f"opu-database-recovery-prepare timed out after {timeout}s") from None

    # Several subcommands (analyze, and rejections like self-approval) exit
    # nonzero with a real, structured JSON result rather than a crash — only
    # treat this as a failure when there's genuinely no stdout to parse.
    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RecoveryError(f"opu-database-recovery-prepare produced unparsable output: {exc}", stderr=result.stdout[-2000:]) from exc

    if result.returncode == 0:
        return None

    raise RecoveryError(f"opu-database-recovery-prepare exited {result.returncode} with no output", stderr=result.stderr.strip())


def create_testmode_demo(request_id: str, requester: str, *, host_id: str | None = None) -> dict:
    fixture_dir = _fixture_path(request_id)
    if not isinstance(requester, str) or not _ID_RE.fullmatch(requester):
        raise RecoveryError("requester contains unsupported characters")
    if host_id is not None:
        evidence.validate_host_id(host_id)
    if _live_path(request_id).exists():
        raise RecoveryError("A live recovery request already uses this request_id")
    runtime_paths.require_fixtures_allowed()
    fx = recovery_fixtures.build(fixture_dir, request_id)
    (fixture_dir / "webapp-metadata.json").write_text(json.dumps({"host_id": host_id}) + "\n")
    args = [
        "create", "--request-id", request_id, "--requester", requester,
        "--snapshot", str(fx["snapshot"]), "--policy", str(fx["policy"]),
        "--database", "ORCL", "--backup-parent", str(fx["backup_parent"]),
        "--window-start", fx["window_start"], "--window-end", fx["window_end"],
    ]
    result = _run(request_id, args)
    return {**(result or {}), "host_id": host_id}


def analyze(request_id: str) -> dict:
    if _is_live(request_id):
        return _live_action(request_id, "analyze", [])
    fixture_dir = _fixture_path(request_id)
    metadata_path = fixture_dir / "webapp-metadata.json"
    metadata = None
    if metadata_path.is_file() and not metadata_path.is_symlink():
        metadata = json.loads(metadata_path.read_text())
        write_json(metadata_path, {**metadata, "analysis": None})
    result = _run(request_id, ["analyze", "--request-id", request_id])
    if metadata is not None:
        write_json(metadata_path, {**metadata, "analysis": result})
    return result


def approve(request_id: str, actor: str, approval_ticket: str) -> dict:
    if _is_live(request_id):
        return _live_action(request_id, "approve", ["--actor", actor, "--approval-ticket", approval_ticket])
    return _run(request_id, ["approve", "--request-id", request_id, "--actor", actor, "--approval-ticket", approval_ticket])


def authorize(request_id: str, actor: str) -> dict:
    if _is_live(request_id):
        return _live_action(request_id, "authorize", ["--actor", actor])
    return _run(request_id, ["authorize", "--request-id", request_id, "--actor", actor])


def execute(request_id: str, actor: str) -> dict:
    if _is_live(request_id):
        return _live_action(request_id, "execute", ["--actor", actor])
    return _run(request_id, ["execute", "--request-id", request_id, "--actor", actor], timeout=EXECUTE_TIMEOUT_SECONDS)


def reconcile(request_id: str, actor: str) -> dict:
    if _is_live(request_id):
        return _live_action(request_id, "reconcile", ["--actor", actor])
    return _run(request_id, ["reconcile", "--request-id", request_id, "--actor", actor])


def status(request_id: str) -> dict:
    if _is_live(request_id):
        return _live_status(request_id)
    fixture_dir = _fixture_path(request_id)
    result = _run(request_id, ["status", "--request-id", request_id]) or {}
    host_id = None
    analysis = None
    metadata = fixture_dir / "webapp-metadata.json"
    if metadata.is_file() and not metadata.is_symlink():
        try:
            saved = json.loads(metadata.read_text())
            host_id = saved.get("host_id")
            if host_id is not None:
                evidence.validate_host_id(host_id)
            analysis = saved.get("analysis") if isinstance(saved.get("analysis"), dict) else None
        except (ValueError, TypeError, AttributeError, OSError):
            host_id = None
    approved_analysis = (result.get("approval") or {}).get("analysis")
    return {**result, "host_id": host_id, "mode": "test_mode",
            "analysis": approved_analysis if isinstance(approved_analysis, dict) else analysis}


def recovery_evidence_path(request_id: str) -> str | None:
    """Returns the completed request's recovery-evidence.json path, if any."""
    current = status(request_id)
    if current.get("mode") == "live":
        return (current.get("webapp_evidence") or {}).get("recovery_evidence", {}).get("path")
    return (current.get("result") or {}).get("recovery_evidence", {}).get("path")


def selection_status(request_id: str, *, host_id: str, host: dict) -> dict:
    """Admit only this host's verified live completion for fresh collection."""
    if not _is_live(request_id):
        raise RecoveryError("Only a completed live recovery request can supply patch evidence")
    metadata = _metadata(request_id)
    configured = _configured_host(metadata)
    if metadata["host_id"] != host_id or any(host.get(k) != configured.get(k) for k in ("id", "ssh_alias", "remote_root", "sudo")):
        raise RecoveryError("Recovery evidence belongs to a different configured host")
    current = status(request_id)
    execution = current.get("webapp_execution") or {}
    if current.get("state") != "completed" or execution.get("terminal") is not True or execution.get("exit_code") != 0:
        raise RecoveryError("Recovery needs a verified successful execution before selecting patch evidence")
    imported = current.get("webapp_evidence") or {}
    for kind in ("recovery_evidence", "post_snapshot", "reconciliation"):
        item = imported.get(kind) or {}
        native_binding = (current.get("result") or {}).get(kind) or {}
        if native_binding.get("sha256") != item.get("sha256") or native_binding.get("path") != item.get("remote_path"):
            raise RecoveryError("Imported recovery evidence differs from the current native completion")
        path = Path(item.get("path") or "")
        if (not path.is_file() or path.is_symlink() or path.parent.resolve() != (_live_path(request_id) / "evidence").resolve()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256")):
            raise RecoveryError("Imported recovery evidence is missing or changed; inspect the existing request")
    return current


def list_requests(host_id: str | None = None) -> list[dict]:
    if host_id is not None:
        evidence.validate_host_id(host_id)
    summaries = []
    for root in (RECOVERY_DIR, LIVE_DIR):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.is_symlink() or not _ID_RE.fullmatch(entry.name):
                continue
            # Filter by stored explicit attribution before any SSH call. Never
            # expose an unrelated host's unreadable request in a scoped list.
            try:
                metadata = _metadata(entry.name) if root == LIVE_DIR else json.loads((entry / "webapp-metadata.json").read_text())
                attributed_host = metadata.get("host_id")
            except (OSError, ValueError, RecoveryError):
                attributed_host = None
            if host_id is not None and attributed_host != host_id:
                continue
            try:
                summaries.append(status(entry.name))
            except (RecoveryError, remote.RemoteError) as exc:
                summaries.append({"request_id": entry.name, "host_id": attributed_host,
                                  "mode": "live" if root == LIVE_DIR else "test_mode",
                                  "state": "unreadable", "error": exc.to_json()})
    return summaries


def _identifier(value, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RecoveryError(f"{field} contains unsupported characters")
    return value


def _absolute_remote_path(value, field: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or value == "/"
            or str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts
            or not re.fullmatch(r"/[A-Za-z0-9._/+:-]+", value)):
        raise RecoveryError(f"{field} must be a normalized absolute path")
    return value


def _live_path(request_id: str) -> Path:
    _identifier(request_id, "request_id")
    candidate = LIVE_DIR / request_id
    if candidate.is_symlink() or candidate.resolve().parent != LIVE_DIR.resolve():
        raise RecoveryError("request_id escapes the live recovery root")
    return candidate


def _is_live(request_id: str) -> bool:
    return _live_path(request_id).exists()


def _metadata(request_id: str) -> dict:
    path = _live_path(request_id) / "metadata.json"
    try:
        if path.is_symlink():
            raise ValueError("symbolic link")
        result = json.loads(path.read_text())
        if not isinstance(result, dict) or result.get("request_id") != request_id or result.get("mode") != "live":
            raise ValueError("request identity mismatch")
        _identifier(result.get("host_id"), "host_id")
        return result
    except (OSError, ValueError) as exc:
        raise RecoveryError("Live recovery transport metadata is missing or invalid") from exc


def _update_metadata(request_id: str, **values) -> dict:
    base = _live_path(request_id)
    with file_lock(base / ".metadata.lock"):
        current = _metadata(request_id)
        current.update(values)
        write_json(base / "metadata.json", current)
    return current


def _configured_host(metadata: dict) -> dict:
    import host_config
    try:
        hosts = host_config.load(HOSTS_FILE).values()
        matches = [h for h in hosts if h.get("id") == metadata["host_id"]]
        if len(matches) != 1:
            raise ValueError("host not configured uniquely")
        host = matches[0]
        bound = metadata["host"]
        if any(host.get(k) != bound.get(k) for k in ("id", "ssh_alias", "remote_root", "sudo")):
            raise ValueError("host configuration changed")
        return host
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise RecoveryError("Configured recovery host changed or is unavailable; refusing to retarget the request") from exc


def _argv(host: dict, action: str, request_id: str, extra: list[str] | None = None) -> list[str]:
    # A clean remote environment cannot inherit fixture overrides, arbitrary
    # Oracle command substitutions, or a shell startup file from the webapp.
    root = _absolute_remote_path(host.get("remote_root"), "configured remote_root")
    return ["env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C",
            f"OPU_RECOVERY_PREP_STATE_DIR={REMOTE_STATE_DIR}",
            f"{root}/bin/opu-database-recovery-prepare", action, "--request-id", request_id, *(extra or [])]


def _parse_native(response, request_id: str) -> dict:
    try:
        payload = json.loads(response.stdout)
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            raise ValueError("request identity missing or mismatched")
    except (ValueError, TypeError) as exc:
        raise RecoveryError(f"Native recovery command exited {response.returncode} without a valid request result",
                            stderr=response.stderr.strip()[-4000:]) from exc
    if response.returncode and not (response.returncode == 2 and payload.get("status") in {"blocked", "in_progress"}):
        raise RecoveryError(f"Native recovery command exited {response.returncode}", stderr=response.stderr.strip()[-4000:])
    return payload


def _native(host: dict, request_id: str, action: str, extra: list[str] | None = None) -> dict:
    response = remote.run_remote_raw(host["ssh_alias"], _argv(host, action, request_id, extra),
                                     timeout=DEFAULT_TIMEOUT_SECONDS, sudo=True)
    result = _parse_native(response, request_id)
    # The native tool verifies its sealed request. Independently bind transport
    # responses to the controller's immutable inputs before exposing authority.
    if "state" in result:
        metadata = _metadata(request_id)
        if (result.get("source_snapshot") != metadata["inputs"]["snapshot"]
                or result.get("policy") != metadata["inputs"]["policy"]
                or result.get("target") != metadata.get("target")):
            raise RecoveryError("Native recovery result differs from the sealed host inputs or target")
    return result


def _decorate(result: dict, metadata: dict) -> dict:
    policy_path = _live_path(metadata["request_id"]) / "policy.json"
    if policy_path.is_symlink() or not policy_path.is_file():
        raise RecoveryError("Sealed recovery preparation policy is missing")
    policy_bytes = policy_path.read_bytes()
    if hashlib.sha256(policy_bytes).hexdigest() != metadata["inputs"]["policy"]["sha256"]:
        raise RecoveryError("Sealed recovery preparation policy changed")
    approved_analysis = (result.get("approval") or {}).get("analysis")
    return {**result, "host_id": metadata["host_id"], "mode": "live",
            "analysis": approved_analysis if isinstance(approved_analysis, dict) else metadata.get("analysis"), "webapp_execution": metadata.get("execution"),
            "webapp_evidence": metadata.get("evidence", {}),
            "webapp_reconciliation_execution": metadata.get("reconciliation_execution"),
            "preparation_policy": json.loads(policy_bytes)}


def _validate_policy(policy: dict) -> None:
    """Use the repository's full JSON Schema before crossing the SSH boundary."""
    interpreter = sys.executable if importlib.util.find_spec("jsonschema") else str(REPO_ROOT / ".venv" / "bin" / "python")
    if not Path(interpreter).is_file():
        raise RecoveryError("The contract-validation environment is required for live recovery policy validation")
    program = (
        "import json,sys; from jsonschema import Draft202012Validator; "
        "schema=json.load(open(sys.argv[1])); policy=json.load(sys.stdin); "
        "errors=list(Draft202012Validator(schema).iter_errors(policy)); "
        "print('\\n'.join(e.message for e in errors),file=sys.stderr); sys.exit(bool(errors))"
    )
    try:
        result = subprocess.run([interpreter, "-c", program,
            str(REPO_ROOT / "contracts" / "readiness" / "readiness-policy-v1.schema.json")],
            input=json.dumps(policy, allow_nan=False), capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("Could not validate the complete recovery readiness policy") from exc
    if result.returncode:
        raise RecoveryError("Recovery policy does not satisfy the readiness-policy contract", result.stderr.strip()[-4000:])
    if not 60 <= policy["maximum_snapshot_age_seconds"] <= 86400:
        raise RecoveryError("Recovery snapshot maximum age must be between 60 and 86400 seconds")
    listener_timeout = policy["database"].get("listener_registration_timeout_seconds", 120)
    if type(listener_timeout) is not int or not 10 <= listener_timeout <= 600:
        raise RecoveryError("Listener registration timeout must be an integer between 10 and 600 seconds")
    if (policy.get("recovery") or {}).get("require_backup") is not True:
        raise RecoveryError("Recovery preparation requires backup evidence; a waiver cannot authorize preparation")


def create_live(request_id: str, requester: str, *, host: dict, host_id: str,
                database: str, backup_parent: str, window_start: str, window_end: str,
                policy: dict | None = None) -> dict:
    """Seal a configured host's current snapshot and policy without downtime."""
    from datetime import datetime, timezone

    _identifier(request_id, "request_id")
    _identifier(requester, "requester")
    _identifier(host_id, "host_id")
    _identifier(database, "database")
    _absolute_remote_path(backup_parent, "backup_parent")
    if not isinstance(host, dict) or host.get("id") != host_id or not isinstance(host.get("ssh_alias"), str):
        raise RecoveryError("A configured recovery host is required")
    _absolute_remote_path(host.get("remote_root"), "configured remote_root")
    _configured_host({"host_id": host_id, "host": host})
    for value in (window_start, window_end):
        if not isinstance(value, str):
            raise RecoveryError("Maintenance window must use UTC timestamps")
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise RecoveryError("Maintenance window must use YYYY-MM-DDTHH:MM:SSZ") from exc
    if window_end <= window_start:
        raise RecoveryError("Maintenance window end must follow its start")
    if (datetime.strptime(window_end, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(window_start, "%Y-%m-%dT%H:%M:%SZ")).total_seconds() > 86400:
        raise RecoveryError("Recovery maintenance window cannot exceed 24 hours")
    snapshot_path = evidence.evidence_path(host_id, "snapshot")
    if not snapshot_path.is_file() or snapshot_path.is_symlink():
        raise RecoveryError("Run discovery for the selected host before creating recovery preparation")
    snapshot_bytes = snapshot_path.read_bytes()
    try:
        snapshot = json.loads(snapshot_bytes)
    except (ValueError, TypeError) as exc:
        raise RecoveryError("Selected discovery is invalid; refresh Discover for this host") from exc
    if policy is None:
        policy = evidence.read_evidence(host_id, "policy")
    if not isinstance(policy, dict) or policy.get("schema_version") != "1.0":
        raise RecoveryError("Save a readiness policy or supply a complete policy object")
    _validate_policy(policy)
    admission = _target_capability(snapshot, database, policy["maximum_snapshot_age_seconds"], window_start=window_start)
    if not admission["can_create"]:
        findings = "; ".join(f"{item['label']}: observed {item['observed']}; required {item['required']}. {item['next_action']}"
                             for item in admission["blockers"])
        raise RecoveryError(f"Recovery request blocked for {database}: {findings}")
    selected = next(db for db in snapshot["databases"] if isinstance(db, dict) and db.get("db_unique_name") == database)
    home = selected["oracle_home"]
    owner = next(item["owner"] for item in snapshot["oracle_homes"] if isinstance(item, dict) and item.get("path") == home)
    target = {"database_unique_name": database, "oracle_home": home,
              "owner": owner, "oracle_sid": selected["runtime"]["instance"]}
    try:
        collected = datetime.strptime(snapshot["collected_at"], "%Y-%m-%dT%H:%M:%SZ")
        age = (datetime.now(timezone.utc).replace(tzinfo=None) - collected).total_seconds()
        if age < 0 or age > policy["maximum_snapshot_age_seconds"]:
            raise ValueError("discovery expired or is in the future")
        if (datetime.strptime(window_start, "%Y-%m-%dT%H:%M:%SZ") - collected).total_seconds() > policy["maximum_snapshot_age_seconds"]:
            raise ValueError("maintenance window starts after discovery expires")
    except (KeyError, ValueError, TypeError) as exc:
        raise RecoveryError("Refresh discovery before creating recovery: snapshot must be fresh through the window start") from exc
    policy_bytes = (json.dumps(policy, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    base = _live_path(request_id)
    if _fixture_path(request_id).exists():
        raise RecoveryError("A demo recovery request already uses this request_id")
    try:
        base.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise RecoveryError("Live recovery request already exists; inspect its status instead of recreating it") from exc
    input_dir = f"{REMOTE_STATE_DIR}/webapp-inputs/{request_id}"
    metadata = {"mode": "live", "request_id": request_id, "host_id": host_id,
                "host": {k: host.get(k) for k in ("id", "ssh_alias", "remote_root", "sudo")},
                "database": database, "target": target, "remote_input_dir": input_dir, "create_state": "preparing",
                "inputs": {"snapshot": {"path": f"{input_dir}/snapshot.json", "sha256": hashlib.sha256(snapshot_bytes).hexdigest()},
                           "policy": {"path": f"{input_dir}/policy.json", "sha256": hashlib.sha256(policy_bytes).hexdigest()}}}
    write_json(base / "metadata.json", metadata)
    for name, data in (("snapshot.json", snapshot_bytes), ("policy.json", policy_bytes)):
        path = base / name
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o400)
    # Install from trusted local package; no payload-provided host or command.
    tools_sync.ensure_tools(host["ssh_alias"], host["remote_root"], bool(host.get("sudo")))
    _update_metadata(request_id, create_state="remote_create_unknown")
    q = shlex.quote
    prepare = (f"set -eu; umask 077; [ ! -L {q(REMOTE_STATE_DIR)} ]; "
               f"mkdir -p {q(REMOTE_STATE_DIR)}; [ ! -L {q(REMOTE_STATE_DIR + '/webapp-inputs')} ]; "
               f"mkdir -p {q(REMOTE_STATE_DIR + '/webapp-inputs')}; "
               f"[ ! -e {q(REMOTE_STATE_DIR + '/' + request_id)} ]; mkdir {q(input_dir)}")
    checked = remote.run_remote_shell(host["ssh_alias"], prepare, sudo=True)
    if checked.returncode:
        raise RecoveryError("Could not reserve a new immutable remote recovery input directory", checked.stderr)
    for name, data in (("snapshot", snapshot_bytes), ("policy", policy_bytes)):
        remote.push_file(host["ssh_alias"], metadata["inputs"][name]["path"], data, sudo=True)
    sealed = remote.run_remote_shell(host["ssh_alias"],
        "set -eu; " + "; ".join(
            f"[ \"$(sha256sum {q(item['path'])} | cut -d' ' -f1)\" = {q(item['sha256'])} ]; chmod 400 {q(item['path'])}"
            for item in metadata["inputs"].values()), sudo=True)
    if sealed.returncode:
        raise RecoveryError("Remote immutable recovery input verification failed", sealed.stderr)
    result = _native(host, request_id, "create", ["--requester", requester,
        "--snapshot", metadata["inputs"]["snapshot"]["path"], "--policy", metadata["inputs"]["policy"]["path"],
        "--database", database, "--backup-parent", backup_parent, "--window-start", window_start, "--window-end", window_end])
    metadata = _update_metadata(request_id, create_state="created", last_status=result)
    return _decorate(result, metadata)


def _poll_launch(host: dict, execution: dict) -> tuple[str, int | None]:
    run_dir = execution.get("remote_run_dir")
    request_id = _identifier(execution.get("request_id"), "request_id")
    prefix = f"{REMOTE_STATE_DIR}/webapp-executions/{request_id}/"
    if not isinstance(run_dir, str) or not run_dir.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{32}", run_dir[len(prefix):]):
        raise RecoveryError("Invalid persisted recovery launch directory")
    expected_pid = execution.get("pid")
    if expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0):
        raise RecoveryError("Persisted recovery wrapper PID is invalid")
    pid_check = f'[ "$pid" = {expected_pid} ] || {{ echo UNKNOWN; exit 0; }}; ' if expected_pid else ""
    script = (f"cd {shlex.quote(run_dir)} 2>/dev/null || {{ echo MISSING; exit 0; }}; "
              'if [ -f rc ]; then echo RC; cat rc; else '
              'pid=$(cat pid 2>/dev/null) || { echo UNKNOWN; exit 0; }; '
              '[[ "$pid" =~ ^[1-9][0-9]*$ ]] || { echo UNKNOWN; exit 0; }; '
              + pid_check + 'if kill -0 "$pid" 2>/dev/null; then echo RUNNING; else echo DEAD; fi; fi')
    response = remote.run_remote_shell(host["ssh_alias"], script, timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=True)
    lines = response.stdout.strip().splitlines()
    if response.returncode or not lines or lines[0] not in {"RC", "RUNNING", "MISSING", "DEAD", "UNKNOWN"}:
        raise RecoveryError("Cannot inspect remote recovery execution", response.stderr)
    if lines[0] == "RC":
        if len(lines) != 2 or not re.fullmatch(r"[0-9]{1,3}", lines[1]) or int(lines[1]) > 255:
            raise RecoveryError("Remote recovery exit status is invalid")
        return "RC", int(lines[1])
    return lines[0], None


def _import_evidence(request_id: str, host: dict, native: dict) -> dict:
    """Import only small sealed documents, retaining every native path and byte."""
    result = native.get("result") or {}
    expected = {"recovery_evidence": "recovery-evidence.json", "post_snapshot": "post-backup-topology.json",
                "reconciliation": "post-backup-reconciliation.json"}
    request_prefix = f"{REMOTE_STATE_DIR}/{request_id}/evidence/"
    imported = {}
    base = _live_path(request_id) / "evidence"
    base.mkdir(mode=0o700, exist_ok=True)
    for kind, filename in expected.items():
        binding = result.get(kind)
        if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
            raise RecoveryError(f"Completed native request lacks {kind}")
        # Native reconciliation filename is explicitly fixed by the executor.
        if binding["path"] != request_prefix + filename:
            raise RecoveryError(f"Completed request contains an unexpected {kind} path")
        payload = remote.pull_file(host["ssh_alias"], binding["path"], sudo=True, max_bytes=MAX_EVIDENCE_BYTES)
        if hashlib.sha256(payload).hexdigest() != binding.get("sha256"):
            raise RecoveryError(f"Completed {kind} bytes do not match the native request digest")
        json.loads(payload)
        target = base / filename
        if target.is_symlink():
            raise RecoveryError("Local recovery evidence cannot be a symbolic link")
        if target.exists():
            if target.read_bytes() != payload:
                raise RecoveryError("Existing recovery evidence differs; refusing replacement")
        else:
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o400)
        imported[kind] = {"path": str(target), "remote_path": binding["path"], "sha256": binding["sha256"]}
    return imported


def _terminal_result(request_id: str, host: dict, execution: dict, returncode: int) -> dict:
    native = _native(host, request_id, "status")
    action = execution.get("action", "execute")
    is_reconcile = action == "reconcile"
    if native.get("state") == "running":
        raise RecoveryError("Recovery wrapper exited but the native request remains running; reconciliation is required")
    expected_states = {"failed_services_restored", "recovery_required"} if is_reconcile else {"completed"}
    if returncode == 0 and native.get("state") not in expected_states:
        raise RecoveryError("Successful recovery exit contradicts the native terminal state")
    field = "reconciliation_execution" if is_reconcile else "execution"
    updates = {"last_status": native, field: {**execution, "terminal": True, "exit_code": returncode}}
    if returncode == 0 and not is_reconcile:
        updates["evidence"] = _import_evidence(request_id, host, native)
    if returncode == 0 and is_reconcile:
        original = _metadata(request_id)["execution"]
        updates["execution"] = {**original, "reconciled": True, "terminal": True,
                                "reconciliation_run_dir": execution["remote_run_dir"]}
    metadata = _update_metadata(request_id, **updates)
    pipeline_runner.set_execution_context(detached_terminal=True)
    if returncode:
        raise RecoveryError(f"Recovery {action} exited {returncode}; native state is {native.get('state')}")
    if native.get("state") == "recovery_required":
        raise RecoveryError("Service reconciliation completed but database/listener health remains unverified; operator recovery is required")
    return _decorate(native, metadata)


def _execute_live(request_id: str, host: dict, actor: str, action: str = "execute") -> dict:
    _identifier(actor, "actor")
    production.require_live_mutation_allowed()
    base = _live_path(request_id)
    with file_lock(base / ".action.lock"):
        metadata = _metadata(request_id)
        field = "reconciliation_execution" if action == "reconcile" else "execution"
        if metadata.get(field):
            raise RecoveryError(f"Recovery {action} was already submitted; inspect or reconcile its run instead of retrying")
        current = _native(host, request_id, "status")
        required_state = "running" if action == "reconcile" else "authorized"
        if current.get("state") != required_state or (current.get("authorization") or {}).get("actor") != actor:
            raise RecoveryError(f"Recovery must be {required_state} and authorized by this actor before {action}")
        if action == "reconcile":
            original = metadata.get("execution")
            if not original or _poll_launch(host, original)[0] not in {"DEAD", "RC"}:
                raise RecoveryError("Original recovery wrapper must be proven exited before service reconciliation")
        run_dir = f"{REMOTE_STATE_DIR}/webapp-executions/{request_id}/{uuid.uuid4().hex}"
        execution = {"request_id": request_id, "remote_run_dir": run_dir, "actor": actor, "action": action,
                     "terminal": False, "submitted_at": time.time()}
        # Persist both request ownership and pipeline context before SSH. A lost
        # launch response is unknown, never permission to launch a second job.
        _update_metadata(request_id, **{field: execution})
        pipeline_runner.set_execution_context(detached_execution=True, detached_terminal=False,
            recovery_request_id=request_id, recovery_action=action, host_id=host["id"], ssh_alias=host["ssh_alias"],
            remote_root=host["remote_root"], remote_run_dir=run_dir)
        q = shlex.quote
        command = " ".join(q(a) for a in _argv(host, action, request_id, ["--actor", actor]))
        wrapper = f"umask 077; echo $$ >pid.tmp; mv pid.tmp pid; {command} >stdout 2>stderr </dev/null; rc=$?; echo $rc >rc.tmp; mv rc.tmp rc"
        parent = str(PurePosixPath(run_dir).parent)
        launch = (f"set -eu; umask 077; mkdir -p {q(parent)}; mkdir {q(run_dir)}; cd {q(run_dir)}; "
                  f"nohup setsid bash -c {q(wrapper)} >/dev/null 2>&1 </dev/null & "
                  'for n in 1 2 3 4 5 6 7 8 9 10; do [ -s pid ] && break; sleep 1; done; cat pid')
        response = remote.run_remote_shell(host["ssh_alias"], launch, timeout=LIVE_POLL_SSH_TIMEOUT_SECONDS, sudo=True)
        pid = response.stdout.strip()
        if response.returncode or not re.fullmatch(r"[1-9][0-9]*", pid):
            raise RecoveryError("Recovery launch response was lost or invalid; inspect the existing execution before any further action", response.stderr)
        execution = {**execution, "pid": int(pid)}
        _update_metadata(request_id, **{field: execution})
        pipeline_runner.set_execution_context(remote_pid=int(pid))
    deadline = time.monotonic() + LIVE_EXECUTE_TIMEOUT_SECONDS
    failures = 0
    while time.monotonic() < deadline:
        time.sleep(LIVE_POLL_INTERVAL_SECONDS)
        try:
            state, rc = _poll_launch(host, execution)
        except (RecoveryError, remote.RemoteError):
            failures += 1
            if failures >= 5:
                raise RecoveryError("Lost contact with recovery execution; it may still be running. Reconcile its existing run.") from None
            continue
        failures = 0
        if state == "RC":
            return _terminal_result(request_id, host, execution, rc)
        if state != "RUNNING":
            raise RecoveryError(f"Recovery execution is {state.lower()} without an exit status; inspect and reconcile the existing request")
    raise RecoveryError("Recovery polling limit reached; remote execution was left running and must be reconciled")


def _live_status(request_id: str) -> dict:
    metadata = _metadata(request_id)
    host = _configured_host(metadata)
    native = _native(host, request_id, "status")
    metadata = _update_metadata(request_id, last_status=native)
    # Status is read-only remotely. Completion import remains local and only
    # follows a verified terminal wrapper plus native sealed completion state.
    execution = metadata.get("execution")
    if native.get("state") == "completed" and execution and not metadata.get("evidence"):
        state, rc = _poll_launch(host, execution)
        if state == "RC" and rc == 0:
            imported = _import_evidence(request_id, host, native)
            metadata = _update_metadata(request_id, evidence=imported,
                                        execution={**execution, "terminal": True, "exit_code": 0})
    return _decorate(native, metadata)


def _live_action(request_id: str, action: str, extra: list[str]) -> dict:
    metadata = _metadata(request_id)
    host = _configured_host(metadata)
    for index in range(0, len(extra), 2):
        _identifier(extra[index + 1], extra[index])
    if action == "execute":
        return _execute_live(request_id, host, extra[1])
    if action in {"authorize", "reconcile"}:
        production.require_live_mutation_allowed()
    if action == "reconcile":
        execution = metadata.get("reconciliation_execution") or metadata.get("execution")
        if not execution:
            raise RecoveryError("No live recovery execution exists to reconcile")
        state, rc = _poll_launch(host, execution)
        if state == "RUNNING":
            return _decorate({"request_id": request_id, "status": "in_progress"}, metadata)
        if state == "RC":
            # A killed native worker can leave an ordinary wrapper exit code
            # while its durable request remains running. That is precisely the
            # interrupted state native service reconciliation is designed for.
            # The native reconciler rechecks its worker PID and host/RMAN lock.
            if metadata.get("reconciliation_execution") or _native(host, request_id, "status").get("state") != "running":
                return _terminal_result(request_id, host, execution, rc)
        if metadata.get("reconciliation_execution"):
            raise RecoveryError("Service reconciliation already has an unknown outcome; inspect its existing run without retrying")
        if state not in {"DEAD", "RC"}:
            raise RecoveryError("Recovery launch has no verified dead wrapper; refusing service reconciliation")
        return _execute_live(request_id, host, extra[1], action="reconcile")
    with file_lock(_live_path(request_id) / ".action.lock"):
        if action == "analyze":
            metadata = _update_metadata(request_id, analysis=None)
        native = _native(host, request_id, action, extra)
        metadata = _update_metadata(request_id, **({"analysis": native} if action == "analyze" else {"last_status": native}))
    return _decorate(native, metadata)


def reconcile_detached_run(record: dict) -> dict:
    """Resolve transport ownership from the existing native job; never relaunch."""
    try:
        context = record.get("context") or {}
        request_id = _identifier(context.get("recovery_request_id"), "request_id")
        metadata = _metadata(request_id)
        host = _configured_host(metadata)
        action = context.get("recovery_action", "execute")
        if action not in {"execute", "reconcile"}:
            raise RecoveryError("Persisted recovery action is invalid")
        field = "reconciliation_execution" if action == "reconcile" else "execution"
        execution = metadata.get(field) or {}
        if any(context.get(k) != expected for k, expected in {
                "host_id": host["id"], "ssh_alias": host["ssh_alias"], "remote_root": host["remote_root"],
                "remote_run_dir": execution.get("remote_run_dir")}.items()):
            raise RecoveryError("Persisted recovery execution context does not match this request")
        if context.get("remote_pid") is not None and context["remote_pid"] != execution.get("pid"):
            raise RecoveryError("Persisted recovery execution PID does not match this request")
        state, rc = _poll_launch(host, execution)
        if state == "RC":
            try:
                result = _terminal_result(request_id, host, execution, rc)
            except RecoveryError as exc:
                if (_metadata(request_id).get(field) or {}).get("terminal"):
                    return {"status": "failed", "error": exc.to_json()}
                raise
            return {"status": "succeeded", "result": result}
        if execution.get("reconciled"):
            current = _native(host, request_id, "status")
            if current.get("state") in {"failed_services_restored", "recovery_required"}:
                return {"status": "failed", "error": {"message": "Interrupted recovery was reconciled", "state": current["state"]}}
        return {"status": "unknown", "error": {"message": "Recovery is active or has no verified terminal result", "remote_state": state}}
    except (RecoveryError, remote.RemoteError, ValueError, OSError) as exc:
        return {"status": "unknown", "error": {"message": str(exc)}}
