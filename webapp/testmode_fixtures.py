"""TEST_MODE fixture harness for the standalone Database executor.

The shim scripts alongside this file (sqlplus.sh, lsnrctl.sh, opatch.sh,
datapatch.sh, java.sh) are extracted verbatim from
tests/single_instance_patch.sh, so the webapp and the test suite share one
definition of what a fake Oracle home looks like rather than two that can
drift. This module builds a self-contained fixture tree plus the full real
evidence chain (via the real opu-artifact-inspect binary, exactly like the
test does) needed to create/approve/authorize/dispatch a real plan against
it, and returns the environment variables the real executor binary needs to
run against the fixture instead of a real Oracle home.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
SHIMS_DIR = Path(__file__).resolve().parent / "testmode_fixtures"


class FixtureError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jq_canonical_hash(data: dict) -> str:
    """Mirrors the fixture's own convention: jq -cS . | tr -d '\\n' | sha256sum."""
    result = subprocess.run(["jq", "-cS", "."], input=json.dumps(data), capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise FixtureError(f"jq canonicalization failed: {result.stderr}")
    return hashlib.sha256(result.stdout.rstrip("\n").encode()).hexdigest()


def _run_tool(name: str, args: list[str], timeout: int = 30) -> None:
    result = subprocess.run([str(BIN / name), *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise FixtureError(f"{name} failed while building fixture: {result.stderr.strip()}")


def build(base_dir: Path) -> dict:
    """Builds a fresh standalone-database TEST_MODE fixture under base_dir.

    Returns {"evidence": {...paths to readiness.json etc.}, "oratab": path,
    "env": {...env vars for invoking the executor}, "state": {...state file paths}}.
    """
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)

    oracle_home = base_dir / "oracle" / "dbhome_1"
    patch_dir = base_dir / "patch" / "39034528"
    backup_root = base_dir / "backup" / "ORCL" / "run-001"
    for d in (
        oracle_home / "bin", oracle_home / "OPatch", oracle_home / "jdk" / "bin",
        patch_dir / "etc" / "config", backup_root / "rman", backup_root / "oracle-home",
    ):
        d.mkdir(parents=True, exist_ok=True)

    for shim, target in (
        ("sqlplus.sh", oracle_home / "bin" / "sqlplus"),
        ("lsnrctl.sh", oracle_home / "bin" / "lsnrctl"),
        ("opatch.sh", oracle_home / "OPatch" / "opatch"),
        ("datapatch.sh", oracle_home / "OPatch" / "datapatch"),
        ("java.sh", oracle_home / "jdk" / "bin" / "java"),
    ):
        shutil.copy(SHIMS_DIR / shim, target)
        target.chmod(0o750)

    state = {
        "database": base_dir / "database.state",
        "listener": base_dir / "listener.state",
        "patch": base_dir / "patch.state",
        "datapatch": base_dir / "datapatch.state",
        "sqlpatch_action": base_dir / "sqlpatch-action.state",
        "opatch_calls": base_dir / "opatch-calls.log",
        "fail_applicability": base_dir / "fail-applicability",
        "fail_opatch": base_dir / "fail-opatch",
        "fail_datapatch": base_dir / "fail-datapatch",
        "fail_rollback": base_dir / "fail-rollback",
    }
    state["database"].write_text("up\n")
    state["listener"].write_text("up\n")

    (patch_dir / "etc" / "config" / "inventory.xml").write_text(
        '<patch patchID="39034528"><description>Database Release Update</description>'
        '<os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n'
    )
    readme = patch_dir / "README.txt"
    readme.write_text("Database Release Update test README\nopatch rollback -id 39034528\ndatapatch -verbose\n")

    artifact_json = base_dir / "artifact.json"
    _run_tool("opu-artifact-inspect", ["--artifact", str(patch_dir), "--output", str(artifact_json)])
    artifact = json.loads(artifact_json.read_text())
    artifact_sha = artifact["artifact"]["sha256"]
    readme_sha = _sha256_file(readme)

    (backup_root / "rman" / "piece-01").write_text("backup piece\n")
    (backup_root / "oracle-home" / "dbhome.tar.gz").write_text("Oracle home archive\n")
    checksum_manifest = backup_root / "SHA256SUMS"
    checksum_manifest.write_text(
        f"{_sha256_file(backup_root / 'rman' / 'piece-01')}  {backup_root / 'rman' / 'piece-01'}\n"
        f"{_sha256_file(backup_root / 'oracle-home' / 'dbhome.tar.gz')}  {backup_root / 'oracle-home' / 'dbhome.tar.gz'}\n"
    )
    rman_log = base_dir / "rman-validate.log"
    rman_log.write_text("RMAN validation complete\n")

    owner = subprocess.run(["id", "-un"], capture_output=True, text=True, timeout=5).stdout.strip()

    now_iso = subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, timeout=5).stdout.strip()
    snapshot = {
        "schema_version": "1.0",
        "collector": {"name": "oracle.topology.discover", "version": "1"},
        "collected_at": now_iso,
        "host": {"name": "testnode.example"},
        "cluster": {"status": "unavailable", "grid_home": None, "runtime": {"status": "unavailable"}, "nodes": []},
        "oracle_homes": [],
        "databases": [],
        "warnings": [],
    }
    snapshot_path = base_dir / "topology-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2))
    snapshot_sha = _sha256_file(snapshot_path)

    recovery_body = {
        "schema_version": "1.0",
        "collector": {"name": "oracle.recovery.evidence", "version": "1"},
        "status": "passed",
        "target": {"database_unique_name": "ORCL", "oracle_home": str(oracle_home), "owner": owner, "oracle_sid": "ORCL"},
        "source_snapshot": {"path": str(snapshot_path), "sha256": snapshot_sha},
        "backup": {
            "root": str(backup_root),
            "checksum_manifest": {"path": str(checksum_manifest), "sha256": _sha256_file(checksum_manifest)},
            "files": [],
            "oracle_home_archive": str(backup_root / "oracle-home" / "dbhome.tar.gz"),
            "rman_backup_set_keys": [1],
            "selected_recovery_set": {
                "observed_at": now_iso,
                "oldest_datafile_backup_completed_at": now_iso,
                "age_seconds_at_collection": 0,
                "restore_piece_handles": [str(backup_root / "backup-piece")],
                "datafile_backup_sets": [1],
            },
            "coverage": {
                "datafiles_backed": 1, "base_datafiles": 1, "datafiles_current": 1,
                "controlfile_records": 1, "spfile_records": 1, "outside_root_pieces": 0, "unavailable_pieces": 0,
            },
        },
        "verification": {"rman_log": {"path": str(rman_log), "sha256": _sha256_file(rman_log)}},
    }
    recovery_record = _jq_canonical_hash(recovery_body)
    recovery = {**recovery_body, "record_sha256": recovery_record}
    recovery_path = base_dir / "recovery.json"
    recovery_path.write_text(json.dumps(recovery, indent=2))

    reconciliation = {
        "schema_version": "1.0",
        "status": "consistent",
        "expected_nodes": ["testnode"],
        "oracle_homes": [{
            "path": str(oracle_home), "owner": owner, "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51",
            "platform": {"status": "collected", "id": "226", "name": "Linux x86-64", "source": "opatch_lsinventory_xml"},
            "patches": [],
        }],
        "databases": [{"db_unique_name": "ORCL", "oracle_home": str(oracle_home)}],
        "snapshot_evidence": [{"path": str(snapshot_path), "sha256": snapshot_sha}],
    }
    reconciliation_path = base_dir / "reconciliation.json"
    reconciliation_path.write_text(json.dumps(reconciliation, indent=2))

    procedure = {
        "schema_version": "1.0",
        "status": "ready_for_planning",
        "procedure": {
            "schema_version": "1.0",
            "patch_id": "39034528",
            "artifact_sha256": artifact_sha,
            "target": {"family": "database", "method": "opatch", "topology": "single_instance", "database_unique_name": "ORCL", "platform_id": "226"},
            "execution": {"adapter": "database_single_instance_opatch", "operations": ["database_shutdown", "database_opatch_apply", "database_startup", "database_datapatch"]},
            "required_opatch_version": "12.2.0.1.49",
            "oracle_references": [{"kind": "patch_readme", "identifier": "README rollback sections", "sha256": readme_sha}],
            "rollback": {"mode": "opatch_rollback", "precondition": "separate approved rollback plan required"},
        },
    }
    procedure_path = base_dir / "procedure.json"
    procedure_path.write_text(json.dumps(procedure, indent=2))

    compatibility = {
        "schema_version": "1.0",
        "status": "passed",
        "patch_id": "39034528",
        "artifact_sha256": artifact_sha,
        "target": {"family": "database", "platform_id": "226"},
        "checks": [{
            "node": "testnode", "home": str(oracle_home), "owner": "test", "status": "passed",
            "platform": {"host_id": "226", "host_name": "Linux x86-64", "artifact_ids": ["226"], "procedure_id": "226", "status": "passed"},
            "opatch": {"actual_version": "12.2.0.1.51", "required_version": "12.2.0.1.49", "status": "passed"},
            "applicability_check": {"name": "CheckPatchApplicableOnCurrentPlatform", "status": "passed", "exit_code": 0, "patch_option": "-ph", "patch_source": "/tmp/patch", "evidence_path": "/tmp/applicability.log", "evidence_sha256": artifact_sha},
            "conflict_check": {"name": "CheckConflictAgainstOHWithDetail", "status": "passed", "exit_code": 0, "patch_option": "-ph", "patch_source": "/tmp/patch", "evidence_path": "/tmp/conflict.log", "evidence_sha256": artifact_sha},
        }],
    }
    compatibility_path = base_dir / "compatibility.json"
    compatibility_path.write_text(json.dumps(compatibility, indent=2))

    policy = {
        "schema_version": "1.0",
        "maximum_snapshot_age_seconds": 3600,
        "require_xml_inventory": True,
        "recovery": {"require_backup": True, "max_backup_age_minutes": 1440, "minimum_fra_free_bytes": 0, "require_guaranteed_restore_point": False},
        "database": {"require_primary_read_write": True, "maximum_invalid_objects": 0},
    }
    policy_path = base_dir / "policy.json"
    policy_path.write_text(json.dumps(policy, indent=2))

    now_epoch = int(subprocess.run(["date", "-u", "+%s"], capture_output=True, text=True, timeout=5).stdout.strip())
    valid_until = subprocess.run(["date", "-u", "-r", str(now_epoch + 3600), "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, timeout=5).stdout.strip()
    readiness = {
        "schema_version": "1.0",
        "status": "ready_for_approval",
        "patch_id": "39034528",
        "target": {"family": "database", "method": "opatch", "platform_id": "226"},
        "evaluated_at": now_iso,
        "valid_until": valid_until,
        "snapshot_evidence": [{"path": str(snapshot_path), "sha256": snapshot_sha, "host": "testnode", "collected_at": now_iso, "valid_until": valid_until}],
        "evidence": {
            "reconciliation_sha256": _sha256_file(reconciliation_path),
            "artifact_manifest_sha256": _sha256_file(artifact_json),
            "procedure_validation_sha256": _sha256_file(procedure_path),
            "compatibility_sha256": _sha256_file(compatibility_path),
            "policy_sha256": _sha256_file(policy_path),
        },
    }
    readiness_path = base_dir / "readiness.json"
    readiness_path.write_text(json.dumps(readiness, indent=2))

    oratab_path = base_dir / "oratab"
    oratab_path.write_text(f"ORCL:{oracle_home}:N\n")

    env = {}
    if shutil.which("flock") is None:
        # macOS ships no util-linux flock — supply the same narrow test double
        # tests/single_instance_patch.sh uses, which takes a real BSD flock on
        # the inherited descriptor.
        flock_shim = base_dir / "flock"
        shutil.copy(SHIMS_DIR / "flock.py", flock_shim)
        flock_shim.chmod(0o750)
        env["OPU_SINGLE_INSTANCE_TEST_FLOCK"] = str(flock_shim)
        env["FLOCK_PROBE"] = str(base_dir / "flock-called")

    env.update({
        "OPU_SINGLE_INSTANCE_STATE_DIR": str(base_dir / "execution-state"),
        "OPU_SINGLE_INSTANCE_ROLLBACK_STATE_DIR": str(base_dir / "rollback-state"),
        "OPU_SINGLE_INSTANCE_TEST_MODE": "1",
        "OPU_SINGLE_INSTANCE_ORATAB": str(oratab_path),
        "OPU_SINGLE_INSTANCE_TEST_LISTENER": "LISTENER",
        "OPU_SINGLE_INSTANCE_TEST_DATABASE_STATE": str(state["database"]),
        "OPU_TEST_DATABASE_STATE": str(state["database"]),
        "OPU_TEST_LISTENER_STATE": str(state["listener"]),
        "OPU_TEST_PATCH_STATE": str(state["patch"]),
        "OPU_TEST_FAIL_APPLICABILITY": str(state["fail_applicability"]),
        "OPU_TEST_OPATCH_CALLS": str(state["opatch_calls"]),
        "OPU_TEST_DATAPATCH_STATE": str(state["datapatch"]),
        "OPU_TEST_FAIL_OPATCH": str(state["fail_opatch"]),
        "OPU_TEST_FAIL_DATAPATCH": str(state["fail_datapatch"]),
        "OPU_TEST_FAIL_ROLLBACK": str(state["fail_rollback"]),
        "OPU_TEST_SQLPATCH_ACTION_STATE": str(state["sqlpatch_action"]),
    })

    (base_dir / "env.json").write_text(json.dumps(env, indent=2))

    return {
        "evidence": {
            "readiness": readiness_path, "reconciliation": reconciliation_path, "artifact": artifact_json,
            "procedure": procedure_path, "compatibility": compatibility_path, "policy": policy_path, "recovery": recovery_path,
        },
        "oratab": oratab_path,
        "env": env,
        "state": state,
    }


def env_for(base_dir: Path) -> dict:
    """Reloads the executor env vars for an already-built fixture, so a plan
    created earlier can have its tasks executed across multiple calls
    without rebuilding (and thereby resetting) the fixture's state."""
    env_path = base_dir / "env.json"
    if not env_path.is_file():
        raise FixtureError(f"No fixture env recorded at {env_path}")
    return json.loads(env_path.read_text())
