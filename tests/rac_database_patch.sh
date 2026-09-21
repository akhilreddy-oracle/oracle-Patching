#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
. "$ROOT/tests/fixtures/retire_plans.sh"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-rac-database.XXXXXX")
cleanup() {
  local rc=$?
  if [ "${OPU_TEST_KEEP_TMP:-0}" = 1 ] && [ "$rc" -ne 0 ]; then
    printf 'RAC test evidence retained at %s\n' "$TMP" >&2
  else
    rm -rf "$TMP"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

PLAN_TOOL="$ROOT/bin/opu-patch-plan"
EXECUTOR="$ROOT/bin/opu-database-rac-node-patch"
ROLLBACK_EXECUTOR="$ROOT/bin/opu-database-rac-node-rollback"
PLAN_STATE="$TMP/plan-state"
EXECUTION_STATE="$TMP/execution-state"
ROLLBACK_EXECUTION_STATE="$TMP/rollback-execution-state"
TEST_DB_PATH="$TMP/oracle/dbhome_1"
TEST_GRID_PATH="$TMP/grid"
PATCH_DIR="$TMP/patch/39034528"
BACKUP_ROOT="$TMP/backup/ORCL/run-001"
RUNTIME_ROOT="$TMP/runtime"
FAIL_OPATCH="$RUNTIME_ROOT/fail-opatch"
FAIL_ROLLBACK="$RUNTIME_ROOT/fail-rollback"

mkdir -p "$TEST_DB_PATH/bin" "$TEST_DB_PATH/OPatch" "$TEST_DB_PATH/jdk/bin" \
  "$TEST_GRID_PATH/bin" "$PATCH_DIR/etc/config" "$BACKUP_ROOT" "$RUNTIME_ROOT"
printf 'running\n' >"$RUNTIME_ROOT/node1.state"
printf 'running\n' >"$RUNTIME_ROOT/node2.state"
: >"$RUNTIME_ROOT/native-mutations.log"

cat >"$TEST_GRID_PATH/bin/srvctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
command_name=${1:-}; object_name=${2:-}
case "$command_name" in
  relocate|stop|start) printf '%s\n' "$*" >>"$OPU_TEST_RAC_RUNTIME/native-mutations.log" ;;
esac
shift 2 || true
node_name=""; instance_name=""; database_name=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -db) database_name=${2:-}; shift 2 ;;
    -node) node_name=${2:-}; shift 2 ;;
    -instance) instance_name=${2:-}; shift 2 ;;
    *) shift ;;
  esac
done
state_for_node() { printf '%s/%s.state' "$OPU_TEST_RAC_RUNTIME" "$1"; }
node_for_instance() { case "$1" in ORCL1) printf 'node1\n' ;; ORCL2) printf 'node2\n' ;; *) exit 64 ;; esac; }
instance_for_node() { case "$1" in node1) printf 'ORCL1\n' ;; node2) printf 'ORCL2\n' ;; *) exit 64 ;; esac; }
case "$command_name:$object_name" in
  config:database)
    if [ -n "$database_name" ]; then printf 'Oracle home: %s\n' "$OPU_TEST_RAC_DB_PATH"; else printf 'ORCL\n'; fi
    ;;
  status:instance)
    instance_name=$(instance_for_node "$node_name")
    if [ "$(cat "$(state_for_node "$node_name")")" = running ]; then
      printf 'Instance %s is running on node %s\n' "$instance_name" "$node_name"
    else
      printf 'Instance %s is not running on node %s\n' "$instance_name" "$node_name"
    fi
    ;;
  status:service)
    :
    ;;
  relocate:service)
    :
    ;;
  stop:instance)
    node_name=$(node_for_instance "$instance_name")
    printf 'stopped\n' >"$(state_for_node "$node_name")"
    ;;
  start:instance)
    node_name=$(node_for_instance "$instance_name")
    printf 'running\n' >"$(state_for_node "$node_name")"
    ;;
  *)
    printf 'unsupported fake srvctl operation: %s %s\n' "$command_name" "$object_name" >&2
    exit 64
    ;;
esac
EOF

cat >"$TEST_DB_PATH/bin/sqlplus" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
case "${ORACLE_SID:-}" in ORCL1) node_name=node1 ;; ORCL2) node_name=node2 ;; *) exit 65 ;; esac
[ "$(cat "$OPU_TEST_RAC_RUNTIME/$node_name.state")" = running ] || exit 1
if grep -q 'SQLPATCH_LATEST_ACTION=' <<<"$input"; then
  [ -f "$OPU_TEST_RAC_RUNTIME/datapatch.state" ] || exit 1
  printf 'SQLPATCH_LATEST_ACTION=%s\n' "$(cat "$OPU_TEST_RAC_RUNTIME/datapatch.state")"
  printf '%s\n' 'SQLPATCH_LATEST_STATUS=SUCCESS'
else
  database_name=ORCL; instance_name=$ORACLE_SID; database_role=PRIMARY; cdb=NO
  case "$(cat "$OPU_TEST_RAC_RUNTIME/health-drift" 2>/dev/null || true)" in
    database) database_name=UNREVIEWED ;;
    instance) instance_name=OTHER ;;
    role) database_role='PHYSICAL STANDBY' ;;
    cdb) cdb=YES ;;
    unknown-cdb) cdb='' ;;
  esac
  printf '%s\n' \
    "INSTANCE_NAME=$instance_name" \
    'INSTANCE_STATUS=OPEN' \
    "DATABASE_UNIQUE_NAME=$database_name" \
    "CDB=$cdb" \
    "DATABASE_ROLE=$database_role" \
    'OPEN_MODE=READ WRITE' \
    'INVALID_OBJECTS=0'
fi
EOF

cat >"$TEST_DB_PATH/OPatch/opatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_RAC_DATABASE_TEST_HOSTNAME:-node1}
patch_state="$OPU_TEST_RAC_RUNTIME/patch-$node_name.state"
case "${1:-}" in
  apply|rollback) printf 'opatch %s\n' "$*" >>"$OPU_TEST_RAC_RUNTIME/native-mutations.log" ;;
esac
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$patch_state" ] && printf '%s\n' '39034528;Database Release Update' || true ;;
  prereq) printf '%s\n' 'Prereq "checkConflictAgainstOHWithDetail" passed.' ;;
  apply)
    [ ! -f "$OPU_TEST_RAC_RUNTIME/fail-opatch" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf 'installed\n' >"$patch_state"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    [ ! -f "$OPU_TEST_RAC_RUNTIME/fail-rollback" ] || { printf 'simulated OPatch rollback failure\n' >&2; exit 74; }
    rm -f "$patch_state"
    printf '%s\n' "$node_name" >>"$OPU_TEST_RAC_RUNTIME/rollback-order.log"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  lsinventory)
    output_path=""
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = -xml ]; then output_path=${2:-}; break; fi
      shift
    done
    [ -n "$output_path" ] || exit 64
    printf '<InventoryInstance/>\n' >"$output_path"
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
EOF

cat >"$TEST_DB_PATH/OPatch/datapatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ -f "$OPU_TEST_RAC_RUNTIME/patch-node1.state" ] && [ -f "$OPU_TEST_RAC_RUNTIME/patch-node2.state" ]; then
  printf 'APPLY\n' >"$OPU_TEST_RAC_RUNTIME/datapatch.state"
elif [ ! -f "$OPU_TEST_RAC_RUNTIME/patch-node1.state" ] && [ ! -f "$OPU_TEST_RAC_RUNTIME/patch-node2.state" ]; then
  printf 'ROLLBACK\n' >"$OPU_TEST_RAC_RUNTIME/datapatch.state"
else
  printf 'mixed binary inventory refused\n' >&2
  exit 65
fi
printf '%s\n' 'SQL Patching tool complete.'
EOF

cat >"$TEST_DB_PATH/jdk/bin/java" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 750 "$TEST_GRID_PATH/bin/srvctl" "$TEST_DB_PATH/bin/sqlplus" \
  "$TEST_DB_PATH/OPatch/opatch" "$TEST_DB_PATH/OPatch/datapatch" "$TEST_DB_PATH/jdk/bin/java"

cat >"$PATCH_DIR/etc/config/actions.xml" <<'EOF'
<patch patchID="39034528"><actions/></patch>
EOF
cat >"$PATCH_DIR/etc/config/inventory.xml" <<'EOF'
<patch patchID="39034528"><description>Database Release Update</description><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>
EOF
mkdir -p "$PATCH_DIR/files/lib" && printf 'test patch payload\n' >"$PATCH_DIR/files/lib/libtestpatch.so"
printf '%s\n' \
  'RAC Database Release Update test README' \
  'srvctl stop instance -db ORCL -instance ORCL1' \
  'opatch apply' \
  'opatch rollback -id 39034528' \
  'srvctl start instance -db ORCL -instance ORCL1' \
  'datapatch -verbose' >"$PATCH_DIR/README.txt"
"$ROOT/bin/opu-artifact-inspect" --artifact "$PATCH_DIR" --output "$TMP/artifact.json" >/dev/null
ARTIFACT_SHA=$(jq -r '.artifact.sha256' "$TMP/artifact.json")
README_SHA=$(sha256sum "$PATCH_DIR/README.txt" | awk '{print $1}')
OWNER=$(id -un)

printf 'recoverable backup\n' >"$BACKUP_ROOT/backup-piece"
sha256sum "$BACKUP_ROOT/backup-piece" >"$BACKUP_ROOT/SHA256SUMS"
CHECKSUM_SHA=$(sha256sum "$BACKUP_ROOT/SHA256SUMS" | awk '{print $1}')
printf 'RMAN validation passed\n' >"$TMP/rman.log"
RMAN_SHA=$(sha256sum "$TMP/rman.log" | awk '{print $1}')
jq -cn --arg oracle_target "$TEST_DB_PATH" --arg owner "$OWNER" --arg root "$BACKUP_ROOT" --arg checksum "$CHECKSUM_SHA" --arg rman_path "$TMP/rman.log" --arg rman_sha "$RMAN_SHA" '
  {schema_version:"1.0",collector:{name:"oracle.recovery.evidence",version:"1"},status:"passed",target:{database_unique_name:"ORCL",oracle_home:$oracle_target,owner:$owner,oracle_sid:"ORCL1"},backup:{root:$root,checksum_manifest:{path:($root+"/SHA256SUMS"),sha256:$checksum},files:[],oracle_home_archive:($root+"/backup-piece"),rman_backup_set_keys:[1],selected_recovery_set:{observed_at:(now|todateiso8601),oldest_datafile_backup_completed_at:(now|todateiso8601),age_seconds_at_collection:0,restore_piece_handles:[($root+"/backup-piece")],datafile_backup_sets:[1]},coverage:{datafiles_backed:1,base_datafiles:1,datafiles_current:1,controlfile_records:1,spfile_records:1,outside_root_pieces:0,unavailable_pieces:0}},verification:{rman_log:{path:$rman_path,sha256:$rman_sha}}}
  ' >"$TMP/recovery.tmp"
RECOVERY_RECORD=$(jq -cS . "$TMP/recovery.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$RECOVERY_RECORD" '.record_sha256=$record' "$TMP/recovery.tmp" >"$TMP/recovery.json"

EVIDENCE_NOW=$(date -u +%s)
EVIDENCE_COLLECTED=$(date -u -r "$EVIDENCE_NOW" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$EVIDENCE_NOW" '+%Y-%m-%dT%H:%M:%SZ')
EVIDENCE_VALID=$(date -u -r "$((EVIDENCE_NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((EVIDENCE_NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ')
for node_name in node1 node2; do
  jq -n --arg node "$node_name" --arg collected "$EVIDENCE_COLLECTED" '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,host:{name:($node+".example")},cluster:{status:"detected",grid_home:null,runtime:{status:"healthy"},nodes:[]},oracle_homes:[],databases:[],warnings:[]}' >"$TMP/$node_name-topology.json"
done
NODE1_SNAPSHOT_SHA=$(sha256sum "$TMP/node1-topology.json" | awk '{print $1}'); NODE2_SNAPSHOT_SHA=$(sha256sum "$TMP/node2-topology.json" | awk '{print $1}')
jq --arg snapshot "$TMP/node1-topology.json" --arg snapshot_sha "$NODE1_SNAPSHOT_SHA" '.source_snapshot={path:$snapshot,sha256:$snapshot_sha}' "$TMP/recovery.json" >"$TMP/recovery.with-snapshot.json"
RECOVERY_RECORD=$(jq -cS 'del(.record_sha256)' "$TMP/recovery.with-snapshot.json" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$RECOVERY_RECORD" '.record_sha256=$record' "$TMP/recovery.with-snapshot.json" >"$TMP/recovery.json"
jq -n --arg oracle_target "$TEST_DB_PATH" --arg grid_target "$TEST_GRID_PATH" --arg owner "$OWNER" \
  --arg node1 "$TMP/node1-topology.json" --arg node1_sha "$NODE1_SNAPSHOT_SHA" --arg node2 "$TMP/node2-topology.json" --arg node2_sha "$NODE2_SNAPSHOT_SHA" '
  {schema_version:"1.0",status:"consistent",expected_nodes:["node1","node2"],cluster:{grid_home:$grid_target,runtime:{status:"healthy",active_version:"19.0.0.0.0",upgrade_state:"NORMAL",active_patch_level:"1"}},oracle_homes:[{path:$grid_target,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]},{path:$oracle_target,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:$oracle_target}],snapshot_evidence:[{path:$node1,sha256:$node1_sha},{path:$node2,sha256:$node2_sha}]}
  ' >"$TMP/reconciliation.json"
jq -n --arg artifact "$ARTIFACT_SHA" --arg readme "$README_SHA" '
  {schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"39034528",artifact_sha256:$artifact,target:{family:"database",method:"opatch",topology:"rac",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_rolling_opatch",operations:["database_stop_instance","database_opatch_apply","database_start_instance","database_datapatch"]},required_opatch_version:"12.2.0.1.49",oracle_references:[{kind:"patch_readme",identifier:"test RAC README",sha256:$readme}],rollback:{mode:"opatch_rollback",precondition:"separate approved recovery plan"}}}
  ' >"$TMP/procedure.json"
jq -n --arg home "$TEST_DB_PATH" --arg digest "$ARTIFACT_SHA" '{schema_version:"1.0",status:"passed",patch_id:"39034528",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:["node1","node2"] | map({node:.,home:$home,owner:"oracle",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.49",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}})}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"
RECONCILIATION_SHA=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}')
ARTIFACT_MANIFEST_SHA=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
PROCEDURE_SHA=$(sha256sum "$TMP/procedure.json" | awk '{print $1}')
COMPATIBILITY_SHA=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
POLICY_SHA=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg reconciliation "$RECONCILIATION_SHA" --arg artifact "$ARTIFACT_MANIFEST_SHA" --arg procedure "$PROCEDURE_SHA" --arg compatibility "$COMPATIBILITY_SHA" --arg policy "$POLICY_SHA" \
  --arg evaluated "$EVIDENCE_COLLECTED" --arg valid "$EVIDENCE_VALID" --arg node1 "$TMP/node1-topology.json" --arg node1_sha "$NODE1_SNAPSHOT_SHA" --arg node2 "$TMP/node2-topology.json" --arg node2_sha "$NODE2_SNAPSHOT_SHA" '
  {schema_version:"1.0",status:"ready_for_approval",patch_id:"39034528",target:{family:"database",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$node1,sha256:$node1_sha,host:"node1",collected_at:$evaluated,valid_until:$valid},{path:$node2,sha256:$node2_sha,host:"node2",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$reconciliation,artifact_manifest_sha256:$artifact,procedure_validation_sha256:$procedure,compatibility_sha256:$compatibility,policy_sha256:$policy}}
  ' >"$TMP/readiness.json"

NOW=$(date -u +%s)
WINDOW_START=$(date -u -r "$((NOW-60))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW-60))" '+%Y-%m-%dT%H:%M:%SZ')
WINDOW_END=$(date -u -r "$((NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ')

plan() { OPU_PLAN_STATE_DIR="$PLAN_STATE" "$PLAN_TOOL" "$@"; }
create_plan() {
  local plan_id=$1
  plan create --plan-id "$plan_id" --requester patch-admin \
    --readiness "$TMP/readiness.json" --reconciliation "$TMP/reconciliation.json" \
    --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" \
    --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" \
    --recovery-evidence "$TMP/recovery.json" --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$plan_id" --actor dba-approver --approval-ticket TEST-RAC-39034528 >/dev/null
  plan authorize --plan-id "$plan_id" --actor patch-operator >/dev/null
  # Independent simulated target scenario; lifecycle tests cover retained reservations.
  retire_fixture_plans "$PLAN_STATE"
  plan dispatch --plan-id "$plan_id" --actor patch-operator >/dev/null
}

execute_task() {
  local plan_id=$1 task_id=$2 node_name=$3
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_RAC_DATABASE_STATE_DIR="$EXECUTION_STATE" \
  OPU_RAC_DATABASE_TEST_MODE=1 \
  OPU_RAC_DATABASE_TEST_HOSTNAME="$node_name" \
  OPU_RAC_DATABASE_TEST_OWNER="$OWNER" \
  OPU_RAC_DATABASE_WAIT_ATTEMPTS=2 \
  OPU_TEST_RAC_RUNTIME="$RUNTIME_ROOT" \
  OPU_TEST_RAC_DB_PATH="$TEST_DB_PATH" \
    bash "$EXECUTOR" execute --plan-id "$plan_id" --task-id "$task_id" --actor rac-worker --lease-seconds 30
}

run_expected() {
  local plan_id=$1 expected_stage=$2 expected_node=$3 task_json task_id
  task_json=$(plan next --plan-id "$plan_id")
  task_id=$(jq -r '.task_id' <<<"$task_json")
  [ "$(jq -r '.stage' <<<"$task_json")" = "$expected_stage" ]
  [ "$(jq -r '.node' <<<"$task_json")" = "$expected_node" ]
  execute_task "$plan_id" "$task_id" "$expected_node"
}

execute_rollback_task() {
  local plan_id=$1 task_id=$2 node_name=$3
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_RAC_DATABASE_ROLLBACK_STATE_DIR="$ROLLBACK_EXECUTION_STATE" \
  OPU_RAC_DATABASE_TEST_MODE=1 \
  OPU_RAC_DATABASE_TEST_HOSTNAME="$node_name" \
  OPU_RAC_DATABASE_TEST_OWNER="$OWNER" \
  OPU_RAC_DATABASE_WAIT_ATTEMPTS=2 \
  OPU_TEST_RAC_RUNTIME="$RUNTIME_ROOT" \
  OPU_TEST_RAC_DB_PATH="$TEST_DB_PATH" \
    bash "$ROLLBACK_EXECUTOR" execute --plan-id "$plan_id" --task-id "$task_id" --actor rac-rollback-worker --lease-seconds 30
}

run_rollback_expected() {
  local plan_id=$1 expected_stage=$2 expected_node=$3 task_json task_id
  task_json=$(plan next --plan-id "$plan_id")
  task_id=$(jq -r '.task_id' <<<"$task_json")
  [ "$(jq -r '.stage' <<<"$task_json")" = "$expected_stage" ]
  [ "$(jq -r '.node' <<<"$task_json")" = "$expected_node" ]
  execute_rollback_task "$plan_id" "$task_id" "$expected_node"
}

create_rollback_plan() {
  local rollback_id=$1
  plan create-rollback --plan-id "$rollback_id" --requester rollback-admin \
    --source-plan-id rac-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$rollback_id" --actor rollback-approver --approval-ticket TEST-RAC-ROLLBACK-39034528 >/dev/null
  plan authorize --plan-id "$rollback_id" --actor rollback-operator >/dev/null
  # Independent simulated target scenario; lifecycle tests cover retained reservations.
  retire_fixture_plans "$PLAN_STATE"
  plan dispatch --plan-id "$rollback_id" --actor rollback-operator >/dev/null
}

verify_artifact_manifest() {
  local evidence_json=$1 evidence_dir entry artifact_path expected_sha artifact_parent
  jq -e '(.artifacts | length) > 0 and any(.artifacts[]; .path | endswith("/claim.json"))' <<<"$evidence_json" >/dev/null
  evidence_dir=$(CDPATH= cd -- "$(dirname -- "$(jq -r '.logs.stdout.path' <<<"$evidence_json")")" && pwd -P)
  while IFS= read -r entry; do
    artifact_path=$(jq -r '.path' <<<"$entry"); expected_sha=$(jq -r '.sha256' <<<"$entry")
    [ -f "$artifact_path" ] && [ ! -L "$artifact_path" ]
    artifact_parent=$(CDPATH= cd -- "$(dirname -- "$artifact_path")" && pwd -P)
    [ "$artifact_parent" = "$evidence_dir" ]
    [ "$(sha256sum "$artifact_path" | awk '{print $1}')" = "$expected_sha" ]
  done < <(jq -c '.artifacts[]' <<<"$evidence_json")
}

assert_health_drift_blocks() {
  local plan_id=$1 stage=$2 node=$3 mode=$4 task task_id drift before rc
  shift 4
  task=$(plan next --plan-id "$plan_id")
  task_id=$(jq -r '.task_id' <<<"$task")
  [ "$(jq -r '.stage' <<<"$task")" = "$stage" ]
  for drift in "$@"; do
    before=$(wc -l <"$RUNTIME_ROOT/native-mutations.log")
    printf '%s\n' "$drift" >"$RUNTIME_ROOT/health-drift"
    set +e
    if [ "$mode" = apply ]; then
      execute_task "$plan_id" "$task_id" "$node" >"$TMP/$plan_id-$stage-$drift.json" 2>"$TMP/drift.stderr"
    else
      execute_rollback_task "$plan_id" "$task_id" "$node" >"$TMP/$plan_id-$stage-$drift.json" 2>"$TMP/drift.stderr"
    fi
    rc=$?
    set -e
    rm "$RUNTIME_ROOT/health-drift"
    # Apply preserves the stage exit code; the rollback CLI returns 1 for any
    # failed task. Both must publish the exact guard failure in sealed evidence.
    if [ "$rc" -eq 0 ] || ! jq -e '.status == "failed" and .exit_code == 65 and .outcome_class == "no_mutation"' "$TMP/$plan_id-$stage-$drift.json" >/dev/null; then
      printf '%s rejected live %s with unexpected rc=%s or evidence\n' "$stage" "$drift" "$rc" >&2
      cat "$TMP/$plan_id-$stage-$drift.json" "$TMP/drift.stderr" >&2
      exit 1
    fi
    [ "$(wc -l <"$RUNTIME_ROOT/native-mutations.log")" -eq "$before" ]
    [ "$(cat "$RUNTIME_ROOT/node1.state")" = running ] && [ "$(cat "$RUNTIME_ROOT/node2.state")" = running ]
    plan status --plan-id "$plan_id" | jq -e '.state == "paused"' >/dev/null
    # The rejection made no new service/binary mutation; retain that attempt
    # and prove the normal task can still proceed after a supported retry.
    plan retry-task --plan-id "$plan_id" --task-id "$task_id" --actor retry-operator >/dev/null
  done
}

# A nonzero OPatch result after mutation starts must pause with unknown binary
# state and must not restart the stopped instance automatically.
create_plan rac-opatch-failure
run_expected rac-opatch-failure rac_precheck node1 >/dev/null
assert_health_drift_blocks rac-opatch-failure rac_drain node1 apply database instance role cdb unknown-cdb
run_expected rac-opatch-failure rac_drain node1 >/dev/null
assert_health_drift_blocks rac-opatch-failure rac_stop node1 apply role
run_expected rac-opatch-failure rac_stop node1 >/dev/null
touch "$FAIL_OPATCH"
failure_task=$(plan next --plan-id rac-opatch-failure)
failure_task_id=$(jq -r '.task_id' <<<"$failure_task")
if execute_task rac-opatch-failure "$failure_task_id" node1 >/dev/null 2>&1; then
  echo 'RAC executor reported simulated OPatch failure as successful' >&2
  exit 1
fi
plan status --plan-id rac-opatch-failure | jq -e '.state == "paused"' >/dev/null
[ "$(cat "$RUNTIME_ROOT/node1.state")" = stopped ]
[ ! -f "$RUNTIME_ROOT/patch-node1.state" ]
jq -e '.status == "failed" and .outcome_class == "binary_state_unknown" and .postcondition.status == "unknown"' \
  "$EXECUTION_STATE/plans/rac-opatch-failure/nodes/node1/tasks/$failure_task_id/evidence.json" >/dev/null
verify_artifact_manifest "$(cat "$EXECUTION_STATE/plans/rac-opatch-failure/nodes/node1/tasks/$failure_task_id/evidence.json")"
rm -f "$FAIL_OPATCH"
printf 'running\n' >"$RUNTIME_ROOT/node1.state"

# Execute the complete two-node rolling sequence and the one-time cluster
# datapatch/final-validation tail.
create_plan rac-success
for node_name in node1 node2; do
  for stage_name in rac_precheck rac_drain rac_stop rac_opatch_apply rac_start rac_node_validate; do
    result=$(run_expected rac-success "$stage_name" "$node_name")
    jq -e --arg stage "$stage_name" '.status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.record_sha256 | test("^[a-f0-9]{64}$"))' <<<"$result" >/dev/null
    verify_artifact_manifest "$result"
  done
done
run_expected rac-success cluster_datapatch node1 >/dev/null
run_expected rac-success cluster_final_validate node1 >/dev/null

plan status --plan-id rac-success | jq -e '.state == "succeeded"' >/dev/null
[ -f "$RUNTIME_ROOT/patch-node1.state" ] && [ -f "$RUNTIME_ROOT/patch-node2.state" ]
[ -f "$RUNTIME_ROOT/datapatch.state" ]
[ "$(cat "$RUNTIME_ROOT/node1.state")" = running ] && [ "$(cat "$RUNTIME_ROOT/node2.state")" = running ]

# A paused/partial apply is never eligible to authorize a rollback plan.
if plan create-rollback --plan-id rollback-from-partial --requester rollback-admin \
  --source-plan-id rac-opatch-failure --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null 2>&1; then
  echo 'controller created RAC rollback from a paused source apply' >&2
  exit 1
fi

# Rollback is a separately approved child plan. It derives reverse node order
# and a sealed source-apply lineage; source workers cannot approve it.
plan create-rollback --plan-id rac-rollback-failure --requester rollback-admin \
  --source-plan-id rac-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
if plan approve --plan-id rac-rollback-failure --actor rac-worker --approval-ticket BAD-SELF-ROLLBACK >/dev/null 2>&1; then
  echo 'source apply worker approved the RAC rollback child plan' >&2
  exit 1
fi
plan approve --plan-id rac-rollback-failure --actor rollback-approver --approval-ticket TEST-RAC-ROLLBACK-FAILURE >/dev/null
plan authorize --plan-id rac-rollback-failure --actor rollback-operator >/dev/null
# Independent simulated target scenario; lifecycle tests cover retained reservations.
retire_fixture_plans "$PLAN_STATE"
plan dispatch --plan-id rac-rollback-failure --actor rollback-operator >/dev/null
jq -e '.procedure.adapter == "database_rac_opatch_rollback" and .nodes == ["node2","node1"] and (.source_apply.lineage | length) == 14' \
  "$PLAN_STATE/plans/rac-rollback-failure/plan.json" >/dev/null

run_rollback_expected rac-rollback-failure rac_rollback_precheck node2 >/dev/null
assert_health_drift_blocks rac-rollback-failure rac_rollback_drain node2 rollback database instance role cdb unknown-cdb
run_rollback_expected rac-rollback-failure rac_rollback_drain node2 >/dev/null
assert_health_drift_blocks rac-rollback-failure rac_rollback_stop node2 rollback role
run_rollback_expected rac-rollback-failure rac_rollback_stop node2 >/dev/null
touch "$FAIL_ROLLBACK"
rollback_failure_task=$(plan next --plan-id rac-rollback-failure)
rollback_failure_task_id=$(jq -r '.task_id' <<<"$rollback_failure_task")
if execute_rollback_task rac-rollback-failure "$rollback_failure_task_id" node2 >/dev/null 2>&1; then
  echo 'RAC rollback executor reported simulated OPatch rollback failure as successful' >&2
  exit 1
fi
plan status --plan-id rac-rollback-failure | jq -e '.state == "paused"' >/dev/null
[ "$(cat "$RUNTIME_ROOT/node2.state")" = stopped ]
[ -f "$RUNTIME_ROOT/patch-node2.state" ]
jq -e '.status == "failed" and .outcome_class == "binary_state_unknown" and .postcondition.status == "unknown"' \
  "$ROLLBACK_EXECUTION_STATE/plans/rac-rollback-failure/nodes/node2/tasks/$rollback_failure_task_id/evidence.json" >/dev/null
rm -f "$FAIL_ROLLBACK"
printf 'running\n' >"$RUNTIME_ROOT/node2.state"

# A fresh rollback child plan executes both nodes in reverse apply order, runs
# datapatch once on the coordinator, and proves binary/SQL rollback completion.
create_rollback_plan rac-rollback-success
for node_name in node2 node1; do
  for stage_name in rac_rollback_precheck rac_rollback_drain rac_rollback_stop rac_opatch_rollback rac_rollback_start rac_rollback_node_validate; do
    result=$(run_rollback_expected rac-rollback-success "$stage_name" "$node_name")
    jq -e --arg stage "$stage_name" '
      .collector.name == "oracle.database.rac.node.rollback.executor" and .intent == "patch_rollback" and
      .status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and
      (.source_apply.lineage_sha256 | test("^[a-f0-9]{64}$")) and (.record_sha256 | test("^[a-f0-9]{64}$"))
    ' <<<"$result" >/dev/null
    verify_artifact_manifest "$result"
  done
done
run_rollback_expected rac-rollback-success rac_rollback_datapatch node1 >/dev/null
run_rollback_expected rac-rollback-success rac_rollback_final_validate node1 >/dev/null
plan status --plan-id rac-rollback-success | jq -e '.state == "succeeded"' >/dev/null
[ ! -f "$RUNTIME_ROOT/patch-node1.state" ] && [ ! -f "$RUNTIME_ROOT/patch-node2.state" ]
[ "$(cat "$RUNTIME_ROOT/datapatch.state")" = ROLLBACK ]
[ "$(tr '\n' ' ' <"$RUNTIME_ROOT/rollback-order.log")" = 'node2 node1 ' ]
[ "$(cat "$RUNTIME_ROOT/node1.state")" = running ] && [ "$(cat "$RUNTIME_ROOT/node2.state")" = running ]

# A completed source result cannot be changed and then reused as authority.
source_validation_task=$(find "$PLAN_STATE/plans/rac-success/tasks" -type f -name '*rac-node-validate-node1.json' -print -quit)
cp "$source_validation_task" "$TMP/source-validation.original"
jq '.completed_at_epoch += 1' "$source_validation_task" >"$source_validation_task.tmp"
mv "$source_validation_task.tmp" "$source_validation_task"
if plan create-rollback --plan-id rollback-from-tampered-source --requester rollback-admin \
  --source-plan-id rac-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null 2>&1; then
  echo 'controller accepted a tampered RAC source task result' >&2
  exit 1
fi
cp "$TMP/source-validation.original" "$source_validation_task"

printf '%s\n' 'RAC database apply and separately approved rollback executor test passed'
