#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-oop.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

PLAN_TOOL="$ROOT/bin/opu-patch-plan"
EXECUTOR="$ROOT/bin/opu-database-out-of-place-patch"
SWITCHBACK_EXECUTOR="$ROOT/bin/opu-database-out-of-place-switchback"
PLAN_STATE="$TMP/plan-state"
EXECUTION_STATE="$TMP/execution-state"
SWITCHBACK_STATE="$TMP/switchback-state"
TEST_HOME="$TMP/oracle/dbhome_1"
CLONE_HOME="$TEST_HOME-oop-39034531"
PATCH_DIR="$TMP/patch/39034531"
BACKUP_ROOT="$TMP/backup/ORCL/run-001"
ACTIVE_HOME_STATE="$TMP/active-home.state"
DATABASE_STATE="$TMP/database.state"
LISTENER_STATE="$TMP/listener.state"
DATAPATCH_STATE="$TMP/datapatch.state"
FAIL_APPLICABILITY="$TMP/fail-applicability"
FAIL_OPATCH="$TMP/fail-opatch"
SQLPATCH_ACTION_STATE="$TMP/sqlpatch-action.state"
DATAPATCH_HOME_LOG="$TMP/datapatch-homes.log"

mkdir -p "$TEST_HOME/bin" "$TEST_HOME/OPatch" "$TEST_HOME/jdk/bin" \
  "$PATCH_DIR/etc/config" "$BACKUP_ROOT/rman" "$BACKUP_ROOT/oracle-home"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"
printf '%s\n' "$TEST_HOME" >"$ACTIVE_HOME_STATE"

cat >"$TEST_HOME/bin/sqlplus" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
state=$(cat "$OPU_TEST_DATABASE_STATE")
active=$(cat "$OPU_TEST_ACTIVE_HOME_STATE")
if grep -q 'shutdown immediate' <<<"$input"; then
  [ "$state" = up ] || exit 1
  [ "$ORACLE_HOME" = "$active" ] || { printf 'shutdown attempted from a non-active home\n' >&2; exit 1; }
  printf 'down\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q '^startup;' <<<"$input"; then
  [ "$state" = down ] || exit 1
  printf 'up\n' >"$OPU_TEST_DATABASE_STATE"
  printf '%s\n' "$ORACLE_HOME" >"$OPU_TEST_ACTIVE_HOME_STATE"
elif grep -q 'SQLPATCH_LATEST_ACTION=' <<<"$input"; then
  [ "$state" = up ] || exit 1
  [ "$ORACLE_HOME" = "$active" ] || exit 1
  action=$(cat "$OPU_TEST_SQLPATCH_ACTION_STATE" 2>/dev/null || true)
  [ -n "$action" ] || exit 1
  printf '%s\n' "SQLPATCH_LATEST_ACTION=$action" 'SQLPATCH_LATEST_STATUS=SUCCESS'
elif grep -q 'SQLPATCH_SUCCESS=' <<<"$input"; then
  [ "$state" = up ] || exit 1
  [ "$ORACLE_HOME" = "$active" ] || exit 1
  if [ "$(cat "$OPU_TEST_SQLPATCH_ACTION_STATE" 2>/dev/null || true)" = APPLY ]; then
    printf '%s\n' 'SQLPATCH_SUCCESS=1' 'SQLPATCH_NON_SUCCESS=0'
  else
    printf '%s\n' 'SQLPATCH_SUCCESS=0' 'SQLPATCH_NON_SUCCESS=0'
  fi
elif grep -q 'alter system register' <<<"$input"; then
  :
else
  [ "$state" = up ] || exit 1
  [ "$ORACLE_HOME" = "$active" ] || { printf 'probe attempted from a non-active home\n' >&2; exit 1; }
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
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$ORACLE_HOME/.fake-patch-state" ] && printf '%s\n' '39034531;Database Release Update' || true ;;
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
    [ "$ORACLE_HOME" != "$(cat "$OPU_TEST_ACTIVE_HOME_STATE")" ] || { printf 'out-of-place apply attempted against the active home\n' >&2; exit 70; }
    [ ! -f "$OPU_TEST_FAIL_OPATCH" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf '39034531\n' >"$ORACLE_HOME/.fake-patch-state"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
EOF

cat >"$TEST_HOME/OPatch/datapatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || { printf 'datapatch requires an open database\n' >&2; exit 74; }
[ "$ORACLE_HOME" = "$(cat "$OPU_TEST_ACTIVE_HOME_STATE")" ] || { printf 'datapatch attempted from a non-active home\n' >&2; exit 74; }
printf '%s\n' "$ORACLE_HOME" >>"$OPU_TEST_DATAPATCH_HOME_LOG"
if [ -f "$ORACLE_HOME/.fake-patch-state" ]; then printf 'APPLY\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; else printf 'ROLLBACK\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; fi
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
<patch patchID="39034531"><description>Database Release Update</description><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>
EOF
printf '%s\n' \
  'Database Release Update test README' \
  'Out-of-place patching: clone the home, patch the clone, then switch.' \
  'To revert, switch the database back to the original home.' \
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
  '{schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"39034531",artifact_sha256:$artifact_sha,target:{family:"database",method:"switch_home",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_out_of_place_switch",operations:["database_home_clone","database_opatch_apply_clone","database_home_switch","database_datapatch"]},required_opatch_version:"12.2.0.1.49",oracle_references:[{kind:"patch_readme",identifier:"Out-of-place README clone/switch sections",sha256:$readme_sha}],rollback:{mode:"home_switch_back",precondition:"separate approved switchback plan required"}}}' >"$TMP/procedure.json"
jq -n --arg home "$TEST_HOME" --arg digest "$ARTIFACT_SHA" '{schema_version:"1.0",status:"passed",patch_id:"39034531",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"testnode",home:$home,owner:"test",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.49",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}}]}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"

RECONCILIATION_SHA=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}')
ARTIFACT_MANIFEST_SHA=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
PROCEDURE_SHA=$(sha256sum "$TMP/procedure.json" | awk '{print $1}')
COMPATIBILITY_SHA=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
POLICY_SHA=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg reconciliation "$RECONCILIATION_SHA" --arg artifact "$ARTIFACT_MANIFEST_SHA" --arg procedure "$PROCEDURE_SHA" --arg compatibility "$COMPATIBILITY_SHA" --arg policy "$POLICY_SHA" \
  --arg evaluated "$EVIDENCE_COLLECTED" --arg valid "$EVIDENCE_VALID" --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"39034531",target:{family:"database",method:"switch_home",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha,host:"testnode",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$reconciliation,artifact_manifest_sha256:$artifact,procedure_validation_sha256:$procedure,compatibility_sha256:$compatibility,policy_sha256:$policy}}' >"$TMP/readiness.json"

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
  plan approve --plan-id "$plan_id" --actor dba-approver --approval-ticket TEST-OOP-39034531 >/dev/null
  plan authorize --plan-id "$plan_id" --actor patch-operator >/dev/null
  plan dispatch --plan-id "$plan_id" --actor patch-operator >/dev/null
}

run_executor() {
  local executor=$1 state_env=$2 state_dir=$3 plan_id=$4 task_id=$5 actor=$6
  env OPU_PLAN_STATE_DIR="$PLAN_STATE" \
    "$state_env=$state_dir" \
    OPU_OOP_TEST_MODE=1 \
    OPU_OOP_ORATAB="$TMP/oratab" \
    OPU_OOP_TEST_LISTENER=LISTENER \
    OPU_OOP_TEST_DATABASE_STATE="$DATABASE_STATE" \
    OPU_TEST_DATABASE_STATE="$DATABASE_STATE" \
    OPU_TEST_ACTIVE_HOME_STATE="$ACTIVE_HOME_STATE" \
    OPU_TEST_LISTENER_STATE="$LISTENER_STATE" \
    OPU_TEST_FAIL_APPLICABILITY="$FAIL_APPLICABILITY" \
    OPU_TEST_FAIL_OPATCH="$FAIL_OPATCH" \
    OPU_TEST_DATAPATCH_STATE="$DATAPATCH_STATE" \
    OPU_TEST_DATAPATCH_HOME_LOG="$DATAPATCH_HOME_LOG" \
    OPU_TEST_SQLPATCH_ACTION_STATE="$SQLPATCH_ACTION_STATE" \
    "$executor" execute --plan-id "$plan_id" --task-id "$task_id" --actor "$actor" --lease-seconds 30
}

execute() { run_executor "$EXECUTOR" OPU_OOP_STATE_DIR "$EXECUTION_STATE" "$1" "$2" oop-worker; }
execute_switchback() { run_executor "$SWITCHBACK_EXECUTOR" OPU_OOP_SWITCHBACK_STATE_DIR "$SWITCHBACK_STATE" "$1" "$2" oop-switchback-worker; }

# A failed platform-applicability precheck must classify no_mutation, leave
# the database on the original home, and never create the clone directory.
create_plan oop-precheck-failure
touch "$FAIL_APPLICABILITY"
PRECHECK_FAILURE_TASK=$(plan next --plan-id oop-precheck-failure | jq -r '.task_id')
set +e
execute oop-precheck-failure "$PRECHECK_FAILURE_TASK" >/dev/null 2>&1
PRECHECK_FAILURE_RC=$?
set -e
[ "$PRECHECK_FAILURE_RC" -eq 73 ]
plan status --plan-id oop-precheck-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.status == "failed" and .exit_code == 73 and .outcome_class == "no_mutation" and .postcondition.status == "failed"' \
  "$EXECUTION_STATE/plans/oop-precheck-failure/tasks/$PRECHECK_FAILURE_TASK/evidence.json" >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$ACTIVE_HOME_STATE")" = "$TEST_HOME" ]
[ ! -e "$CLONE_HOME" ] && [ ! -f "$TEST_HOME/.fake-patch-state" ]
grep -Fxq "ORCL:$TEST_HOME:N" "$TMP/oratab"
rm -f "$FAIL_APPLICABILITY"

# Run the complete fixed clone/patch/validate/switch sequence.
create_plan oop-success
for expected_stage in oop_precheck oop_clone_home oop_patch_clone oop_validate_clone oop_switch_home oop_datapatch oop_final_validate; do
  TASK_JSON=$(plan next --plan-id oop-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute oop-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" --arg clone "$CLONE_HOME" '.status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and .target.clone_home == $clone and (.outcome_class | IN("no_mutation","binary_state_known")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done
plan status --plan-id oop-success | jq -e '.state == "succeeded"' >/dev/null

# Clone digest evidence: the clone task seals a baseline digest and the clone
# patch task seals a different digest for the patched clone.
jq -e '.clone.oracle_home != null and (.clone.digest_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/oop_clone_home-result.json" >/dev/null
jq -e '.clone.digest_sha256 | test("^[a-f0-9]{64}$")' "$TMP/oop_patch_clone-result.json" >/dev/null
BASELINE_DIGEST=$(jq -r '.clone.digest_sha256' "$TMP/oop_clone_home-result.json")
PATCHED_DIGEST=$(jq -r '.clone.digest_sha256' "$TMP/oop_patch_clone-result.json")
[ "$BASELINE_DIGEST" != "$PATCHED_DIGEST" ]
[ "$(cat "$EXECUTION_STATE/plans/oop-success/clone-baseline-digest")" = "$BASELINE_DIGEST" ]
[ "$(cat "$EXECUTION_STATE/plans/oop-success/clone-patched-digest")" = "$PATCHED_DIGEST" ]
[ "$(jq -r '.clone.digest_sha256' "$TMP/oop_validate_clone-result.json")" = "$PATCHED_DIGEST" ]

# The database now runs from the patched clone; the original home is untouched.
grep -Fxq "ORCL:$CLONE_HOME:N" "$TMP/oratab"
[ "$(cat "$ACTIVE_HOME_STATE")" = "$CLONE_HOME" ]
[ -f "$CLONE_HOME/.fake-patch-state" ] && [ ! -f "$TEST_HOME/.fake-patch-state" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ "$(cat "$SQLPATCH_ACTION_STATE")" = APPLY ]
grep -Fxq "$CLONE_HOME" "$DATAPATCH_HOME_LOG"

create_switchback_plan() {
  local switchback_plan_id=$1
  plan create-rollback --plan-id "$switchback_plan_id" --requester rollback-admin \
    --source-plan-id oop-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$switchback_plan_id" --actor rollback-approver --approval-ticket TEST-OOP-SWITCHBACK >/dev/null
  plan authorize --plan-id "$switchback_plan_id" --actor rollback-operator >/dev/null
  plan dispatch --plan-id "$switchback_plan_id" --actor rollback-operator >/dev/null
}

# Source apply worker cannot approve the derived switchback plan.
plan create-rollback --plan-id oop-switchback-sod --requester rollback-admin \
  --source-plan-id oop-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
plan status --plan-id oop-switchback-sod | jq -e '.source_apply.actors == ["oop-worker"]' >/dev/null
if plan approve --plan-id oop-switchback-sod --actor oop-worker --approval-ticket TEST-OOP-SOD >/dev/null 2>&1; then
  echo 'source apply worker was allowed to approve the switchback' >&2
  exit 1
fi

# A fresh switchback plan repoints the database to the untouched original home.
create_switchback_plan oop-switchback-success
: >"$DATAPATCH_HOME_LOG"
for expected_stage in oop_switchback_precheck oop_switch_back oop_switchback_datapatch oop_switchback_final_validate; do
  TASK_JSON=$(plan next --plan-id oop-switchback-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute_switchback oop-switchback-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" --arg clone "$CLONE_HOME" '.intent == "patch_rollback" and .status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and .target.clone_home == $clone and (.outcome_class | IN("no_mutation","binary_state_known")) and (.source_apply.plan_sha256 | test("^[a-f0-9]{64}$")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done
plan status --plan-id oop-switchback-success | jq -e '.intent == "patch_rollback" and .state == "succeeded"' >/dev/null
grep -Fxq "ORCL:$TEST_HOME:N" "$TMP/oratab"
[ "$(cat "$ACTIVE_HOME_STATE")" = "$TEST_HOME" ]
[ -f "$CLONE_HOME/.fake-patch-state" ] && [ ! -f "$TEST_HOME/.fake-patch-state" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ "$(cat "$SQLPATCH_ACTION_STATE")" = ROLLBACK ]
grep -Fxq "$TEST_HOME" "$DATAPATCH_HOME_LOG"
printf '%s\n' 'Out-of-place database patch executor test passed'
