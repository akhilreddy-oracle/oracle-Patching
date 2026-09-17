#!/usr/bin/env python3
"""Offline reproductions of native defects found by the second code review."""
import json
import hashlib
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    start = source.index(name + "() {")
    end = source.index("\n", start)
    return source[start:end] if source[start:end].endswith(" }") else source[start:source.index("\n}", start) + 2]


class NativeSecondReviewTests(unittest.TestCase):
    def test_ojvm_and_home_switch_datapatch_use_native_inventory_selection(self):
        for name in ("ojvm", "out-of-place"):
            source = (ROOT / ("bin/opu-database-" + name + "-patch")).read_text()
            for action in ("apply", "rollback"):
                with self.subTest(adapter=name, action=action), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    (base / "OPatch").mkdir()
                    tool = base / "OPatch/datapatch"
                    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$ORACLE_HOME/arguments"\n')
                    tool.chmod(0o700)
                    command = "set -eu; " + function(source, "datapatch_run")
                    command += '\nas_owner() { shift; "$@"; }; die() { exit 65; }; ORACLE_HOME_TARGET="$1"; ORACLE_OWNER=fixture; ORACLE_SID=fixture; PATCH_ID=87654321; '
                    command += 'datapatch_run "$1" "$2"' if name == "out-of-place" else 'datapatch_run "$2"'
                    result = subprocess.run(["bash", "-c", command, "fixture", temporary, action], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((base / "arguments").read_text().splitlines(), ["-verbose"])

    def test_all_database_adapters_require_known_non_cdb_health(self):
        for name in ("single-instance-patch", "ojvm-patch", "out-of-place-patch", "rac-node-patch", "rac-node-rollback"):
            source = (ROOT / ("bin/opu-database-" + name)).read_text()
            probe = function(source, "database_probe")
            self.assertIn("select 'CDB=' || cdb from v$database;", probe)
            for health in ("healthy_probe", "upgrade_probe") if name == "ojvm-patch" else ("healthy_probe",):
                definitions = "\n".join(function(source, func) for func in ("probe_value", health))
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "probe.log"
                    for scope in ("NO", "YES", "", "NO\nCDB=YES", "NO\nCDB=NO"):
                        with self.subTest(adapter=name, health=health, scope=scope):
                            path.write_text("DATABASE_UNIQUE_NAME=fixture\nINSTANCE_NAME=fixture\nINSTANCE_STATUS=" + ("OPEN MIGRATE" if health == "upgrade_probe" else "OPEN") + "\nDATABASE_ROLE=PRIMARY\nOPEN_MODE=READ WRITE\n" + ("CDB=" + scope + "\n" if scope else ""))
                            command = "set -eu; . " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; " + definitions
                            command += '\nDATABASE=fixture; ' + health + ' "$1" fixture'
                            result = subprocess.run(["bash", "-c", command, "fixture", str(path)], capture_output=True, text=True, timeout=10)
                            self.assertEqual(result.returncode == 0, scope == "NO", result.stderr)
                            if scope != "NO":
                                self.assertIn("non-CDB databases only", result.stderr)

    def test_compatibility_keeps_distinct_home_logs_and_unknown_version_records(self):
        source = (ROOT / "bin/opu-opatch-compatibility-collect").read_text()
        definitions = "\n".join(function(source, name) for name in ("check_home", "version_ge", "prereq_detail", "emit"))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "snapshot.json").write_text(json.dumps({"host": {"name": "fixture.example"}}))
            for name in ("one", "two", "bad"):
                path = base / name / "dbhome/OPatch/opatch"
                path.parent.mkdir(parents=True)
                path.write_text("fixture")
                path.chmod(0o700)
            command = "set -eu; . " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; " + definitions + r'''
declare -a CHECKS=() FINDINGS=()
as_owner() { return 0; }; artifact_payload_ok() { return 0; }
opatch_as_owner() { if [ "$3" = version ]; then printf 'OPatch Version: %s\n' "$ACTUAL_VERSION"; else printf '%s %s\n' "$2" "$4"; fi; }
SNAPSHOT="$1/snapshot.json"; EVIDENCE_DIR="$1"; ARTIFACT="$1/media"; ARTIFACT_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
PATCH_ID=87654321; REQUIRED_OPATCH=12.2.0.1.49; TARGET_FAMILY=database; ARTIFACT_PLATFORM=226; PROCEDURE_PLATFORM=226; ACTUAL_VERSION=12.2.0.1.51
check_home "$1/one/dbhome" owner 226 Linux
check_home "$1/two/dbhome" owner 226 Linux
check_home "$1/missing/dbhome" unknown 226 Linux
ACTUAL_VERSION=999garbage
check_home "$1/bad/dbhome" owner 226 Linux
emit "$1/result.json"
'''
            result = subprocess.run(["bash", "-c", command, "fixture", temporary], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((base / "result.json").read_text())
            self.assertEqual(len(record["checks"]), 4)
            paths = [check["applicability_check"]["evidence_path"] for check in record["checks"][:3]]
            self.assertEqual(len(set(paths)), 3, "distinct full home paths must not overwrite shared-basename evidence")
            self.assertNotIn("actual_version", record["checks"][2]["opatch"])
            self.assertEqual(record["checks"][2]["status"], "blocked")
            self.assertEqual(record["checks"][3]["opatch"]["status"], "failed")
            for check in record["checks"]:
                for key in ("applicability_check", "conflict_check"):
                    evidence = check[key]
                    self.assertEqual(hashlib.sha256(Path(evidence["evidence_path"]).read_bytes()).hexdigest(), evidence["evidence_sha256"])

    def test_rac_rollback_accepts_real_timestamped_metadata_only(self):
        source = (ROOT / "bin/opu-database-rac-node-rollback").read_text()
        declarations = "\n".join(function(source, name) for name in ("version_ge", "stage_rac_rollback_precheck"))
        doubles = "\n".join(f"{name}() {{ :; }}" for name in (
            "verify_source_apply_lineage", "verify_source_documents", "verify_recovery", "verify_artifact", "verify_readme_procedure",
            "discover_and_bind", "reject_shared_oracle_home", "require_inventory_state", "require_sqlpatch_apply_success",
            "require_surviving_instance", "verify_services_available"))
        command = "set -eu; " + declarations + "\n" + doubles + r'''
die() { echo "$*" >&2; exit 65; }
opatch_run() { printf 'OPatch Version: 12.2.0.1.51\n'; }
GRID_HOME_TARGET="$1/grid"; ORACLE_HOME_TARGET="$1/db"; TASK_DIR="$1";
PATCH_ID=87654321; TEST_MODE=0; REQUIRED_OPATCH=12.2.0.1.49
stage_rac_rollback_precheck
'''
        for entry, expected in (("87654321_Sep_17_2026_10_11_12", True), ("12345678_Sep_17_2026_10_11_12", False), (None, False), ("symlink", False)):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                for name in ("grid/bin/srvctl", "db/bin/sqlplus", "db/OPatch/opatch", "db/OPatch/datapatch"):
                    path = base / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("#!/bin/sh\nexit 0\n")
                    path.chmod(0o700)
                storage = base / "db/.patch_storage"
                storage.mkdir()
                if entry == "symlink":
                    (storage / "87654321_Sep_17_2026_10_11_12").symlink_to(base / "grid", target_is_directory=True)
                elif entry:
                    (storage / entry).mkdir()
                result = subprocess.run(["bash", "-c", command, "fixture", temporary], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_grid_supervisor_emits_this_attempt_observation_not_baseline(self):
        for tool in ("opu-grid-node-patch", "opu-grid-opatchauto-patch"):
            source = (ROOT / "bin" / tool).read_text()
            names = ["collect_cluster_runtime", "execute_task"]
            if "load_task_cluster_runtime()" in source:
                names.append("load_task_cluster_runtime")
            definitions = "\n".join(function(source, name) for name in names)
            definitions += "\n" + function((ROOT / "lib/opu/execution.sh").read_text(), "opu_execution_prepare_attempt")
            for collect in ("1", "0", "fail"):
                with self.subTest(tool=tool, collect=collect), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    task = base / "plans/plans/fixture/tasks/final.json"
                    task.parent.mkdir(parents=True)
                    task.write_text('{"retry_count":1}')
                    old = base / "state/plans/fixture/tasks/final"
                    old.mkdir(parents=True)
                    (old / "cluster-runtime.json").write_text('{"active_version":"19.0.0.0.0","upgrade_state":"NORMAL","active_patch_level":"999","release_patch_level":"999"}')
                    (base / "grid/bin").mkdir(parents=True)
                    crsctl = base / "grid/bin/crsctl"
                    crsctl.write_text("#!/bin/sh\nif [ \"$3\" = activeversion ]; then printf 'Oracle Clusterware active version [19.0.0.0.0] upgrade state [NORMAL] active patch level [222]\\n'; else printf 'Oracle Clusterware release patch level [333]\\n'; fi\n")
                    crsctl.chmod(0o700)
                    controller = base / "controller"
                    controller.write_text("#!/bin/sh\nprintf '{}\\n'\n")
                    controller.chmod(0o700)
                    command = "set -eu; " + definitions + r'''
die() { echo "$*" >&2; exit 65; }
require_root() { :; }; opu_execution_host_lock() { :; }; load_plan() { :; }
load_task_after_claim() { LOCAL_NODE=fixture; TASK_FILE=fixture; }
opu_execution_verify_attempt() { :; }; close_inherited_executor_lock() { :; }
verify_sealed_json() { :; }; heartbeat() { :; }; opu_now_utc() { printf fixture; }
acquire_executor_lock() { printf '%s' '{"baseline":{"active_version":"18.0.0.0.0","upgrade_state":"NORMAL","active_patch_level":"111","release_patch_level":"111"}}' >"$BINDING_FILE"; }
run_typed_stage() { if [ "$COLLECT" = fail ]; then return 65; elif [ "$COLLECT" = 1 ]; then collect_cluster_runtime "$TASK_DIR/current"; fi; }
emit_evidence() { jq -n --arg active "${CLUSTER_ACTIVE_PATCH_LEVEL:-unknown}" --arg release "${CLUSTER_RELEASE_PATCH_LEVEL:-unknown}" '{active_patch_level:$active,release_patch_level:$release}' >"$TASK_DIR/evidence.json"; }
EXECUTION_STATE_DIR="$1/state"; PLAN_STATE_DIR="$1/plans"; PLAN_TOOL="$1/controller"; GRID_HOME_TARGET="$1/grid";
PLAN_ID=fixture; TASK_ID=final; ACTOR=worker; LEASE_SECONDS=30; OPERATION=apply; STAGE=final_validate; COLLECT="$2"
execute_task
'''
                    result = subprocess.run(["bash", "-c", command, "fixture", temporary, collect], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 65 if collect == "fail" else 0, result.stderr)
                    record = json.loads((base / "state/plans/fixture/tasks/final-retry1/evidence.json").read_text())
                    self.assertEqual(record, {"active_patch_level": "222" if collect == "1" else "unknown", "release_patch_level": "333" if collect == "1" else "unknown"})
                    self.assertEqual(json.loads((old / "cluster-runtime.json").read_text())["active_patch_level"], "999")


if __name__ == "__main__":
    unittest.main()
