#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
. "$ROOT/tests/fixtures/retire_plans.sh"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-ojvm.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

PLAN_TOOL="$ROOT/bin/opu-patch-plan"
EXECUTOR="$ROOT/bin/opu-database-ojvm-patch"
ROLLBACK_EXECUTOR="$ROOT/bin/opu-database-ojvm-rollback"
PLAN_STATE="$TMP/plan-state"
EXECUTION_STATE="$TMP/execution-state"
ROLLBACK_STATE="$TMP/rollback-state"
TEST_HOME="$TMP/oracle/dbhome_1"
PATCH_DIR="$TMP/patch/39034530"
BACKUP_ROOT="$TMP/backup/ORCL/run-001"
PATCH_STATE="$TMP/patch.state"
DATABASE_STATE="$TMP/database.state"
LISTENER_STATE="$TMP/listener.state"
DATAPATCH_STATE="$TMP/datapatch.state"
FAIL_APPLICABILITY="$TMP/fail-applicability"
FAIL_OPATCH="$TMP/fail-opatch"
FAIL_ROLLBACK="$TMP/fail-rollback"
SQLPATCH_ACTION_STATE="$TMP/sqlpatch-action.state"
DATAPATCH_MODE_LOG="$TMP/datapatch-modes.log"

mkdir -p "$TEST_HOME/bin" "$TEST_HOME/OPatch" "$TEST_HOME/jdk/bin" \
  "$PATCH_DIR/etc/config" "$BACKUP_ROOT/rman" "$BACKUP_ROOT/oracle-home"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"

cat >"$TEST_HOME/bin/sqlplus" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
state=$(cat "$OPU_TEST_DATABASE_STATE")
if grep -q 'shutdown immediate' <<<"$input"; then
  [ "$state" != down ] || exit 1
  printf 'down\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q '^startup upgrade;' <<<"$input"; then
  [ "$state" = down ] || exit 1
  printf 'upgrade\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q '^startup;' <<<"$input"; then
  [ "$state" = down ] || exit 1
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
  [ "$state" != down ] || exit 1
  if [ "$state" = upgrade ]; then instance_status='OPEN MIGRATE'; else instance_status=OPEN; fi
  printf '%s\n' \
    'INSTANCE_NAME=ORCL' \
    "INSTANCE_STATUS=$instance_status" \
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
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$OPU_TEST_PATCH_STATE" ] && printf '%s\n' '39034530;OJVM Release Update' || true ;;
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
    [ "$(cat "$OPU_TEST_DATABASE_STATE")" = down ] || { printf 'OJVM binary apply attempted against a running database\n' >&2; exit 70; }
    [ ! -f "$OPU_TEST_FAIL_OPATCH" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf '39034530\n' >"$OPU_TEST_PATCH_STATE"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    # Real OPatch prompts for confirmation and exits 73 without -silent.
    case " $* " in *' -silent '*) ;; *) printf 'Is the local system ready for patching? [y|n]\nOPatch failed with error code 73\n'; exit 73 ;; esac
    [ "$(cat "$OPU_TEST_DATABASE_STATE")" = down ] || { printf 'OJVM binary rollback attempted against a running database\n' >&2; exit 70; }
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
state=$(cat "$OPU_TEST_DATABASE_STATE")
printf '%s\n' "$state" >>"$OPU_TEST_DATAPATCH_MODE_LOG"
[ "$state" = upgrade ] || { printf 'OJVM datapatch requires upgrade mode\n' >&2; exit 74; }
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
<patch patchID="39034530"><description>OJVM Release Update</description><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>
EOF
mkdir -p "$PATCH_DIR/files/lib" && printf 'test patch payload\n' >"$PATCH_DIR/files/lib/libtestpatch.so"
printf '%s\n' \
  'OJVM Release Update test README' \
  'Shut down all database instances before applying this patch.' \
  'opatch rollback -id 39034530' \
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
  '{schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"39034530",artifact_sha256:$artifact_sha,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_ojvm_opatch",operations:["database_shutdown","database_opatch_apply","database_startup_upgrade","database_datapatch_upgrade"]},required_opatch_version:"12.2.0.1.49",oracle_references:[{kind:"patch_readme",identifier:"OJVM README shutdown/upgrade sections",sha256:$readme_sha}],rollback:{mode:"opatch_rollback",precondition:"separate approved rollback plan required"}}}' >"$TMP/procedure.json"
jq -n --arg home "$TEST_HOME" --arg digest "$ARTIFACT_SHA" '{schema_version:"1.0",status:"passed",patch_id:"39034530",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"testnode",home:$home,owner:"test",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.49",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}}]}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"

RECONCILIATION_SHA=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}')
ARTIFACT_MANIFEST_SHA=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
PROCEDURE_SHA=$(sha256sum "$TMP/procedure.json" | awk '{print $1}')
COMPATIBILITY_SHA=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
POLICY_SHA=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg reconciliation "$RECONCILIATION_SHA" --arg artifact "$ARTIFACT_MANIFEST_SHA" --arg procedure "$PROCEDURE_SHA" --arg compatibility "$COMPATIBILITY_SHA" --arg policy "$POLICY_SHA" \
  --arg evaluated "$EVIDENCE_COLLECTED" --arg valid "$EVIDENCE_VALID" --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"39034530",target:{family:"database",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha,host:"testnode",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$reconciliation,artifact_manifest_sha256:$artifact,procedure_validation_sha256:$procedure,compatibility_sha256:$compatibility,policy_sha256:$policy}}' >"$TMP/readiness.json"

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
  plan approve --plan-id "$plan_id" --actor dba-approver --approval-ticket TEST-OJVM-39034530 >/dev/null
  plan authorize --plan-id "$plan_id" --actor patch-operator >/dev/null
  # Independent simulated target scenario; lifecycle tests cover retained reservations.
  retire_fixture_plans "$PLAN_STATE"
  plan dispatch --plan-id "$plan_id" --actor patch-operator >/dev/null
}

run_executor() {
  local executor=$1 state_env=$2 state_dir=$3 plan_id=$4 task_id=$5 actor=$6
  env OPU_PLAN_STATE_DIR="$PLAN_STATE" \
    "$state_env=$state_dir" \
    OPU_OJVM_TEST_MODE=1 \
    OPU_OJVM_ORATAB="$TMP/oratab" \
    OPU_OJVM_TEST_LISTENER=LISTENER \
    OPU_OJVM_TEST_DATABASE_STATE="$DATABASE_STATE" \
    OPU_TEST_DATABASE_STATE="$DATABASE_STATE" \
    OPU_TEST_LISTENER_STATE="$LISTENER_STATE" \
    OPU_TEST_PATCH_STATE="$PATCH_STATE" \
    OPU_TEST_FAIL_APPLICABILITY="$FAIL_APPLICABILITY" \
    OPU_TEST_FAIL_OPATCH="$FAIL_OPATCH" \
    OPU_TEST_FAIL_ROLLBACK="$FAIL_ROLLBACK" \
    OPU_TEST_DATAPATCH_STATE="$DATAPATCH_STATE" \
    OPU_TEST_DATAPATCH_MODE_LOG="$DATAPATCH_MODE_LOG" \
    OPU_TEST_SQLPATCH_ACTION_STATE="$SQLPATCH_ACTION_STATE" \
    "$executor" execute --plan-id "$plan_id" --task-id "$task_id" --actor "$actor" --lease-seconds 30
}

execute() { run_executor "$EXECUTOR" OPU_OJVM_STATE_DIR "$EXECUTION_STATE" "$1" "$2" ojvm-worker; }
execute_rollback() { run_executor "$ROLLBACK_EXECUTOR" OPU_OJVM_ROLLBACK_STATE_DIR "$ROLLBACK_STATE" "$1" "$2" ojvm-rollback-worker; }

# A failed platform-applicability precheck must classify no_mutation and leave
# the database, listener, and binary inventory untouched.
create_plan ojvm-precheck-failure
touch "$FAIL_APPLICABILITY"
PRECHECK_FAILURE_TASK=$(plan next --plan-id ojvm-precheck-failure | jq -r '.task_id')
set +e
execute ojvm-precheck-failure "$PRECHECK_FAILURE_TASK" >/dev/null 2>&1
PRECHECK_FAILURE_RC=$?
set -e
[ "$PRECHECK_FAILURE_RC" -eq 73 ]
plan status --plan-id ojvm-precheck-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.status == "failed" and .exit_code == 73 and .outcome_class == "no_mutation" and .postcondition.status == "failed"' \
  "$EXECUTION_STATE/plans/ojvm-precheck-failure/tasks/$PRECHECK_FAILURE_TASK/evidence.json" >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
rm -f "$FAIL_APPLICABILITY"

# Run the complete fixed OJVM full-shutdown/upgrade-mode sequence.
create_plan ojvm-success
for expected_stage in ojvm_precheck ojvm_stop_all ojvm_binary_apply ojvm_start_upgrade ojvm_datapatch_upgrade ojvm_restart_normal ojvm_final_validate; do
  TASK_JSON=$(plan next --plan-id ojvm-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute ojvm-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" '.status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.outcome_class | IN("no_mutation","database_down","binary_state_known")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done
jq -e '.outcome_class == "database_down"' "$TMP/ojvm_stop_all-result.json" >/dev/null
plan status --plan-id ojvm-success | jq -e '.state == "succeeded"' >/dev/null
[ -f "$PATCH_STATE" ] && [ -f "$DATAPATCH_STATE" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ "$(cat "$SQLPATCH_ACTION_STATE")" = APPLY ]
grep -q '^upgrade$' "$DATAPATCH_MODE_LOG"
if grep -qv '^upgrade$' "$DATAPATCH_MODE_LOG"; then
  echo 'OJVM datapatch ran outside upgrade mode' >&2
  exit 1
fi

create_rollback_plan() {
  local rollback_plan_id=$1
  plan create-rollback --plan-id "$rollback_plan_id" --requester rollback-admin \
    --source-plan-id ojvm-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$rollback_plan_id" --actor rollback-approver --approval-ticket TEST-OJVM-ROLLBACK >/dev/null
  plan authorize --plan-id "$rollback_plan_id" --actor rollback-operator >/dev/null
  # Independent simulated target scenario; lifecycle tests cover retained reservations.
  retire_fixture_plans "$PLAN_STATE"
  plan dispatch --plan-id "$rollback_plan_id" --actor rollback-operator >/dev/null
}

# Source apply worker cannot approve the derived OJVM rollback plan.
plan create-rollback --plan-id ojvm-rollback-sod --requester rollback-admin \
  --source-plan-id ojvm-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
plan status --plan-id ojvm-rollback-sod | jq -e '.source_apply.actors == ["ojvm-worker"]' >/dev/null
if plan approve --plan-id ojvm-rollback-sod --actor ojvm-worker --approval-ticket TEST-OJVM-SOD >/dev/null 2>&1; then
  echo 'source apply worker was allowed to approve the OJVM rollback' >&2
  exit 1
fi

# A fresh rollback plan completes the full-shutdown README-bound rollback flow.
create_rollback_plan ojvm-rollback-success
: >"$DATAPATCH_MODE_LOG"
for expected_stage in ojvm_rollback_precheck ojvm_rollback_stop_all ojvm_rollback_binary ojvm_rollback_start_upgrade ojvm_rollback_datapatch ojvm_rollback_restart_normal ojvm_rollback_final_validate; do
  TASK_JSON=$(plan next --plan-id ojvm-rollback-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute_rollback ojvm-rollback-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" '.intent == "patch_rollback" and .status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.outcome_class | IN("no_mutation","database_down","binary_state_known")) and (.source_apply.plan_sha256 | test("^[a-f0-9]{64}$")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done
jq -e '.outcome_class == "database_down"' "$TMP/ojvm_rollback_stop_all-result.json" >/dev/null
plan status --plan-id ojvm-rollback-success | jq -e '.intent == "patch_rollback" and .state == "succeeded"' >/dev/null
[ ! -f "$PATCH_STATE" ] && [ "$(cat "$SQLPATCH_ACTION_STATE")" = ROLLBACK ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
grep -q '^upgrade$' "$DATAPATCH_MODE_LOG"
printf '%s\n' 'OJVM database patch executor test passed'
