#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-single-instance.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# macOS does not provide util-linux flock. Supply a narrow test double that
# acquires a real BSD flock on the inherited descriptor so this suite still
# exercises executor-lock descriptor inheritance exactly as Oracle Linux does.
if ! command -v flock >/dev/null 2>&1; then
  mkdir -p "$TMP/test-bin"
  FLOCK_PROBE="$TMP/flock-called"
  OPU_SINGLE_INSTANCE_TEST_FLOCK="$TMP/test-bin/flock"
  export FLOCK_PROBE OPU_SINGLE_INSTANCE_TEST_FLOCK
  cat >"$TMP/test-bin/flock" <<'PY'
#!/usr/bin/python3
import fcntl
import os
import sys

if sys.argv[1:] != ["-n", "8"]:
    raise SystemExit(64)

try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)

with open(os.environ["FLOCK_PROBE"], "a", encoding="utf-8") as marker:
    marker.write("acquired\n")
PY
  chmod 0750 "$TMP/test-bin/flock"
fi

PLAN_TOOL="$ROOT/bin/opu-patch-plan"
EXECUTOR="$ROOT/bin/opu-database-single-instance-patch"
PLAN_STATE="$TMP/plan-state"
EXECUTION_STATE="$TMP/execution-state"
ROLLBACK_STATE="$TMP/rollback-state"
TEST_HOME="$TMP/oracle/dbhome_1"
PATCH_DIR="$TMP/patch/39034528"
BACKUP_ROOT="$TMP/backup/ORCL/run-001"
PATCH_STATE="$TMP/patch.state"
DATABASE_STATE="$TMP/database.state"
LISTENER_STATE="$TMP/listener.state"
DATAPATCH_STATE="$TMP/datapatch.state"
FAIL_OPATCH="$TMP/fail-opatch"
FAIL_APPLICABILITY="$TMP/fail-applicability"
FAIL_DATAPATCH="$TMP/fail-datapatch"
FAIL_ROLLBACK="$TMP/fail-rollback"
SQLPATCH_ACTION_STATE="$TMP/sqlpatch-action.state"
OPATCH_CALLS="$TMP/opatch-calls.log"

mkdir -p "$TEST_HOME/bin" "$TEST_HOME/OPatch" "$TEST_HOME/jdk/bin" \
  "$PATCH_DIR/etc/config" "$BACKUP_ROOT/rman" "$BACKUP_ROOT/oracle-home"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"

cat >"$TEST_HOME/bin/sqlplus" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
if grep -q 'shutdown immediate' <<<"$input"; then
  [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
  printf 'down\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q '^startup;' <<<"$input"; then
  printf 'up\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q 'SQLPATCH_LATEST_ACTION=' <<<"$input"; then
  action=$(cat "$OPU_TEST_SQLPATCH_ACTION_STATE" 2>/dev/null || true)
  [ -n "$action" ] || exit 1
  printf '%s\n' "SQLPATCH_LATEST_ACTION=$action" 'SQLPATCH_LATEST_STATUS=SUCCESS'
elif grep -q 'SQLPATCH_SUCCESS=' <<<"$input"; then
  if [ -f "$OPU_TEST_DATAPATCH_STATE" ]; then
    printf '%s\n' 'SQLPATCH_SUCCESS=1' 'SQLPATCH_NON_SUCCESS=0'
  else
    printf '%s\n' 'SQLPATCH_SUCCESS=0' 'SQLPATCH_NON_SUCCESS=0'
  fi
elif grep -q 'alter system register' <<<"$input"; then
  :
else
  [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
  printf '%s\n' \
    'INSTANCE_NAME=ORCL' \
    'INSTANCE_STATUS=OPEN' \
    'DATABASE_UNIQUE_NAME=ORCL' \
    'DATABASE_ROLE=PRIMARY' \
    'OPEN_MODE=READ WRITE' \
    'LOG_MODE=ARCHIVELOG' \
    'INVALID_OBJECTS=0'
fi
EOF

cat >"$TEST_HOME/bin/lsnrctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  start) printf 'up\n' >"$OPU_TEST_LISTENER_STATE"; printf 'listener started\n' ;;
  stop) printf 'down\n' >"$OPU_TEST_LISTENER_STATE"; printf 'listener stopped\n' ;;
  status)
    [ "$(cat "$OPU_TEST_LISTENER_STATE")" = up ] || exit 1
    [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
    printf '%s\n' 'Instance "ORCL", status READY, has 1 handler(s) for this service...'
    ;;
  *) exit 64 ;;
esac
EOF

cat >"$TEST_HOME/OPatch/opatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$OPU_TEST_OPATCH_CALLS"
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$OPU_TEST_PATCH_STATE" ] && printf '%s\n' '39034528;Database Release Update' || true ;;
  prereq)
    case "${2:-}" in
      CheckPatchApplicableOnCurrentPlatform)
        [ ! -f "$OPU_TEST_FAIL_APPLICABILITY" ] || { printf 'Prerequisite check "CheckPatchApplicableOnCurrentPlatform" failed.\nOPatch failed with error code 73\n' >&2; exit 73; }
        printf '%s\n' 'Prereq "CheckPatchApplicableOnCurrentPlatform" passed.'
        ;;
      CheckConflictAgainstOHWithDetail) printf '%s\n' 'Prereq "CheckConflictAgainstOHWithDetail" passed.' ;;
      *) printf 'unsupported fake OPatch prerequisite: %s\n' "${2:-}" >&2; exit 64 ;;
    esac
    ;;
  apply)
    [ ! -f "$OPU_TEST_FAIL_OPATCH" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf '39034528\n' >"$OPU_TEST_PATCH_STATE"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    [ ! -f "$OPU_TEST_FAIL_ROLLBACK" ] || { printf 'simulated OPatch rollback failure\n' >&2; exit 73; }
    rm -f "$OPU_TEST_PATCH_STATE"
    printf '%s\n' 'OPatch rollback succeeded.'
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
EOF

cat >"$TEST_HOME/OPatch/datapatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ ! -f "$OPU_TEST_FAIL_DATAPATCH" ] || { printf 'simulated datapatch failure\n' >&2; exit 74; }
[ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ]
if [ -f "$OPU_TEST_PATCH_STATE" ]; then printf 'APPLY\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; else printf 'ROLLBACK\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; fi
printf 'complete\n' >"$OPU_TEST_DATAPATCH_STATE"
printf '%s\n' 'SQL Patching tool complete.'
EOF

cat >"$TEST_HOME/jdk/bin/java" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 750 "$TEST_HOME/bin/sqlplus" "$TEST_HOME/bin/lsnrctl" \
  "$TEST_HOME/OPatch/opatch" "$TEST_HOME/OPatch/datapatch" "$TEST_HOME/jdk/bin/java"

cat >"$PATCH_DIR/etc/config/inventory.xml" <<'EOF'
<patch patchID="39034528"><description>Database Release Update</description><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>
EOF
printf '%s\n' \
  'Database Release Update test README' \
  'opatch rollback -id 39034528' \
  'datapatch -verbose' >"$PATCH_DIR/README.txt"
"$ROOT/bin/opu-artifact-inspect" --artifact "$PATCH_DIR" --output "$TMP/artifact.json" >/dev/null
ARTIFACT_SHA=$(jq -r '.artifact.sha256' "$TMP/artifact.json")
README_SHA=$(sha256sum "$PATCH_DIR/README.txt" | awk '{print $1}')

printf 'backup piece\n' >"$BACKUP_ROOT/rman/piece-01"
printf 'Oracle home archive\n' >"$BACKUP_ROOT/oracle-home/dbhome.tar.gz"
sha256sum "$BACKUP_ROOT/rman/piece-01" "$BACKUP_ROOT/oracle-home/dbhome.tar.gz" >"$BACKUP_ROOT/SHA256SUMS"
printf '%s\n' 'RMAN validation complete' >"$TMP/rman-validate.log"
CHECKSUM_SHA=$(sha256sum "$BACKUP_ROOT/SHA256SUMS" | awk '{print $1}')
RMAN_SHA=$(sha256sum "$TMP/rman-validate.log" | awk '{print $1}')
OWNER=$(id -un)

jq -cn --arg database ORCL --arg oracle_home "$TEST_HOME" --arg owner "$OWNER" \
  --arg backup_root "$BACKUP_ROOT" --arg checksum_path "$BACKUP_ROOT/SHA256SUMS" \
  --arg checksum_sha "$CHECKSUM_SHA" --arg rman_log "$TMP/rman-validate.log" --arg rman_sha "$RMAN_SHA" \
  '{schema_version:"1.0",collector:{name:"oracle.recovery.evidence",version:"1"},status:"passed",target:{database_unique_name:$database,oracle_home:$oracle_home,owner:$owner,oracle_sid:"ORCL"},backup:{root:$backup_root,checksum_manifest:{path:$checksum_path,sha256:$checksum_sha},files:[],oracle_home_archive:($backup_root+"/oracle-home/dbhome.tar.gz"),rman_backup_set_keys:[1],selected_recovery_set:{observed_at:(now|todateiso8601),oldest_datafile_backup_completed_at:(now|todateiso8601),age_seconds_at_collection:0,restore_piece_handles:[($backup_root+"/backup-piece")],datafile_backup_sets:[1]},coverage:{datafiles_backed:1,base_datafiles:1,datafiles_current:1,controlfile_records:1,spfile_records:1,outside_root_pieces:0,unavailable_pieces:0}},verification:{rman_log:{path:$rman_log,sha256:$rman_sha}}}' >"$TMP/recovery.tmp"
RECOVERY_RECORD=$(jq -cS . "$TMP/recovery.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$RECOVERY_RECORD" '.record_sha256=$record' "$TMP/recovery.tmp" >"$TMP/recovery.json"

EVIDENCE_NOW=$(date -u +%s)
EVIDENCE_COLLECTED=$(date -u -r "$EVIDENCE_NOW" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$EVIDENCE_NOW" '+%Y-%m-%dT%H:%M:%SZ')
EVIDENCE_VALID=$(date -u -r "$((EVIDENCE_NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((EVIDENCE_NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ')
jq -n --arg collected "$EVIDENCE_COLLECTED" '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,host:{name:"testnode.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},oracle_homes:[],databases:[],warnings:[]}' >"$TMP/topology-snapshot.json"
SNAPSHOT_SHA=$(sha256sum "$TMP/topology-snapshot.json" | awk '{print $1}')
jq --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" '.source_snapshot={path:$snapshot,sha256:$snapshot_sha}' "$TMP/recovery.json" >"$TMP/recovery.with-snapshot.json"
RECOVERY_RECORD=$(jq -cS 'del(.record_sha256)' "$TMP/recovery.with-snapshot.json" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$RECOVERY_RECORD" '.record_sha256=$record' "$TMP/recovery.with-snapshot.json" >"$TMP/recovery.json"
jq -n --arg oracle_home "$TEST_HOME" --arg owner "$OWNER" --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["testnode"],oracle_homes:[{path:$oracle_home,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:$oracle_home}],snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha}]}' >"$TMP/reconciliation.json"
jq -n --arg artifact_sha "$ARTIFACT_SHA" --arg readme_sha "$README_SHA" \
  '{schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"39034528",artifact_sha256:$artifact_sha,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]},required_opatch_version:"12.2.0.1.49",oracle_references:[{kind:"patch_readme",identifier:"README rollback sections",sha256:$readme_sha}],rollback:{mode:"opatch_rollback",precondition:"separate approved rollback plan required"}}}' >"$TMP/procedure.json"
jq -n --arg home "$TEST_HOME" --arg digest "$ARTIFACT_SHA" '{schema_version:"1.0",status:"passed",patch_id:"39034528",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"testnode",home:$home,owner:"test",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.49",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}}]}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"

RECONCILIATION_SHA=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}')
ARTIFACT_MANIFEST_SHA=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
PROCEDURE_SHA=$(sha256sum "$TMP/procedure.json" | awk '{print $1}')
COMPATIBILITY_SHA=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
POLICY_SHA=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg reconciliation "$RECONCILIATION_SHA" --arg artifact "$ARTIFACT_MANIFEST_SHA" --arg procedure "$PROCEDURE_SHA" --arg compatibility "$COMPATIBILITY_SHA" --arg policy "$POLICY_SHA" \
  --arg evaluated "$EVIDENCE_COLLECTED" --arg valid "$EVIDENCE_VALID" --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"39034528",target:{family:"database",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha,host:"testnode",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$reconciliation,artifact_manifest_sha256:$artifact,procedure_validation_sha256:$procedure,compatibility_sha256:$compatibility,policy_sha256:$policy}}' >"$TMP/readiness.json"

NOW=$(date -u +%s)
WINDOW_START=$(date -u -r "$((NOW-60))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW-60))" '+%Y-%m-%dT%H:%M:%SZ')
WINDOW_END=$(date -u -r "$((NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ')
printf 'ORCL:%s:N\n' "$TEST_HOME" >"$TMP/oratab"

plan() { OPU_PLAN_STATE_DIR="$PLAN_STATE" "$PLAN_TOOL" "$@"; }
create_plan() {
  local plan_id=$1
  plan create --plan-id "$plan_id" --requester patch-admin \
    --readiness "$TMP/readiness.json" --reconciliation "$TMP/reconciliation.json" \
    --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" \
    --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" \
    --recovery-evidence "$TMP/recovery.json" --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$plan_id" --actor dba-approver --approval-ticket TEST-39034528 >/dev/null
  plan authorize --plan-id "$plan_id" --actor patch-operator >/dev/null
  plan dispatch --plan-id "$plan_id" --actor patch-operator >/dev/null
}

execute() {
  local plan_id=$1 task_id=$2
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_SINGLE_INSTANCE_STATE_DIR="$EXECUTION_STATE" \
  OPU_SINGLE_INSTANCE_TEST_MODE=1 \
  OPU_SINGLE_INSTANCE_ORATAB="$TMP/oratab" \
  OPU_SINGLE_INSTANCE_TEST_LISTENER=LISTENER \
  OPU_SINGLE_INSTANCE_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_LISTENER_STATE="$LISTENER_STATE" \
  OPU_TEST_PATCH_STATE="$PATCH_STATE" \
  OPU_TEST_FAIL_APPLICABILITY="$FAIL_APPLICABILITY" \
  OPU_TEST_OPATCH_CALLS="$OPATCH_CALLS" \
  OPU_TEST_DATAPATCH_STATE="$DATAPATCH_STATE" \
  OPU_TEST_FAIL_OPATCH="$FAIL_OPATCH" \
  OPU_TEST_FAIL_DATAPATCH="$FAIL_DATAPATCH" \
  OPU_TEST_FAIL_ROLLBACK="$FAIL_ROLLBACK" \
  OPU_TEST_SQLPATCH_ACTION_STATE="$SQLPATCH_ACTION_STATE" \
    "$EXECUTOR" execute --plan-id "$plan_id" --task-id "$task_id" --actor standalone-worker --lease-seconds 30
}

execute_rollback() {
  local plan_id=$1 task_id=$2
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_SINGLE_INSTANCE_ROLLBACK_STATE_DIR="$ROLLBACK_STATE" \
  OPU_SINGLE_INSTANCE_TEST_MODE=1 \
  OPU_SINGLE_INSTANCE_ORATAB="$TMP/oratab" \
  OPU_SINGLE_INSTANCE_TEST_LISTENER=LISTENER \
  OPU_SINGLE_INSTANCE_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_LISTENER_STATE="$LISTENER_STATE" \
  OPU_TEST_PATCH_STATE="$PATCH_STATE" \
  OPU_TEST_FAIL_APPLICABILITY="$FAIL_APPLICABILITY" \
  OPU_TEST_OPATCH_CALLS="$OPATCH_CALLS" \
  OPU_TEST_DATAPATCH_STATE="$DATAPATCH_STATE" \
  OPU_TEST_FAIL_OPATCH="$FAIL_OPATCH" \
  OPU_TEST_FAIL_DATAPATCH="$FAIL_DATAPATCH" \
  OPU_TEST_FAIL_ROLLBACK="$FAIL_ROLLBACK" \
  OPU_TEST_SQLPATCH_ACTION_STATE="$SQLPATCH_ACTION_STATE" \
    "$ROOT/bin/opu-database-single-instance-rollback" execute --plan-id "$plan_id" --task-id "$task_id" --actor rollback-worker --lease-seconds 30
}

# Artifact tampering must fail before any database mutation.
create_plan standalone-tamper
cp "$PATCH_DIR/README.txt" "$TMP/README.original"
printf 'tamper\n' >>"$PATCH_DIR/README.txt"
TAMPER_TASK=$(plan next --plan-id standalone-tamper | jq -r '.task_id')
if execute standalone-tamper "$TAMPER_TASK" >/dev/null 2>&1; then
  echo 'tampered artifact was accepted by the standalone executor' >&2
  exit 1
fi
plan status --plan-id standalone-tamper | jq -e '.state == "paused"' >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
cp "$TMP/README.original" "$PATCH_DIR/README.txt"

# Platform applicability must also fail closed in the first precheck, preserve
# OPatch's native return code, and leave all services and binaries untouched.
create_plan standalone-platform-precheck-failure
touch "$FAIL_APPLICABILITY"
PLATFORM_INITIAL_PRECHECK=$(plan next --plan-id standalone-platform-precheck-failure | jq -r '.task_id')
set +e
execute standalone-platform-precheck-failure "$PLATFORM_INITIAL_PRECHECK" >/dev/null 2>&1
PLATFORM_INITIAL_RC=$?
set -e
[ "$PLATFORM_INITIAL_RC" -eq 73 ]
plan status --plan-id standalone-platform-precheck-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.exit_code == 73 and .outcome_class == "no_mutation"' \
  "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK/evidence.json" >/dev/null
[ "$(cat "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK/precheck-platform-applicability.log.exit-code")" -eq 73 ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
rm -f "$FAIL_APPLICABILITY"

# The apply task must repeat platform applicability before any outage. If the
# runtime prerequisite changes after precheck, the plan pauses without
# stopping the database or listener and without touching binary inventory.
create_plan standalone-platform-failure
PLATFORM_PRECHECK=$(plan next --plan-id standalone-platform-failure | jq -r '.task_id')
execute standalone-platform-failure "$PLATFORM_PRECHECK" >/dev/null
touch "$FAIL_APPLICABILITY"
PLATFORM_APPLY=$(plan next --plan-id standalone-platform-failure | jq -r '.task_id')
APPLY_CALLS_BEFORE=$(grep -c '^apply ' "$OPATCH_CALLS" 2>/dev/null || true)
set +e
execute standalone-platform-failure "$PLATFORM_APPLY" >/dev/null 2>&1
PLATFORM_APPLY_RC=$?
set -e
if [ "$PLATFORM_APPLY_RC" -eq 0 ]; then
  echo 'failed platform applicability was accepted by the apply task' >&2
  exit 1
fi
[ "$PLATFORM_APPLY_RC" -eq 73 ]
plan status --plan-id standalone-platform-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.exit_code == 73 and .outcome_class == "no_mutation"' \
  "$EXECUTION_STATE/plans/standalone-platform-failure/tasks/$PLATFORM_APPLY/evidence.json" >/dev/null
[ "$(cat "$EXECUTION_STATE/plans/standalone-platform-failure/tasks/$PLATFORM_APPLY/apply-platform-applicability.log.exit-code")" -eq 73 ]
APPLY_CALLS_AFTER=$(grep -c '^apply ' "$OPATCH_CALLS" 2>/dev/null || true)
[ "$APPLY_CALLS_AFTER" -eq "$APPLY_CALLS_BEFORE" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
rm -f "$FAIL_APPLICABILITY"

# A binary-apply failure must pause without pretending that Oracle recovered.
create_plan standalone-opatch-failure
FAILURE_PRECHECK=$(plan next --plan-id standalone-opatch-failure | jq -r '.task_id')
execute standalone-opatch-failure "$FAILURE_PRECHECK" >/dev/null
touch "$FAIL_OPATCH"
FAILURE_APPLY=$(plan next --plan-id standalone-opatch-failure | jq -r '.task_id')
set +e
execute standalone-opatch-failure "$FAILURE_APPLY" >/dev/null 2>&1
FAILURE_APPLY_RC=$?
set -e
if [ "$FAILURE_APPLY_RC" -eq 0 ]; then
  echo 'simulated OPatch failure was reported as successful' >&2
  exit 1
fi
[ "$FAILURE_APPLY_RC" -eq 73 ]
plan status --plan-id standalone-opatch-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.exit_code == 73 and .outcome_class == "binary_state_unknown"' \
  "$EXECUTION_STATE/plans/standalone-opatch-failure/tasks/$FAILURE_APPLY/evidence.json" >/dev/null
[ "$(cat "$EXECUTION_STATE/plans/standalone-opatch-failure/tasks/$FAILURE_APPLY/opatch-apply.exit-code")" -eq 73 ]
[ "$(cat "$DATABASE_STATE")" = down ] && [ "$(cat "$LISTENER_STATE")" = down ] && [ ! -f "$PATCH_STATE" ]

# A paused mutation task is immutable: replay and deriving a rollback plan from
# the incomplete source plan are both refused.
if execute standalone-opatch-failure "$FAILURE_APPLY" >/dev/null 2>&1; then
  echo 'paused binary task replay was accepted' >&2
  exit 1
fi
if plan create-rollback --plan-id rollback-from-paused-source --requester rollback-admin \
  --source-plan-id standalone-opatch-failure --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null 2>&1; then
  echo 'rollback plan was created from a paused, incomplete apply plan' >&2
  exit 1
fi
rm -f "$FAIL_OPATCH"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"

# Run the complete fixed standalone sequence.
create_plan standalone-success
for expected_stage in precheck apply validate datapatch final_validate; do
  TASK_JSON=$(plan next --plan-id standalone-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute standalone-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" '.status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.outcome_class | IN("no_mutation","binary_state_known")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done

plan status --plan-id standalone-success | jq -e '.state == "succeeded"' >/dev/null
[ -f "$PATCH_STATE" ] && [ -f "$DATAPATCH_STATE" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ "$(cat "$SQLPATCH_ACTION_STATE")" = APPLY ]
grep -E 'prereq CheckPatchApplicableOnCurrentPlatform -ph .+ -oh .+ -jdk .+' "$OPATCH_CALLS" >/dev/null
grep -E 'prereq CheckConflictAgainstOHWithDetail -ph .+ -oh .+ -jdk .+' "$OPATCH_CALLS" >/dev/null

create_rollback_plan() {
  local rollback_plan_id=$1
  plan create-rollback --plan-id "$rollback_plan_id" --requester rollback-admin \
    --source-plan-id standalone-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$rollback_plan_id" --actor rollback-approver --approval-ticket TEST-ROLLBACK-39034528 >/dev/null
  plan authorize --plan-id "$rollback_plan_id" --actor rollback-operator >/dev/null
  plan dispatch --plan-id "$rollback_plan_id" --actor rollback-operator >/dev/null
}

# Source apply worker cannot approve or authorize the derived rollback plan.
plan create-rollback --plan-id standalone-rollback-sod --requester rollback-admin \
  --source-plan-id standalone-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
plan status --plan-id standalone-rollback-sod | jq -e '.source_apply.actors == ["standalone-worker"]' >/dev/null
if plan approve --plan-id standalone-rollback-sod --actor standalone-worker --approval-ticket TEST-ROLLBACK-SOD >/dev/null 2>&1; then
  echo 'source apply worker was allowed to approve standalone rollback' >&2
  exit 1
fi
plan approve --plan-id standalone-rollback-sod --actor rollback-approver --approval-ticket TEST-ROLLBACK-SOD >/dev/null
if plan authorize --plan-id standalone-rollback-sod --actor standalone-worker >/dev/null 2>&1; then
  echo 'source apply worker was allowed to authorize standalone rollback' >&2
  exit 1
fi

# A failed OPatch rollback is an unknown binary outcome and cannot be retried.
create_rollback_plan standalone-rollback-failure
ROLLBACK_FAILURE_PRECHECK=$(plan next --plan-id standalone-rollback-failure | jq -r '.task_id')
execute_rollback standalone-rollback-failure "$ROLLBACK_FAILURE_PRECHECK" >/dev/null
touch "$FAIL_ROLLBACK"
ROLLBACK_FAILURE_BINARY=$(plan next --plan-id standalone-rollback-failure | jq -r '.task_id')
if execute_rollback standalone-rollback-failure "$ROLLBACK_FAILURE_BINARY" >/dev/null 2>&1; then
  echo 'simulated OPatch rollback failure was reported as successful' >&2
  exit 1
fi
plan status --plan-id standalone-rollback-failure | jq -e '.state == "paused"' >/dev/null
[ "$(jq -r '.outcome_class' "$ROLLBACK_STATE/plans/standalone-rollback-failure/tasks/$ROLLBACK_FAILURE_BINARY/evidence.json")" = binary_state_unknown ]
[ -f "$PATCH_STATE" ] && [ "$(cat "$DATABASE_STATE")" = down ] && [ "$(cat "$LISTENER_STATE")" = down ]
rm -f "$FAIL_ROLLBACK"
printf 'up\n' >"$DATABASE_STATE"; printf 'up\n' >"$LISTENER_STATE"

# A new rollback plan with a fresh approval completes the README-bound flow.
create_rollback_plan standalone-rollback-success
for expected_stage in rollback_precheck rollback_binary rollback_binary_validate rollback_datapatch rollback_final_validate; do
  TASK_JSON=$(plan next --plan-id standalone-rollback-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute_rollback standalone-rollback-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" '.intent == "patch_rollback" and .status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.outcome_class | IN("no_mutation","binary_state_known")) and (.source_apply.plan_sha256 | test("^[a-f0-9]{64}$")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done

plan status --plan-id standalone-rollback-success | jq -e '.intent == "patch_rollback" and .state == "succeeded"' >/dev/null
[ ! -f "$PATCH_STATE" ] && [ "$(cat "$SQLPATCH_ACTION_STATE")" = ROLLBACK ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]

[ -z "${FLOCK_PROBE:-}" ] || [ -s "$FLOCK_PROBE" ]
printf '%s\n' 'standalone database patch executor test passed'
