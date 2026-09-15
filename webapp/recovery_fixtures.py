"""TEST_MODE fixture harness for opu-database-recovery-prepare.

Shim scripts alongside this file (sqlplus.sh, rman.sh, lsnrctl.sh,
fake-topology.sh) model the constrained responses in tests/recovery_prepare.sh.
tests/recovery_demo_fixture.py exercises the app-built files through native
analysis and execution so missing probe responses or fixture files fail tests.

Builds a self-contained fake standalone Oracle home plus a matching topology
snapshot and policy document, and returns the environment variables needed to
run bin/opu-database-recovery-prepare's full create/analyze/approve/
authorize/execute cycle against it — real RMAN-command generation, real
opu-recovery-evidence-collect invocation, no real Oracle software involved.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
SHIMS_DIR = Path(__file__).resolve().parent / "recovery_fixtures"


class FixtureError(Exception):
    pass


def build(base_dir: Path, request_id: str) -> dict:
    """Builds a fresh recovery-prepare fixture under base_dir for one request_id.

    Returns {"snapshot": path, "policy": path, "backup_parent": path,
    "window_start": iso, "window_end": iso, "env": {...}}.
    """
    # Never replace an existing request: it may contain the only recovery
    # evidence. mkdir(exist_ok=False) also makes concurrent creates exclusive.
    if base_dir.exists() or base_dir.is_symlink():
        raise FixtureError(f"Recovery fixture already exists: {base_dir}")
    base_dir.mkdir(parents=True, exist_ok=False)

    oracle_home = base_dir / "oracle" / "dbhome_1"
    inventory = base_dir / "oraInventory"
    backup_parent = base_dir / "backups"
    runtime = base_dir / "runtime"
    state_root = base_dir / "state"
    for d in (oracle_home / "bin", oracle_home / "OPatch", inventory, backup_parent, runtime / "oradata"):
        d.mkdir(parents=True, exist_ok=True)
    backup_parent_canonical = backup_parent.resolve()

    owner = pwd.getpwuid(os.getuid()).pw_name

    (runtime / "database.state").write_text("OPEN\n")
    # The SQL probe reports these files. Native capacity analysis must measure
    # real fixture files rather than depend on an unrelated path under /tmp.
    (runtime / "spfileORCL.ora").write_text("fixture SPFILE\n")
    (runtime / "oradata" / "system01.dbf").write_bytes(b"D" * 1024)
    (oracle_home / "oraInst.loc").write_text(f"inventory_loc={inventory}\ninst_group=oinstall\n")
    (inventory / "ContentsXML").write_text("inventory content\n")
    (oracle_home / "bin" / "oracle").write_text("home content\n")

    for shim, target in (
        ("sqlplus.sh", oracle_home / "bin" / "sqlplus"),
        ("rman.sh", oracle_home / "bin" / "rman"),
        ("lsnrctl.sh", oracle_home / "bin" / "lsnrctl"),
    ):
        shutil.copy(SHIMS_DIR / shim, target)
        target.chmod(0o750)

    fake_topology = runtime / "fake-topology"
    shutil.copy(SHIMS_DIR / "fake-topology.sh", fake_topology)
    fake_topology.chmod(0o750)

    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now_iso = now.strftime(fmt)
    window_start = (now - timedelta(seconds=60)).strftime(fmt)
    window_end = (now + timedelta(seconds=1800)).strftime(fmt)

    snapshot = {
        "schema_version": "1.0",
        "collector": {"name": "oracle.topology.discover", "version": "1"},
        "collected_at": now_iso,
        "host": {"name": "standalone.example"},
        "cluster": {"status": "unavailable", "grid_home": None, "runtime": {"status": "unavailable"}, "nodes": []},
        "oracle_homes": [{
            "path": str(oracle_home), "owner": owner, "version": "19.0.0.0.0", "opatch_version": "12.2.0.1.51",
            "platform": {
                "status": "collected", "id": "226", "name": "Linux x86-64", "source": "opatch_lsinventory_xml",
                "source_sha256": "a" * 64,
            },
            "patches": [],
        }],
        "databases": [{
            "db_unique_name": "ORCL", "oracle_home": str(oracle_home),
            "runtime": {
                "status": "complete", "instance": "ORCL", "database_role": "PRIMARY",
                "open_mode": "READ WRITE", "log_mode": "NOARCHIVELOG", "instance_state": "OPEN",
            },
        }],
        "warnings": [],
    }
    snapshot_path = base_dir / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2))

    policy = {
        "schema_version": "1.0",
        "maximum_snapshot_age_seconds": 3600,
        "recovery": {"require_backup": True, "max_backup_age_minutes": 1440},
    }
    policy_path = base_dir / "policy.json"
    policy_path.write_text(json.dumps(policy, indent=2))

    env = {
        "OPU_RECOVERY_PREP_STATE_DIR": str(state_root),
        "OPU_RECOVERY_PREP_TEST_ALLOW_NONROOT": "1",
        "OPU_RECOVERY_PREP_TEST_MODE": "1",
        "OPU_TEST_MODE": "1",
        "OPU_RECOVERY_PREP_TEST_LISTENER": "LISTENER",
        "OPU_RECOVERY_PREP_TEST_TOPOLOGY_TOOL": str(fake_topology),
        "OPU_RECOVERY_TEST_ALLOW_NONROOT": "1",
        "OPU_TEST_RUNTIME": str(runtime),
        "OPU_TEST_SUCCESS_ROOT": str(backup_parent_canonical / request_id),
        "OPU_TEST_SNAPSHOT": str(snapshot_path),
    }
    (base_dir / "env.json").write_text(json.dumps(env, indent=2))

    return {
        "snapshot": snapshot_path,
        "policy": policy_path,
        "backup_parent": backup_parent,
        "state_root": state_root,
        "window_start": window_start,
        "window_end": window_end,
        "env": env,
    }


def env_for(base_dir: Path) -> dict:
    """Reloads the executor env vars for an already-built fixture."""
    env_path = base_dir / "env.json"
    if not env_path.is_file():
        raise FixtureError(f"No fixture env recorded at {env_path}")
    return json.loads(env_path.read_text())
