#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-grid-node.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

PLAN_TOOL="$ROOT/bin/opu-patch-plan"
EXECUTOR="$ROOT/bin/opu-grid-node-patch"
PLAN_STATE="$TMP/plan-state"
EXECUTION_STATE="$TMP/execution-state"
GRID_PATH="$TMP/grid"
PATCH_DIR="$TMP/patch/39034528"
RECOVERY_ROOT="$TMP/recovery"
RUNTIME_ROOT="$TMP/runtime"
OWNER=$(id -un)

mkdir -p "$GRID_PATH/bin" "$GRID_PATH/OPatch" "$GRID_PATH/jdk/bin" \
  "$GRID_PATH/crs/install" "$GRID_PATH/rdbms/install" "$PATCH_DIR/etc/config" \
  "$RECOVERY_ROOT" "$RUNTIME_ROOT"

cat >"$GRID_PATH/bin/crsctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  'query crs activeversion -f')
    printf '%s\n' \
      'Oracle Clusterware active version on the cluster is [19.0.0.0.0].' \
      'The cluster upgrade state is [NORMAL].' \
      'The cluster active patch level is [1].'
    ;;
  'query crs releasepatch') printf '%s\n' 'Oracle Clusterware release patch level is [1].' ;;
  'check crs') printf '%s\n' 'CRS-4638: Oracle High Availability Services is online' ;;
  'check cluster -all') printf '%s\n' 'CRS-4537: Cluster Ready Services is online' ;;
  'stat res -t') printf '%s\n' 'Name Target State Server' 'ora.asm ONLINE ONLINE node1' ;;
  'query css votedisk') printf '%s\n' '1. ONLINE 0000 (/dev/test) [DATA]' 'Located 1 voting disk(s).' ;;
  'query crs softwareversion -all')
    printf '%s\n' \
      'Oracle Clusterware version on node [node1] is [19.0.0.0.0]' \
      'Oracle Clusterware version on node [node2] is [19.0.0.0.0]'
    ;;
  *) printf 'unsupported fake crsctl command: %s\n' "$*" >&2; exit 64 ;;
esac
EOF

cat >"$GRID_PATH/bin/ocrcheck" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' 'Status of Oracle Cluster Registry is OK'
EOF

cat >"$GRID_PATH/bin/srvctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}:${2:-}" in
  status:asm) printf 'ASM is running on node %s\n' "${OPU_GRID_NODE_TEST_HOST:-node1}" ;;
  status:listener) printf 'Listener LISTENER is running on node %s\n' "${OPU_GRID_NODE_TEST_HOST:-node1}" ;;
  *) exit 64 ;;
esac
EOF

cat >"$GRID_PATH/bin/olsnodes" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' 'node1 Active Hub' 'node2 Active Hub'
EOF

cat >"$GRID_PATH/crs/install/rootcrs.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_GRID_NODE_TEST_HOST:-node1}
case "${1:-}" in
  -prepatch)
    [ ! -f "$OPU_TEST_GRID_RUNTIME/fail-prepatch" ] || { printf 'simulated prepatch failure\n' >&2; exit 72; }
    printf 'quiesced\n' >"$OPU_TEST_GRID_RUNTIME/prepatch-$node_name.state"
    ;;
  -postpatch)
    printf 'online\n' >"$OPU_TEST_GRID_RUNTIME/postpatch-$node_name.state"
    ;;
  *) exit 64 ;;
esac
EOF

cat >"$GRID_PATH/rdbms/install/rootadd_rdbms.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_GRID_NODE_TEST_HOST:-node1}
printf 'complete\n' >"$OPU_TEST_GRID_RUNTIME/rootadd-$node_name.state"
EOF

cat >"$GRID_PATH/OPatch/opatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_GRID_NODE_TEST_HOST:-node1}
patch_state="$OPU_TEST_GRID_RUNTIME/patch-$node_name.state"
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$patch_state" ] && printf '%s\n' '39034528;Grid Release Update' || true ;;
  prereq) printf '%s\n' 'Prereq "checkConflictAgainstOHWithDetail" passed.' ;;
  apply)
    [ ! -f "$OPU_TEST_GRID_RUNTIME/fail-opatch" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf 'installed\n' >"$patch_state"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
EOF

cat >"$GRID_PATH/jdk/bin/java" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 750 "$GRID_PATH/bin/crsctl" "$GRID_PATH/bin/ocrcheck" "$GRID_PATH/bin/srvctl" \
  "$GRID_PATH/bin/olsnodes" "$GRID_PATH/crs/install/rootcrs.sh" \
  "$GRID_PATH/rdbms/install/rootadd_rdbms.sh" "$GRID_PATH/OPatch/opatch" "$GRID_PATH/jdk/bin/java"

cat >"$PATCH_DIR/etc/config/actions.xml" <<'EOF'
<patch patchID="39034528"><actions/></patch>
EOF
cat >"$PATCH_DIR/etc/config/inventory.xml" <<'EOF'
<patch patchID="39034528"><description>Grid Release Update</description><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>
EOF
printf '%s\n' \
  'Grid rolling patch test README' \
  'rootcrs.sh -prepatch' \
  'opatch apply' \
  'rootadd_rdbms.sh' \
  'rootcrs.sh -postpatch' >"$PATCH_DIR/README.txt"
"$ROOT/bin/opu-artifact-inspect" --artifact "$PATCH_DIR" --output "$TMP/artifact.json" >/dev/null
ARTIFACT_SHA=$(jq -r '.artifact.sha256' "$TMP/artifact.json")
README_SHA=$(sha256sum "$PATCH_DIR/README.txt" | awk '{print $1}')

for asset_name in snapshot.json bundle.json grid-home.tar.gz inventory.tar.gz oraInst.loc ocr.backup node1.olr node2.olr checksum.log grid-archive.log inventory-archive.log ocr-backup.log crs-health.log active-version.log ocrcheck.log voting-disks.log node1-olr.log node2-olr.log; do
  printf 'sealed Grid recovery evidence\n' >"$RECOVERY_ROOT/$asset_name"
done
ASSET_SHA=$(sha256sum "$RECOVERY_ROOT/grid-home.tar.gz" | awk '{print $1}')
sha256sum "$RECOVERY_ROOT/grid-home.tar.gz" "$RECOVERY_ROOT/inventory.tar.gz" \
  "$RECOVERY_ROOT/oraInst.loc" "$RECOVERY_ROOT/ocr.backup" \
  "$RECOVERY_ROOT/node1.olr" "$RECOVERY_ROOT/node2.olr" >"$RECOVERY_ROOT/SHA256SUMS"
CHECKSUM_SHA=$(sha256sum "$RECOVERY_ROOT/SHA256SUMS" | awk '{print $1}')
jq -cn --arg grid_target "$GRID_PATH" --arg owner "$OWNER" --arg root "$RECOVERY_ROOT" --arg asset "$ASSET_SHA" --arg checksum "$CHECKSUM_SHA" --arg collected_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '
  {schema_version:"1.0",collector:{name:"oracle.grid.recovery.evidence",version:"1"},status:"passed",collected_at:$collected_at,target:{grid_home:$grid_target,owner:$owner,nodes:["node1","node2"],central_inventory:$root,oraInst_loc:($root+"/oraInst.loc")},source_snapshot:{path:($root+"/snapshot.json"),sha256:$asset},source_bundle:{path:($root+"/bundle.json"),sha256:$asset,record_sha256:$asset},backup:{checksum_manifest:{path:($root+"/SHA256SUMS"),sha256:$checksum},grid_home_archive:{path:($root+"/grid-home.tar.gz"),sha256:$asset},central_inventory_archive:{path:($root+"/inventory.tar.gz"),sha256:$asset},oraInst_loc:{path:($root+"/oraInst.loc"),sha256:$asset},ocr_backup:{path:($root+"/ocr.backup"),sha256:$asset},olr_backups:[{node:"node1",path:($root+"/node1.olr"),sha256:$asset},{node:"node2",path:($root+"/node2.olr"),sha256:$asset}]},verification:{checksum_log:{path:($root+"/checksum.log"),sha256:$asset},grid_home_archive_log:{path:($root+"/grid-archive.log"),sha256:$asset},central_inventory_archive_log:{path:($root+"/inventory-archive.log"),sha256:$asset},ocr_backup_log:{path:($root+"/ocr-backup.log"),sha256:$asset},crs_health_log:{path:($root+"/crs-health.log"),sha256:$asset},active_version_log:{path:($root+"/active-version.log"),sha256:$asset},ocrcheck_log:{path:($root+"/ocrcheck.log"),sha256:$asset},voting_disks_log:{path:($root+"/voting-disks.log"),sha256:$asset},olr_backup_logs:[{node:"node1",path:($root+"/node1-olr.log"),sha256:$asset},{node:"node2",path:($root+"/node2-olr.log"),sha256:$asset}]}}
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
jq -n --arg grid_target "$GRID_PATH" --arg owner "$OWNER" \
  --arg node1 "$TMP/node1-topology.json" --arg node1_sha "$NODE1_SNAPSHOT_SHA" --arg node2 "$TMP/node2-topology.json" --arg node2_sha "$NODE2_SNAPSHOT_SHA" '
  {schema_version:"1.0",status:"consistent",expected_nodes:["node1","node2"],cluster:{grid_home:$grid_target,runtime:{status:"healthy",active_version:"19.0.0.0.0",upgrade_state:"NORMAL",active_patch_level:"1"}},oracle_homes:[{path:$grid_target,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]}],databases:[],snapshot_evidence:[{path:$node1,sha256:$node1_sha},{path:$node2,sha256:$node2_sha}]}
  ' >"$TMP/reconciliation.json"
jq -n --arg artifact "$ARTIFACT_SHA" --arg readme "$README_SHA" '
  {schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"39034528",artifact_sha256:$artifact,target:{family:"grid",method:"opatch",topology:"grid_rolling",platform_id:"226"},execution:{adapter:"grid_rolling_opatch",operations:["grid_rootcrs_prepatch","grid_opatch_apply","grid_rootadd_rdbms","grid_rootcrs_postpatch"]},required_opatch_version:"12.2.0.1.49",oracle_references:[{kind:"patch_readme",identifier:"test Grid README",sha256:$readme}],rollback:{mode:"opatch_rollback",precondition:"separate approved recovery plan"}}}
  ' >"$TMP/procedure.json"
jq -n --arg home "$GRID_PATH" --arg digest "$ARTIFACT_SHA" '{schema_version:"1.0",status:"passed",patch_id:"39034528",artifact_sha256:$digest,target:{family:"grid",platform_id:"226"},checks:["node1","node2"] | map({node:.,home:$home,owner:"grid",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.49",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}})}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"
RECONCILIATION_SHA=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}')
ARTIFACT_MANIFEST_SHA=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
PROCEDURE_SHA=$(sha256sum "$TMP/procedure.json" | awk '{print $1}')
COMPATIBILITY_SHA=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
POLICY_SHA=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg reconciliation "$RECONCILIATION_SHA" --arg artifact "$ARTIFACT_MANIFEST_SHA" --arg procedure "$PROCEDURE_SHA" --arg compatibility "$COMPATIBILITY_SHA" --arg policy "$POLICY_SHA" \
  --arg evaluated "$EVIDENCE_COLLECTED" --arg valid "$EVIDENCE_VALID" --arg node1 "$TMP/node1-topology.json" --arg node1_sha "$NODE1_SNAPSHOT_SHA" --arg node2 "$TMP/node2-topology.json" --arg node2_sha "$NODE2_SNAPSHOT_SHA" '
  {schema_version:"1.0",status:"ready_for_approval",patch_id:"39034528",target:{family:"grid",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$node1,sha256:$node1_sha,host:"node1",collected_at:$evaluated,valid_until:$valid},{path:$node2,sha256:$node2_sha,host:"node2",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$reconciliation,artifact_manifest_sha256:$artifact,procedure_validation_sha256:$procedure,compatibility_sha256:$compatibility,policy_sha256:$policy}}
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
  plan approve --plan-id "$plan_id" --actor dba-approver --approval-ticket TEST-GRID-39034528 >/dev/null
  plan authorize --plan-id "$plan_id" --actor patch-operator >/dev/null
  plan dispatch --plan-id "$plan_id" --actor patch-operator >/dev/null
}

execute_task() {
  local plan_id=$1 task_id=$2 node_name=$3
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_GRID_NODE_STATE_DIR="$EXECUTION_STATE" \
  OPU_GRID_NODE_TEST_MODE=1 \
  OPU_GRID_NODE_TEST_HOST="$node_name" \
  OPU_GRID_NODE_TEST_OWNER="$OWNER" \
  OPU_TEST_GRID_RUNTIME="$RUNTIME_ROOT" \
    bash "$EXECUTOR" execute --plan-id "$plan_id" --task-id "$task_id" --actor grid-worker --lease-seconds 30
}

run_expected() {
  local plan_id=$1 expected_stage=$2 expected_node=$3 task_json task_id
  task_json=$(plan next --plan-id "$plan_id")
  task_id=$(jq -r '.task_id' <<<"$task_json")
  [ "$(jq -r '.stage' <<<"$task_json")" = "$expected_stage" ]
  [ "$(jq -r '.node' <<<"$task_json")" = "$expected_node" ]
  execute_task "$plan_id" "$task_id" "$expected_node"
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

# A failed rootcrs prepatch must pause for typed recovery. It must never invoke
# postpatch automatically.
create_plan grid-prepatch-failure
run_expected grid-prepatch-failure grid_precheck node1 >/dev/null
touch "$RUNTIME_ROOT/fail-prepatch"
failure_task=$(plan next --plan-id grid-prepatch-failure)
failure_task_id=$(jq -r '.task_id' <<<"$failure_task")
if execute_task grid-prepatch-failure "$failure_task_id" node1 >/dev/null 2>&1; then
  echo 'Grid executor reported simulated rootcrs prepatch failure as successful' >&2
  exit 1
fi
plan status --plan-id grid-prepatch-failure | jq -e '.state == "paused"' >/dev/null
[ ! -f "$RUNTIME_ROOT/postpatch-node1.state" ]
jq -e '.status == "failed" and .outcome_class == "recovery_required" and .postcondition.status == "unknown"' \
  "$EXECUTION_STATE/plans/grid-prepatch-failure/tasks/$failure_task_id/evidence.json" >/dev/null
verify_artifact_manifest "$(cat "$EXECUTION_STATE/plans/grid-prepatch-failure/tasks/$failure_task_id/evidence.json")"
rm -f "$RUNTIME_ROOT/fail-prepatch"

# Execute the full two-node manual README-derived sequence and coordinator
# validation.
create_plan grid-success
for node_name in node1 node2; do
  for stage_name in grid_precheck grid_rootcrs_prepatch grid_opatch_apply grid_rootadd_rdbms grid_rootcrs_postpatch grid_node_validate; do
    result=$(run_expected grid-success "$stage_name" "$node_name")
    jq -e --arg stage "$stage_name" '.status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.record_sha256 | test("^[a-f0-9]{64}$"))' <<<"$result" >/dev/null
    verify_artifact_manifest "$result"
  done
done
if ! run_expected grid-success grid_cluster_final_validate node1 >"$TMP/grid-cluster-final-result.json"; then
  final_stderr="$EXECUTION_STATE/plans/grid-success/tasks/013-grid-cluster-final-validate-node1/stderr.log"
  [ ! -f "$final_stderr" ] || { printf '%s\n' 'Grid cluster final validation stderr:' >&2; sed -n '1,160p' "$final_stderr" >&2; }
  exit 1
fi

plan status --plan-id grid-success | jq -e '.state == "succeeded"' >/dev/null
[ -f "$RUNTIME_ROOT/patch-node1.state" ] && [ -f "$RUNTIME_ROOT/patch-node2.state" ]
[ -f "$RUNTIME_ROOT/postpatch-node1.state" ] && [ -f "$RUNTIME_ROOT/postpatch-node2.state" ]
printf '%s\n' 'Grid local node executor test passed'
