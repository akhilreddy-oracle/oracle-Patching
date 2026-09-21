#!/usr/bin/env bash
# Data Guard standby-first order sealing into immutable apply plans:
# create --dataguard-order accepts only a sealed plan-order result backed by a
# passing dataguard_standby_first readiness gate, seals its digest into
# plan.json, and dispatch re-verifies the sealed order fail-closed.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-plan-dg.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
PLAN="$ROOT/bin/opu-patch-plan"; digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
now=$(date -u +%s)
start=$(date -u -r "$((now-60))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now-60)) '+%Y-%m-%dT%H:%M:%SZ')
end=$(date -u -r "$((now+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now+3600)) '+%Y-%m-%dT%H:%M:%SZ')
collected=$(date -u -r "$now" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'"$now" '+%Y-%m-%dT%H:%M:%SZ')
oracle_home="$TMP/oracle/dbhome_1"
owner=$(id -un)

# Single-node non-CDB database evidence set; real readiness -> plan admission.
jq -n --arg collected "$collected" --arg home "$oracle_home" --arg owner "$owner" --arg digest "$digest" \
  '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,host:{name:"node1.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},oracle_homes:[{path:$home,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest},patch_inventory_source:"opatch_lsinventory_xml",opatch_inventory_xml_sha256:$digest,patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:$home,runtime:{status:"complete",cdb:"NO",database_role:"PRIMARY",open_mode:"READ WRITE",instance_state:"OPEN",invalid_objects:0,sqlpatch_non_success:0,pdb_not_read_write:0,backup_age_minutes:0,fra_space_limit_bytes:100,fra_space_used_bytes:0,guaranteed_restore_points:0}}],warnings:[]}' >"$TMP/node1-snapshot.json"
snapshot_sha=$(sha256sum "$TMP/node1-snapshot.json" | awk '{print $1}')
jq -n --arg home "$oracle_home" --arg owner "$owner" --arg snapshot "$TMP/node1-snapshot.json" --arg snapshot_sha "$snapshot_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["node1"],oracle_homes:[{path:$home,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:$home}],snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha}]}' >"$TMP/reconciliation.json"
jq -n --arg digest "$digest" --arg path "$TMP/artifact-stage" \
  '{artifact:{status:"ready_for_catalog",path:$path,sha256:$digest,patch_ids:["12345678"],platforms:[{id:"226",name:"Linux x86-64",source:"etc/config/inventory.xml",source_sha256:$digest}]}}' >"$TMP/artifact.json"
jq -n --arg digest "$digest" \
  '{schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]},required_opatch_version:"12.2.0.1",oracle_references:[{kind:"patch_readme",identifier:"test-readme",sha256:$digest}],rollback:{mode:"opatch_rollback",precondition:"README"}}}' >"$TMP/procedure.json"
jq -n --arg digest "$digest" --arg home "$oracle_home" \
  '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"node1",home:$home,owner:"oracle",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}}]}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"
mkdir -p "$TMP/backup"
printf 'recoverable backup\n' >"$TMP/backup/backup-piece"
sha256sum "$TMP/backup/backup-piece" >"$TMP/backup/SHA256SUMS"
checksum_sha=$(sha256sum "$TMP/backup/SHA256SUMS" | awk '{print $1}')
printf 'RMAN validation passed\n' >"$TMP/rman.log"
rman_sha=$(sha256sum "$TMP/rman.log" | awk '{print $1}')
jq -cn --arg home "$oracle_home" --arg owner "$owner" --arg root "$TMP/backup" --arg checksum "$checksum_sha" --arg rman_path "$TMP/rman.log" --arg rman_sha "$rman_sha" \
  --arg snapshot "$TMP/node1-snapshot.json" --arg snapshot_sha "$snapshot_sha" --arg observed "$collected" '
  {schema_version:"1.0",collector:{name:"oracle.recovery.evidence",version:"1"},status:"passed",target:{database_unique_name:"ORCL",oracle_home:$home,owner:$owner,oracle_sid:"ORCL"},source_snapshot:{path:$snapshot,sha256:$snapshot_sha},backup:{root:$root,checksum_manifest:{path:($root+"/SHA256SUMS"),sha256:$checksum},files:[],oracle_home_archive:($root+"/backup-piece"),rman_backup_set_keys:[1],selected_recovery_set:{observed_at:$observed,oldest_datafile_backup_completed_at:$observed,age_seconds_at_collection:0,restore_piece_handles:[($root+"/backup-piece")],datafile_backup_sets:[1]},coverage:{datafiles_backed:1,base_datafiles:1,datafiles_current:1,controlfile_records:1,spfile_records:1,outside_root_pieces:0,unavailable_pieces:0}},verification:{rman_log:{path:$rman_path,sha256:$rman_sha}}}
  ' >"$TMP/recovery.tmp"
recovery_record=$(jq -cS . "$TMP/recovery.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$recovery_record" '.record_sha256=$record' "$TMP/recovery.tmp" >"$TMP/recovery.json"

rhash=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}'); ahash=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
phash=$(sha256sum "$TMP/procedure.json" | awk '{print $1}'); chash=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
yhash=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg r "$rhash" --arg a "$ahash" --arg p "$phash" --arg c "$chash" --arg y "$yhash" \
  --arg evaluated "$collected" --arg valid "$end" --arg snapshot "$TMP/node1-snapshot.json" --arg snapshot_sha "$snapshot_sha" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"12345678",target:{family:"database",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha,host:"node1",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$r,artifact_manifest_sha256:$a,procedure_validation_sha256:$p,compatibility_sha256:$c,policy_sha256:$y}}' >"$TMP/readiness-nogates.json"
# Sealed Data Guard observe → evaluate → plan-order chain (real tools; the
# observe fixture sealing pattern is copied from tests/dataguard.sh).
jq -n --arg home "$oracle_home" --arg now "$collected" '
  {schema_version:"1.0",collector:{name:"oracle.dataguard.observe",version:"1"},collected_at:$now,
   target:{oracle_home:$home,oracle_sid:"ORCL",db_unique_name:"ORCL"},
   primary:{database_role:"PRIMARY",open_mode:"READ WRITE",protection_mode:"MAXIMIZE PERFORMANCE"},
   broker:{status:"configured"},
   members:[{db_unique_name:"ORCL_STBY",status:"APPLYING_LOG",transport_lag_seconds:2,apply_lag_seconds:5}]}
' >"$TMP/observe.raw.json"
observe_canonical=$(jq -cS . "$TMP/observe.raw.json")
observe_hash=$(printf '%s' "$observe_canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$observe_hash" '.record_sha256=$hash' "$TMP/observe.raw.json" >"$TMP/observe.json"
jq -n '{schema_version:"1.0",dataguard:{max_transport_lag_seconds:30,max_apply_lag_seconds:60,require_broker:true}}' >"$TMP/dg-policy.json"
"$ROOT/bin/opu-dataguard-evaluate" --observe "$TMP/observe.json" --policy "$TMP/dg-policy.json" --output "$TMP/dg-eval.json" >/dev/null
jq -e '.status == "ready_for_standby_first"' "$TMP/dg-eval.json" >/dev/null
"$ROOT/bin/opu-dataguard-plan-order" --observe "$TMP/observe.json" --evaluation "$TMP/dg-eval.json" --output "$TMP/order.json" >/dev/null
order_record=$(jq -r '.record_sha256' "$TMP/order.json")
order_file_sha=$(sha256sum "$TMP/order.json" | awk '{print $1}')

"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/reconciliation.json" --snapshot "$TMP/node1-snapshot.json" \
  --artifact "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" \
  --policy "$TMP/policy.json" --recovery-evidence "$TMP/recovery.json" --dataguard "$TMP/dg-eval.json" --output "$TMP/readiness-gated.json" >/dev/null
jq -e '.status == "ready_for_approval" and .dataguard_evaluation.maximum_age_seconds == 300 and
  .valid_until == .dataguard_evaluation.valid_until and
  (.valid_until | fromdateiso8601) < (.snapshot_evidence[0].valid_until | fromdateiso8601)' "$TMP/readiness-gated.json" >/dev/null

# Passing Data Guard evidence does not waive the database adapter's explicit
# non-CDB requirement. Bind the changed snapshot correctly so the rejection
# demonstrates container scope, not a stale reconciliation digest.
jq '.databases[0].runtime.cdb = "YES"' "$TMP/node1-snapshot.json" >"$TMP/cdb-snapshot.json"
cdb_sha=$(sha256sum "$TMP/cdb-snapshot.json" | awk '{print $1}')
jq --arg path "$TMP/cdb-snapshot.json" --arg sha "$cdb_sha" '.snapshot_evidence=[{path:$path,sha256:$sha}]' \
  "$TMP/reconciliation.json" >"$TMP/cdb-reconciliation.json"
if "$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/cdb-reconciliation.json" --snapshot "$TMP/cdb-snapshot.json" \
  --artifact "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" \
  --policy "$TMP/policy.json" --dataguard "$TMP/dg-eval.json" --output "$TMP/cdb-readiness.json" >/dev/null; then
  echo 'passing Data Guard evidence admitted an unsupported CDB' >&2; exit 1
fi
jq -e '.status == "blocked" and
  any(.gates[]; .name == "dataguard_standby_first" and .status == "pass") and
  ([.gates[] | select(.status == "blocker") | .name] == ["database_container_scope"])' "$TMP/cdb-readiness.json" >/dev/null

run() { OPU_PLAN_STATE_DIR="$TMP/state" "$PLAN" "$@"; }
create_plan() {
  local plan_id=$1 readiness=$2; shift 2
  run create --plan-id "$plan_id" --requester patch-admin \
    --readiness "$readiness" --reconciliation "$TMP/reconciliation.json" \
    --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" \
    --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" \
    --recovery-evidence "$TMP/recovery.json" --window-start "$start" --window-end "$end" "$@"
}

# create without the flag is unchanged and seals no Data Guard section.
create_plan plan-nodg "$TMP/readiness-nogates.json" >"$TMP/plan-nodg.json"
jq -e '.dataguard == null and .plan_sha256' "$TMP/plan-nodg.json" >/dev/null

# A sealed standby-first order plus a gated readiness seals the order digest.
create_plan plan-dg "$TMP/readiness-gated.json" --dataguard-order "$TMP/order.json" >"$TMP/plan-dg.json"
jq -e --arg record "$order_record" --arg file_sha "$order_file_sha" \
  '.dataguard.order_sha256 == $record and .dataguard.order_file_sha256 == $file_sha and .dataguard.strategy == "standby_first" and .source_documents.dataguard_order.sha256 == $file_sha and .source_documents.dataguard_evaluation.maximum_age_seconds == 300 and .source_documents.dataguard_evaluation.valid_until == .planning_evidence.readiness_valid_until and .plan_sha256' \
  "$TMP/plan-dg.json" >/dev/null

# Readiness without the dataguard_standby_first pass gate must fail closed.
if create_plan plan-dg-ungated "$TMP/readiness-nogates.json" --dataguard-order "$TMP/order.json" >/dev/null 2>&1; then
  echo 'a Data Guard order was sealed without a passing readiness gate' >&2
  exit 1
fi

# A tampered/mismatched order record must fail closed at create.
jq '.order[0].apply_lag_seconds = 1' "$TMP/order.json" >"$TMP/order-tampered.json"
if create_plan plan-dg-tampered "$TMP/readiness-gated.json" --dataguard-order "$TMP/order-tampered.json" >/dev/null 2>&1; then
  echo 'a tampered Data Guard order was sealed into a plan' >&2
  exit 1
fi

# The shorter expiry is valid only with the exact sealed DG evidence binding.
for mutation in 'del(.dataguard_evaluation)' '.dataguard_evaluation.maximum_age_seconds=true' '.dataguard_evaluation.maximum_age_seconds=3601' '.dataguard_evaluation.valid_until=.snapshot_evidence[0].valid_until' '.valid_until=.snapshot_evidence[0].valid_until' '.dataguard_evaluation.sha256=("f"*64)'; do
  jq "$mutation" "$TMP/readiness-gated.json" >"$TMP/readiness-invalid.json"
  if create_plan invalid-dg-binding "$TMP/readiness-invalid.json" --dataguard-order "$TMP/order.json" >/dev/null 2>&1; then
    echo "plan accepted invalid DG freshness binding: $mutation" >&2; exit 1
  fi
done
# A different sealed evaluation/order cannot substitute for readiness's evaluation.
jq '.evidence.evaluation_sha256=("e"*64) | del(.record_sha256)' "$TMP/order.json" >"$TMP/order-other.raw"
other_sha=$(jq -cS . "$TMP/order-other.raw" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg sha "$other_sha" '.record_sha256=$sha' "$TMP/order-other.raw" >"$TMP/order-other.json"
if create_plan other-dg-order "$TMP/readiness-gated.json" --dataguard-order "$TMP/order-other.json" >/dev/null 2>&1; then
  echo 'plan accepted an order from another DG evaluation' >&2; exit 1
fi
# Later approval must reread the exact evaluation; it cannot use cached readiness.
cp "$TMP/dg-eval.json" "$TMP/dg-eval.saved"
printf '\n' >>"$TMP/dg-eval.json"
if run approve --plan-id plan-dg --actor dba-approver --approval-ticket CHG-DG-TAMPER >/dev/null 2>&1; then
  echo 'approval accepted changed DG evaluation bytes' >&2; exit 1
fi
mv "$TMP/dg-eval.saved" "$TMP/dg-eval.json"
# At a later clock, snapshots can remain fresh while DG evidence is expired.
if OPU_PLAN_STATE_DIR="$TMP/state" bash -c '
  . "$1" help >/dev/null
  verify_readiness_current "$2" "$3" "$4" "$5"
' fixture "$PLAN" "$TMP/readiness-gated.json" "$TMP/reconciliation.json" "$TMP/policy.json" "$((now+301))" >/dev/null 2>&1; then
  echo 'plan verifier accepted expired DG evidence with fresh snapshots' >&2; exit 1
fi

# Dispatch re-verifies the sealed order file and refuses a changed one.
run approve --plan-id plan-dg --actor dba-approver --approval-ticket CHG-DG-001
run authorize --plan-id plan-dg --actor patch-operator
cp "$TMP/order.json" "$TMP/order.saved"
printf '\n' >>"$TMP/order.json"
if run dispatch --plan-id plan-dg --actor patch-operator >/dev/null 2>&1; then
  echo 'dispatch accepted a Data Guard order that changed after plan creation' >&2
  exit 1
fi
mv "$TMP/order.saved" "$TMP/order.json"
run dispatch --plan-id plan-dg --actor patch-operator
run next --plan-id plan-dg | jq -e '.task_id == "001-precheck-node1" and .adapter == "database_single_instance_opatch"' >/dev/null

printf '%s\n' 'Data Guard standby-first plan sealing test passed'
