#!/usr/bin/env python3
"""Adapter final validation requires the latest SQL action, not historical success."""
import json
from pathlib import Path
import subprocess
import stat
import shlex
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(script, name):
    start = script.index(name + "() {")
    line_end = script.index("\n", start)
    if script[start:line_end].endswith(" }"):
        return script[start:line_end]
    return script[start:script.index("\n}", start) + 2]


DOUBLES = r'''
load_binding() { return 0; }
require_marker() { return 0; }
oratab_home_for_sid() { printf '%s' "$CLONE_HOME"; }
inventory_has_patch() { [ "$1" != "$ORACLE_HOME_TARGET" ]; }
wait_for_listener_ready() { return 0; }
current_health() { printf 'INVALID_OBJECTS=0\n' >"${!#}"; }
die() { echo "$*" >&2; exit 65; }
sqlplus_fixed() {
    local input
    input=$(cat)
    if [[ "$input" == *SQLPATCH_LATEST_ACTION* ]]; then
        [ "$QUERY_RC" = 0 ] || return "$QUERY_RC"
        printf 'SQLPATCH_LATEST_ACTION=%s\nSQLPATCH_LATEST_STATUS=%s\n' "$LATEST_ACTION" "$LATEST_STATUS"
    else
        # A prior APPLY succeeded and was then rolled back. Historical counts
        # alone incorrectly accept this as a successfully applied SQL patch.
        printf 'SQLPATCH_SUCCESS=1\nSQLPATCH_NON_SUCCESS=0\n'
    fi
}
'''

with tempfile.TemporaryDirectory(prefix="opu-postconditions-") as temporary:
    directory = Path(temporary)
    policy = directory / "policy.json"
    policy.write_text(json.dumps({"database": {"maximum_invalid_objects": 0}}))
    (directory / "plan.json").write_text(json.dumps({"source_documents": {"policy": {"path": str(policy)}}}))
    for tool, stage in (("opu-database-ojvm-patch", "stage_ojvm_final_validate"),
                        ("opu-database-out-of-place-patch", "stage_oop_final_validate")):
        script = (ROOT / "bin" / tool).read_text()
        definitions = "\n".join(function(script, name) for name in ("probe_value", "sqlpatch_latest", "require_latest_sqlpatch", stage))
        command = "set -e; . " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; " + definitions + "\n" + DOUBLES
        command += '\nTASK_DIR="$1"; PLAN_FILE="$1/plan.json"; CLONE_HOME=/fixture/clone; ORACLE_HOME_TARGET=/fixture/original; ORACLE_SID=fixture; PATCH_ID=87654321; LATEST_ACTION="$2"; LATEST_STATUS="$3"; QUERY_RC="$4"; ' + stage
        for action, status, query_rc, succeeds in (("ROLLBACK", "SUCCESS", 0, False), ("APPLY", "WITH ERRORS", 0, False),
                                                  ("APPLY", "SUCCESS", 1, False), ("APPLY", "SUCCESS", 0, True)):
            result = subprocess.run(["bash", "-c", command, "fixture", temporary, action, status, str(query_rc)],
                                    capture_output=True, text=True, timeout=10)
            assert (result.returncode == 0) == succeeds, (tool, action, status, query_rc, result.returncode, result.stderr)

    # A home switch replaces the oratab inode atomically without removing the
    # original Oracle-readable permissions or changing its owner/group.
    oratab = directory / "oratab"
    oratab.write_text("# keep this comment\nDB1:/old/home:Y\nDB2:/other/home:N\n")
    oratab.chmod(0o640)
    before = oratab.stat()
    script = (ROOT / "bin/opu-database-out-of-place-patch").read_text()
    command = "set -e; umask 077; " + "\n".join(function(script, name) for name in ("rewrite_oratab", "oratab_home_for_sid"))
    command += '\ndie() { echo "$*" >&2; exit 65; }; ORATAB="$1"; rewrite_oratab DB1 /new/home'
    result = subprocess.run(["bash", "-c", command, "fixture", str(oratab)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert oratab.read_text() == "# keep this comment\nDB1:/new/home:Y\nDB2:/other/home:N\n"
    after = oratab.stat()
    assert (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) == (stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid)

    # srvctl status can successfully report a stopped resource. Mixed positive
    # and negative text, or "not running", must fail the real Grid health gate.
    grid = directory / "grid"
    (grid / "bin").mkdir(parents=True)
    (grid / "bin" / "crsctl").write_text("#!/bin/sh\nprintf 'ONLINE\\n'\n")
    (grid / "bin" / "ocrcheck").write_text("#!/bin/sh\nexit 0\n")
    (grid / "bin" / "srvctl").write_text('#!/bin/sh\nif [ "$2" = asm ]; then printf "%s\\n" "$ASM_STATUS"; else printf "%s\\n" "$LISTENER_STATUS"; fi\n')
    for executable in (grid / "bin").iterdir():
        executable.chmod(0o700)
    for tool in ("opu-grid-node-patch", "opu-grid-opatchauto-patch"):
        script = (ROOT / "bin" / tool).read_text()
        command = "set -e; . " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; " + function(script, "check_cluster_health")
        command += '\nas_grid() { "$@"; }; die() { echo "$*" >&2; exit 65; }; GRID_HOME_TARGET="$1/grid"; LOCAL_NODE=fixture; export ASM_STATUS="$2" LISTENER_STATUS="$3"; check_cluster_health "$1/health"'
        for asm_status, listener_status, succeeds in (("ASM is running", "Listener is running", True),
                                                       ("ASM is running on failover-node", "Listener LISTENER_FAILOVER is running", True),
                                                       ("ASM is not running", "Listener is running", False),
                                                       ("ASM is running", "Listener is not running", False),
                                                       ("ASM is online", "Listener A is running\nListener B is offline", False)):
            result = subprocess.run(["bash", "-c", command, "fixture", temporary, asm_status, listener_status],
                                    capture_output=True, text=True, timeout=10)
            assert (result.returncode == 0) == succeeds, (tool, asm_status, listener_status, result.returncode, result.stderr)

    # The real rollback supervisor must preserve a failed datapatch invocation,
    # even when a subsequent SQL probe could otherwise report prior success.
    script = (ROOT / "bin/opu-database-rac-node-rollback").read_text()
    controller = directory / "controller"
    controller.write_text("#!/bin/sh\nprintf '{}\\n'\n")
    controller.chmod(0o700)
    command = ("set -e; . " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; . "
               + shlex.quote(str(ROOT / "lib/opu/execution.sh")) + "; "
               + "\n".join(function(script, name) for name in ("execute_task", "stage_rac_rollback_datapatch"))) + r'''
require_root() { :; }; opu_execution_host_lock() { :; }; load_plan() { :; }
verify_source_apply_lineage() { :; }; verify_source_documents() { :; }; verify_recovery() { :; }
node_key() { printf '%s' "$1"; }; current_short_host() { printf fixture; }
opu_execution_prepare_attempt() { TASK_DIR="$1/$3"; }
acquire_executor_lock() { :; }; load_task_after_claim() { LOCAL_NODE=fixture; TASK_FILE=fixture; }
opu_execution_verify_attempt() { :; }; opu_now_utc() { printf fixture; }; close_inherited_executor_lock() { :; }
stage_success_outcome() { printf binary_state_known; }; derive_outcome() { printf binary_state_known; }
emit_evidence() { printf '{"status":"%s","exit_code":%s}\n' "$1" "$3" >"$TASK_DIR/evidence.json"; }
load_binding() { :; }; verify_open_window() { :; }; verify_prior_node_validations() { :; }
verify_all_nodes_running() { :; }; verify_services_available() { :; }; require_inventory_state() { :; }
database_probe() { :; }; healthy_probe() { :; }
as_owner() { return 42; }
require_sqlpatch_rollback_success() { : >"$TASK_DIR/probe-after-failed-command"; }
run_typed_stage() { stage_rac_rollback_datapatch; }
die() { echo "$*" >&2; exit 65; }
EXECUTION_STATE_DIR="$1/rollback-state"; PLAN_STATE_DIR="$1/plans"; PLAN_TOOL="$1/controller"
PLAN_ID=fixture-plan; TASK_ID=fixture-task; ACTOR=worker; LEASE_SECONDS=30
STAGE=rac_rollback_datapatch; ORACLE_SID=DB1; ORACLE_OWNER=oracle; ORACLE_HOME_TARGET=/fixture/home
execute_task
'''
    result = subprocess.run(["bash", "-c", command, "fixture", temporary], capture_output=True, text=True, timeout=10)
    task = directory / "rollback-state/plans/fixture-plan/nodes/fixture/tasks/fixture-task"
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert (task / "evidence.json").is_file(), (result.returncode, result.stdout, result.stderr)
    assert json.loads((task / "evidence.json").read_text()) == {"status": "failed", "exit_code": 42}
    assert not (task / "probe-after-failed-command").exists()

    # Manual rolling rollback must remain on the claimed node and must not ask
    # an unattended worker interactive questions.
    script = (ROOT / "bin/opu-grid-node-patch").read_text()
    command = "set -e; " + function(script, "stage_grid_opatch_rollback") + r'''
load_binding() { :; }; verify_open_window() { :; }; verify_source_apply_lineage() { :; }
verify_rollback_readme() { :; }; verify_recovery() { :; }; verify_artifact() { :; }
require_inventory_state() { :; }; opatch_prereq_check() { :; }
opatch_run() { printf '%s\n' "$@" >"$TASK_DIR/rollback-arguments"; }
TASK_DIR="$1"; NODE_RUN_DIR="$1"; GRID_HOME_TARGET='/fixture/grid home'; PATCH_ID=87654321
: >"$NODE_RUN_DIR/rollback-prepatch-complete"
stage_grid_opatch_rollback
'''
    result = subprocess.run(["bash", "-c", command, "fixture", temporary], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (directory / "rollback-arguments").read_text().splitlines() == [
        "rollback", "-id", "87654321", "-silent", "-local", "-oh", "/fixture/grid home"]

print("native postconditions: SQL action, oratab metadata, Grid resource health and rollback scope passed")
