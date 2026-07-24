"""TEST_MODE fixture harnesses for the standalone, RAC, and Grid executors.

The shim scripts alongside this file (sqlplus.sh, lsnrctl.sh, opatch.sh,
datapatch.sh, java.sh, rac_*.sh, grid_*.sh) are extracted verbatim from
tests/single_instance_patch.sh, tests/rac_database_patch.sh, and
tests/grid_node_patch.sh / tests/grid_node_rollback.sh, so the webapp and the
test suite share one definition of what a fake Oracle/Grid home looks like
rather than two that can drift. This module builds a self-contained fixture
tree plus the full real evidence chain (via the real opu-artifact-inspect
binary, exactly like the tests do) needed to
create/approve/authorize/dispatch a real plan against it, and returns the
environment variables the real executor binary needs to run against the
fixture instead of a real Oracle home.
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


def _owner() -> str:
    return subprocess.run(["id", "-un"], capture_output=True, text=True, timeout=5).stdout.strip()


def _now_epoch() -> int:
    return int(subprocess.run(["date", "-u", "+%s"], capture_output=True, text=True, timeout=5).stdout.strip())


def _iso_from_epoch(epoch: int) -> str:
    result = subprocess.run(
        ["date", "-u", "-r", str(epoch), "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = subprocess.run(
        ["date", "-u", "-d", f"@{epoch}", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise FixtureError("cannot format a UTC timestamp with date")
    return result.stdout.strip()


def _copy_shims(pairs: tuple[tuple[str, Path], ...]) -> None:
    for shim, target in pairs:
        shutil.copy(SHIMS_DIR / shim, target)
        target.chmod(0o750)


def _write_cluster_snapshots(base_dir: Path, collected_at: str) -> dict[str, tuple[Path, str]]:
    """Two-node topology snapshots shared by the RAC and Grid fixtures."""
    snapshots: dict[str, tuple[Path, str]] = {}
    for node in ("node1", "node2"):
        snapshot = {
            "schema_version": "1.0",
            "collector": {"name": "oracle.topology.discover", "version": "1"},
            "collected_at": collected_at,
            "host": {"name": f"{node}.example"},
            "cluster": {"status": "detected", "grid_home": None, "runtime": {"status": "healthy"}, "nodes": []},
            "oracle_homes": [],
            "databases": [],
            "warnings": [],
        }
        path = base_dir / f"{node}-topology.json"
        path.write_text(json.dumps(snapshot, indent=2))
        snapshots[node] = (path, _sha256_file(path))
    return snapshots


def _cluster_policy() -> dict:
    return {
        "schema_version": "1.0",
        "maximum_snapshot_age_seconds": 3600,
        "require_xml_inventory": True,
        "recovery": {"require_backup": True, "max_backup_age_minutes": 1440, "minimum_fra_free_bytes": 0, "require_guaranteed_restore_point": False},
        "database": {"require_primary_read_write": True, "maximum_invalid_objects": 0},
    }


def _cluster_readiness(family: str, evaluated_at: str, valid_until: str, snapshots: dict[str, tuple[Path, str]], evidence_paths: dict[str, Path]) -> dict:
    return {
        "schema_version": "1.0",
        "status": "ready_for_approval",
        "patch_id": "39034528",
        "target": {"family": family, "method": "opatch", "platform_id": "226"},
        "evaluated_at": evaluated_at,
        "valid_until": valid_until,
        "snapshot_evidence": [
            {"path": str(path), "sha256": sha, "host": node, "collected_at": evaluated_at, "valid_until": valid_until}
            for node, (path, sha) in snapshots.items()
        ],
        "evidence": {
            "reconciliation_sha256": _sha256_file(evidence_paths["reconciliation"]),
            "artifact_manifest_sha256": _sha256_file(evidence_paths["artifact"]),
            "procedure_validation_sha256": _sha256_file(evidence_paths["procedure"]),
            "compatibility_sha256": _sha256_file(evidence_paths["compatibility"]),
            "policy_sha256": _sha256_file(evidence_paths["policy"]),
        },
    }


def _compatibility_check(node: str, home: Path, owner: str, artifact_sha: str) -> dict:
    return {
        "node": node, "home": str(home), "owner": owner, "status": "passed",
        "platform": {"host_id": "226", "host_name": "Linux x86-64", "artifact_ids": ["226"], "procedure_id": "226", "status": "passed"},
        "opatch": {"actual_version": "12.2.0.1.51", "required_version": "12.2.0.1.49", "status": "passed"},
        "applicability_check": {"name": "CheckPatchApplicableOnCurrentPlatform", "status": "passed", "exit_code": 0, "patch_option": "-ph", "patch_source": "/tmp/patch", "evidence_path": "/tmp/applicability.log", "evidence_sha256": artifact_sha},
        "conflict_check": {"name": "CheckConflictAgainstOHWithDetail", "status": "passed", "exit_code": 0, "patch_option": "-ph", "patch_source": "/tmp/patch", "evidence_path": "/tmp/conflict.log", "evidence_sha256": artifact_sha},
    }


def build_rac(base_dir: Path) -> dict:
    """Builds a fresh two-node RAC database TEST_MODE fixture under base_dir,
    mirroring tests/rac_database_patch.sh. Returns the same shape as build()."""
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)

    oracle_home = base_dir / "oracle" / "dbhome_1"
    grid_home = base_dir / "grid"
    patch_dir = base_dir / "patch" / "39034528"
    backup_root = base_dir / "backup" / "ORCL" / "run-001"
    runtime = base_dir / "runtime"
    for d in (
        oracle_home / "bin", oracle_home / "OPatch", oracle_home / "jdk" / "bin",
        grid_home / "bin", patch_dir / "etc" / "config", backup_root, runtime,
    ):
        d.mkdir(parents=True, exist_ok=True)
    (runtime / "node1.state").write_text("running\n")
    (runtime / "node2.state").write_text("running\n")

    _copy_shims((
        ("rac_srvctl.sh", grid_home / "bin" / "srvctl"),
        ("rac_sqlplus.sh", oracle_home / "bin" / "sqlplus"),
        ("rac_opatch.sh", oracle_home / "OPatch" / "opatch"),
        ("rac_datapatch.sh", oracle_home / "OPatch" / "datapatch"),
        ("java.sh", oracle_home / "jdk" / "bin" / "java"),
    ))

    (patch_dir / "etc" / "config" / "actions.xml").write_text('<patch patchID="39034528"><actions/></patch>\n')
    (patch_dir / "etc" / "config" / "inventory.xml").write_text(
        '<patch patchID="39034528"><description>Database Release Update</description>'
        '<os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n'
    )
    readme = patch_dir / "README.txt"
    readme.write_text(
        "RAC Database Release Update test README\n"
        "srvctl stop instance -db ORCL -instance ORCL1\n"
        "opatch apply\n"
        "opatch rollback -id 39034528\n"
        "srvctl start instance -db ORCL -instance ORCL1\n"
        "datapatch -verbose\n"
    )

    artifact_json = base_dir / "artifact.json"
    _run_tool("opu-artifact-inspect", ["--artifact", str(patch_dir), "--output", str(artifact_json)])
    artifact_sha = json.loads(artifact_json.read_text())["artifact"]["sha256"]
    readme_sha = _sha256_file(readme)
    owner = _owner()
    now_epoch = _now_epoch()
    now_iso = _iso_from_epoch(now_epoch)
    valid_until = _iso_from_epoch(now_epoch + 3600)
    snapshots = _write_cluster_snapshots(base_dir, now_iso)

    backup_piece = backup_root / "backup-piece"
    backup_piece.write_text("recoverable backup\n")
    checksum_manifest = backup_root / "SHA256SUMS"
    checksum_manifest.write_text(f"{_sha256_file(backup_piece)}  {backup_piece}\n")
    rman_log = base_dir / "rman-validate.log"
    rman_log.write_text("RMAN validation passed\n")

    node1_snapshot_path, node1_snapshot_sha = snapshots["node1"]
    recovery_body = {
        "schema_version": "1.0",
        "collector": {"name": "oracle.recovery.evidence", "version": "1"},
        "status": "passed",
        "target": {"database_unique_name": "ORCL", "oracle_home": str(oracle_home), "owner": owner, "oracle_sid": "ORCL1"},
        "source_snapshot": {"path": str(node1_snapshot_path), "sha256": node1_snapshot_sha},
        "backup": {
            "root": str(backup_root),
            "checksum_manifest": {"path": str(checksum_manifest), "sha256": _sha256_file(checksum_manifest)},
            "files": [],
            "oracle_home_archive": str(backup_piece),
            "rman_backup_set_keys": [1],
            "selected_recovery_set": {
                "observed_at": now_iso,
                "oldest_datafile_backup_completed_at": now_iso,
                "age_seconds_at_collection": 0,
                "restore_piece_handles": [str(backup_piece)],
                "datafile_backup_sets": [1],
            },
            "coverage": {
                "datafiles_backed": 1, "base_datafiles": 1, "datafiles_current": 1,
                "controlfile_records": 1, "spfile_records": 1, "outside_root_pieces": 0, "unavailable_pieces": 0,
            },
        },
        "verification": {"rman_log": {"path": str(rman_log), "sha256": _sha256_file(rman_log)}},
    }
    recovery = {**recovery_body, "record_sha256": _jq_canonical_hash(recovery_body)}
    recovery_path = base_dir / "recovery.json"
    recovery_path.write_text(json.dumps(recovery, indent=2))

    home_platform = {"status": "collected", "id": "226", "name": "Linux x86-64", "source": "opatch_lsinventory_xml"}
    reconciliation = {
        "schema_version": "1.0",
        "status": "consistent",
        "expected_nodes": ["node1", "node2"],
        "cluster": {"grid_home": str(grid_home), "runtime": {"status": "healthy", "active_version": "19.0.0.0.0", "upgrade_state": "NORMAL", "active_patch_level": "1"}},
        "oracle_homes": [
            {"path": str(grid_home), "owner": owner, "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51", "platform": home_platform, "patches": []},
            {"path": str(oracle_home), "owner": owner, "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51", "platform": home_platform, "patches": []},
        ],
        "databases": [{"db_unique_name": "ORCL", "oracle_home": str(oracle_home)}],
        "snapshot_evidence": [{"path": str(path), "sha256": sha} for path, sha in snapshots.values()],
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
            "target": {"family": "database", "method": "opatch", "topology": "rac", "database_unique_name": "ORCL", "platform_id": "226"},
            "execution": {"adapter": "database_rolling_opatch", "operations": ["database_stop_instance", "database_opatch_apply", "database_start_instance", "database_datapatch"]},
            "required_opatch_version": "12.2.0.1.49",
            "oracle_references": [{"kind": "patch_readme", "identifier": "test RAC README", "sha256": readme_sha}],
            "rollback": {"mode": "opatch_rollback", "precondition": "separate approved recovery plan"},
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
        "checks": [_compatibility_check(node, oracle_home, "oracle", artifact_sha) for node in ("node1", "node2")],
    }
    compatibility_path = base_dir / "compatibility.json"
    compatibility_path.write_text(json.dumps(compatibility, indent=2))

    policy_path = base_dir / "policy.json"
    policy_path.write_text(json.dumps(_cluster_policy(), indent=2))

    evidence_paths = {
        "readiness": base_dir / "readiness.json", "reconciliation": reconciliation_path, "artifact": artifact_json,
        "procedure": procedure_path, "compatibility": compatibility_path, "policy": policy_path, "recovery": recovery_path,
    }
    readiness = _cluster_readiness("database", now_iso, valid_until, snapshots, evidence_paths)
    evidence_paths["readiness"].write_text(json.dumps(readiness, indent=2))

    env = {
        "OPU_RAC_DATABASE_STATE_DIR": str(base_dir / "execution-state"),
        "OPU_RAC_DATABASE_ROLLBACK_STATE_DIR": str(base_dir / "rollback-state"),
        "OPU_RAC_DATABASE_TEST_MODE": "1",
        "OPU_RAC_DATABASE_TEST_OWNER": owner,
        "OPU_RAC_DATABASE_WAIT_ATTEMPTS": "2",
        "OPU_TEST_RAC_RUNTIME": str(runtime),
        "OPU_TEST_RAC_DB_PATH": str(oracle_home),
    }
    (base_dir / "env.json").write_text(json.dumps(env, indent=2))
    return {"evidence": evidence_paths, "env": env}


def build_grid(base_dir: Path) -> dict:
    """Builds a fresh two-node Grid Infrastructure TEST_MODE fixture under
    base_dir, mirroring tests/grid_node_patch.sh. Returns the same shape as
    build()."""
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)

    # Two levels below base_dir so _fixture_dir_for_plan resolves the fixture
    # root from the sealed grid_home exactly like it does for oracle_home.
    grid_home = base_dir / "grid" / "home"
    patch_dir = base_dir / "patch" / "39034528"
    recovery_root = base_dir / "recovery"
    runtime = base_dir / "runtime"
    for d in (
        grid_home / "bin", grid_home / "OPatch", grid_home / "jdk" / "bin",
        grid_home / "crs" / "install", grid_home / "rdbms" / "install",
        patch_dir / "etc" / "config", recovery_root, runtime,
    ):
        d.mkdir(parents=True, exist_ok=True)

    _copy_shims((
        ("grid_crsctl.sh", grid_home / "bin" / "crsctl"),
        ("grid_ocrcheck.sh", grid_home / "bin" / "ocrcheck"),
        ("grid_srvctl.sh", grid_home / "bin" / "srvctl"),
        ("grid_olsnodes.sh", grid_home / "bin" / "olsnodes"),
        ("grid_rootcrs.sh", grid_home / "crs" / "install" / "rootcrs.sh"),
        ("grid_rootadd_rdbms.sh", grid_home / "rdbms" / "install" / "rootadd_rdbms.sh"),
        ("grid_opatch.sh", grid_home / "OPatch" / "opatch"),
        ("java.sh", grid_home / "jdk" / "bin" / "java"),
    ))

    (patch_dir / "etc" / "config" / "actions.xml").write_text('<patch patchID="39034528"><actions/></patch>\n')
    (patch_dir / "etc" / "config" / "inventory.xml").write_text(
        '<patch patchID="39034528"><description>Grid Release Update</description>'
        '<os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n'
    )
    readme = patch_dir / "README.txt"
    readme.write_text(
        "Grid rolling patch test README\n"
        "rootcrs.sh -prepatch\n"
        "opatch apply\n"
        "rootadd_rdbms.sh\n"
        "rootcrs.sh -postpatch\n"
        "opatch rollback -id 39034528\n"
    )

    artifact_json = base_dir / "artifact.json"
    _run_tool("opu-artifact-inspect", ["--artifact", str(patch_dir), "--output", str(artifact_json)])
    artifact_sha = json.loads(artifact_json.read_text())["artifact"]["sha256"]
    readme_sha = _sha256_file(readme)
    owner = _owner()
    now_epoch = _now_epoch()
    now_iso = _iso_from_epoch(now_epoch)
    valid_until = _iso_from_epoch(now_epoch + 3600)
    snapshots = _write_cluster_snapshots(base_dir, now_iso)

    asset_names = (
        "snapshot.json", "bundle.json", "grid-home.tar.gz", "inventory.tar.gz", "oraInst.loc", "ocr.backup",
        "node1.olr", "node2.olr", "checksum.log", "grid-archive.log", "inventory-archive.log", "ocr-backup.log",
        "crs-health.log", "active-version.log", "ocrcheck.log", "voting-disks.log", "node1-olr.log", "node2-olr.log",
    )
    for asset_name in asset_names:
        (recovery_root / asset_name).write_text("sealed Grid recovery evidence\n")
    asset_sha = _sha256_file(recovery_root / "grid-home.tar.gz")
    checksum_manifest = recovery_root / "SHA256SUMS"
    checksum_manifest.write_text("".join(
        f"{_sha256_file(recovery_root / name)}  {recovery_root / name}\n"
        for name in ("grid-home.tar.gz", "inventory.tar.gz", "oraInst.loc", "ocr.backup", "node1.olr", "node2.olr")
    ))

    def asset(name: str) -> dict:
        return {"path": str(recovery_root / name), "sha256": asset_sha}

    recovery_body = {
        "schema_version": "1.0",
        "collector": {"name": "oracle.grid.recovery.evidence", "version": "1"},
        "status": "passed",
        "collected_at": now_iso,
        "target": {"grid_home": str(grid_home), "owner": owner, "nodes": ["node1", "node2"], "central_inventory": str(recovery_root), "oraInst_loc": str(recovery_root / "oraInst.loc")},
        "source_snapshot": asset("snapshot.json"),
        "source_bundle": {**asset("bundle.json"), "record_sha256": asset_sha},
        "backup": {
            "checksum_manifest": {"path": str(checksum_manifest), "sha256": _sha256_file(checksum_manifest)},
            "grid_home_archive": asset("grid-home.tar.gz"),
            "central_inventory_archive": asset("inventory.tar.gz"),
            "oraInst_loc": asset("oraInst.loc"),
            "ocr_backup": asset("ocr.backup"),
            "olr_backups": [{"node": "node1", **asset("node1.olr")}, {"node": "node2", **asset("node2.olr")}],
        },
        "verification": {
            "checksum_log": asset("checksum.log"),
            "grid_home_archive_log": asset("grid-archive.log"),
            "central_inventory_archive_log": asset("inventory-archive.log"),
            "ocr_backup_log": asset("ocr-backup.log"),
            "crs_health_log": asset("crs-health.log"),
            "active_version_log": asset("active-version.log"),
            "ocrcheck_log": asset("ocrcheck.log"),
            "voting_disks_log": asset("voting-disks.log"),
            "olr_backup_logs": [{"node": "node1", **asset("node1-olr.log")}, {"node": "node2", **asset("node2-olr.log")}],
        },
    }
    recovery = {**recovery_body, "record_sha256": _jq_canonical_hash(recovery_body)}
    recovery_path = base_dir / "recovery.json"
    recovery_path.write_text(json.dumps(recovery, indent=2))

    home_platform = {"status": "collected", "id": "226", "name": "Linux x86-64", "source": "opatch_lsinventory_xml"}
    reconciliation = {
        "schema_version": "1.0",
        "status": "consistent",
        "expected_nodes": ["node1", "node2"],
        "cluster": {"grid_home": str(grid_home), "runtime": {"status": "healthy", "active_version": "19.0.0.0.0", "upgrade_state": "NORMAL", "active_patch_level": "1"}},
        "oracle_homes": [{"path": str(grid_home), "owner": owner, "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51", "platform": home_platform, "patches": []}],
        "databases": [],
        "snapshot_evidence": [{"path": str(path), "sha256": sha} for path, sha in snapshots.values()],
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
            "target": {"family": "grid", "method": "opatch", "topology": "grid_rolling", "platform_id": "226"},
            "execution": {"adapter": "grid_rolling_opatch", "operations": ["grid_rootcrs_prepatch", "grid_opatch_apply", "grid_rootadd_rdbms", "grid_rootcrs_postpatch"]},
            "required_opatch_version": "12.2.0.1.49",
            "oracle_references": [{"kind": "patch_readme", "identifier": "test Grid README", "sha256": readme_sha}],
            "rollback": {"mode": "opatch_rollback", "precondition": "separate approved recovery plan"},
        },
    }
    procedure_path = base_dir / "procedure.json"
    procedure_path.write_text(json.dumps(procedure, indent=2))

    compatibility = {
        "schema_version": "1.0",
        "status": "passed",
        "patch_id": "39034528",
        "artifact_sha256": artifact_sha,
        "target": {"family": "grid", "platform_id": "226"},
        "checks": [_compatibility_check(node, grid_home, "grid", artifact_sha) for node in ("node1", "node2")],
    }
    compatibility_path = base_dir / "compatibility.json"
    compatibility_path.write_text(json.dumps(compatibility, indent=2))

    policy_path = base_dir / "policy.json"
    policy_path.write_text(json.dumps(_cluster_policy(), indent=2))

    evidence_paths = {
        "readiness": base_dir / "readiness.json", "reconciliation": reconciliation_path, "artifact": artifact_json,
        "procedure": procedure_path, "compatibility": compatibility_path, "policy": policy_path, "recovery": recovery_path,
    }
    readiness = _cluster_readiness("grid", now_iso, valid_until, snapshots, evidence_paths)
    evidence_paths["readiness"].write_text(json.dumps(readiness, indent=2))

    env = {
        "OPU_GRID_NODE_STATE_DIR": str(base_dir / "execution-state"),
        "OPU_GRID_NODE_TEST_MODE": "1",
        "OPU_GRID_NODE_TEST_OWNER": owner,
        "OPU_TEST_GRID_RUNTIME": str(runtime),
    }
    (base_dir / "env.json").write_text(json.dumps(env, indent=2))
    return {"evidence": evidence_paths, "env": env}


def env_for(base_dir: Path) -> dict:
    """Reloads the executor env vars for an already-built fixture, so a plan
    created earlier can have its tasks executed across multiple calls
    without rebuilding (and thereby resetting) the fixture's state."""
    env_path = base_dir / "env.json"
    if not env_path.is_file():
        raise FixtureError(f"No fixture env recorded at {env_path}")
    return json.loads(env_path.read_text())
