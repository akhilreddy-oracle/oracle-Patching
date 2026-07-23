#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-plan.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
PLAN="$ROOT/bin/opu-patch-plan"; digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
now=$(date -u +%s); start=$(date -u -r "$((now-60))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now-60)) '+%Y-%m-%dT%H:%M:%SZ'); end=$(date -u -r "$((now+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now+3600)) '+%Y-%m-%dT%H:%M:%SZ')
collected=$(date -u -r "$now" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'"$now" '+%Y-%m-%dT%H:%M:%SZ')
grid_path="$TMP/grid"
grid_owner=$(id -un)
mkdir -p "$grid_path" "$TMP/grid-recovery"
for node_name in node1 node2; do
  jq -n --arg node "$node_name" --arg collected "$collected" \
    '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,host:{name:($node+".example")},cluster:{status:"detected",grid_home:null,runtime:{status:"healthy"},nodes:[]},oracle_homes:[],databases:[],warnings:[]}' >"$TMP/$node_name-snapshot.json"
done
node1_snapshot_sha=$(sha256sum "$TMP/node1-snapshot.json" | awk '{print $1}')
node2_snapshot_sha=$(sha256sum "$TMP/node2-snapshot.json" | awk '{print $1}')
jq -n --arg grid_path "$grid_path" --arg grid_owner "$grid_owner" \
  --arg node1 "$TMP/node1-snapshot.json" --arg node1_sha "$node1_snapshot_sha" \
  --arg node2 "$TMP/node2-snapshot.json" --arg node2_sha "$node2_snapshot_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["node1","node2"],cluster:{grid_home:$grid_path,runtime:{status:"healthy",active_version:"19.0.0.0.0",upgrade_state:"NORMAL",active_patch_level:"1"}},oracle_homes:[{path:$grid_path,owner:$grid_owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]}],databases:[],snapshot_evidence:[{path:$node1,sha256:$node1_sha},{path:$node2,sha256:$node2_sha}]}' >"$TMP/reconciliation.json"
jq -n --arg digest "$digest" --arg path "$TMP/artifact-stage" '{artifact:{status:"ready_for_catalog",path:$path,sha256:$digest,patch_ids:["12345678"],platforms:[{id:"226",name:"Linux x86-64",source:"etc/config/inventory.xml",source_sha256:$digest}]}}' >"$TMP/artifact.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$digest,target:{family:"grid",method:"opatch",topology:"grid_rolling",platform_id:"226"},execution:{adapter:"grid_rolling_opatch",operations:["grid_rootcrs_prepatch","grid_opatch_apply","grid_rootadd_rdbms","grid_rootcrs_postpatch"]},required_opatch_version:"12.2.0.1",oracle_references:[{kind:"patch_readme",identifier:"test-readme",sha256:$digest}],rollback:{mode:"opatch_rollback",precondition:"README"}}}' >"$TMP/procedure.json"
jq -n --arg digest "$digest" --arg home "$grid_path" '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"grid",platform_id:"226"},checks:["node1","node2"] | map({node:.,home:$home,owner:"grid",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}})}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"
for asset_name in snapshot.json bundle.json grid-home.tar.gz inventory.tar.gz oraInst.loc ocr.backup node1.olr node2.olr checksum.log grid-archive.log inventory-archive.log ocr-backup.log crs-health.log active-version.log ocrcheck.log voting-disks.log node1-olr.log node2-olr.log; do
  printf 'sealed test evidence\n' >"$TMP/grid-recovery/$asset_name"
done
grid_asset_sha=$(sha256sum "$TMP/grid-recovery/snapshot.json" | awk '{print $1}')
printf '%s  %s\n' "$grid_asset_sha" "$TMP/grid-recovery/grid-home.tar.gz" >"$TMP/grid-recovery/SHA256SUMS"
grid_checksum_sha=$(sha256sum "$TMP/grid-recovery/SHA256SUMS" | awk '{print $1}')
jq -cn --arg grid_path "$grid_path" --arg owner "$grid_owner" --arg root "$TMP/grid-recovery" --arg asset_sha "$grid_asset_sha" --arg checksum_sha "$grid_checksum_sha" --arg digest "$digest" --arg collected_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '
  {schema_version:"1.0",collector:{name:"oracle.grid.recovery.evidence",version:"1"},status:"passed",collected_at:$collected_at,
   target:{grid_home:$grid_path,owner:$owner,nodes:["node1","node2"],central_inventory:$root,oraInst_loc:($root+"/oraInst.loc")},
   source_snapshot:{path:($root+"/snapshot.json"),sha256:$asset_sha},
   source_bundle:{path:($root+"/bundle.json"),sha256:$asset_sha,record_sha256:$digest},
   backup:{checksum_manifest:{path:($root+"/SHA256SUMS"),sha256:$checksum_sha},grid_home_archive:{path:($root+"/grid-home.tar.gz"),sha256:$asset_sha},central_inventory_archive:{path:($root+"/inventory.tar.gz"),sha256:$asset_sha},oraInst_loc:{path:($root+"/oraInst.loc"),sha256:$asset_sha},ocr_backup:{path:($root+"/ocr.backup"),sha256:$asset_sha},olr_backups:[{node:"node1",path:($root+"/node1.olr"),sha256:$asset_sha},{node:"node2",path:($root+"/node2.olr"),sha256:$asset_sha}]},
   verification:{checksum_log:{path:($root+"/checksum.log"),sha256:$asset_sha},grid_home_archive_log:{path:($root+"/grid-archive.log"),sha256:$asset_sha},central_inventory_archive_log:{path:($root+"/inventory-archive.log"),sha256:$asset_sha},ocr_backup_log:{path:($root+"/ocr-backup.log"),sha256:$asset_sha},crs_health_log:{path:($root+"/crs-health.log"),sha256:$asset_sha},active_version_log:{path:($root+"/active-version.log"),sha256:$asset_sha},ocrcheck_log:{path:($root+"/ocrcheck.log"),sha256:$asset_sha},voting_disks_log:{path:($root+"/voting-disks.log"),sha256:$asset_sha},olr_backup_logs:[{node:"node1",path:($root+"/node1-olr.log"),sha256:$asset_sha},{node:"node2",path:($root+"/node2-olr.log"),sha256:$asset_sha}]}}
  ' >"$TMP/grid-recovery.tmp"
grid_recovery_record=$(jq -cS . "$TMP/grid-recovery.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$grid_recovery_record" '.record_sha256=$record' "$TMP/grid-recovery.tmp" >"$TMP/grid-recovery.json"
rhash=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}'); ahash=$(sha256sum "$TMP/artifact.json" | awk '{print $1}'); phash=$(sha256sum "$TMP/procedure.json" | awk '{print $1}'); chash=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}'); yhash=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg r "$rhash" --arg a "$ahash" --arg p "$phash" --arg c "$chash" --arg y "$yhash" \
  --arg evaluated "$collected" --arg valid "$end" \
  --arg node1 "$TMP/node1-snapshot.json" --arg node1_sha "$node1_snapshot_sha" \
  --arg node2 "$TMP/node2-snapshot.json" --arg node2_sha "$node2_snapshot_sha" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"12345678",target:{family:"grid",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$node1,sha256:$node1_sha,host:"node1",collected_at:$evaluated,valid_until:$valid},{path:$node2,sha256:$node2_sha,host:"node2",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$r,artifact_manifest_sha256:$a,procedure_validation_sha256:$p,compatibility_sha256:$c,policy_sha256:$y}}' >"$TMP/readiness.json"
readyhash=$(sha256sum "$TMP/readiness.json" | awk '{print $1}')
run() { OPU_PLAN_STATE_DIR="$TMP/state" "$PLAN" "$@"; }

# lock() must fail fast and report a missing plan as such, not loop through
# its full lock-contention retry window and report a misleading timeout.
set +e
run approve --plan-id does-not-exist --actor someone --approval-ticket CHG-X >/dev/null 2>"$TMP/nonexistent.err"
nonexistent_status=$?
set -e
[ "$nonexistent_status" -eq 66 ] || { echo "approve on a nonexistent plan should exit 66, got $nonexistent_status" >&2; cat "$TMP/nonexistent.err" >&2; exit 1; }
grep -q 'plan does not exist' "$TMP/nonexistent.err" || { echo 'nonexistent plan error message regressed' >&2; exit 1; }
if grep -q 'timed out waiting for plan task lock' "$TMP/nonexistent.err"; then
  echo 'nonexistent plan was misreported as a lock timeout' >&2
  exit 1
fi

# A forged overall pass from the legacy compatibility shape must not produce a
# plan when the independent platform-applicability evidence is absent.
jq 'del(.checks[].applicability_check)' "$TMP/compatibility.json" >"$TMP/legacy-compatibility.json"
legacy_compatibility_sha=$(sha256sum "$TMP/legacy-compatibility.json" | awk '{print $1}')
jq --arg digest "$legacy_compatibility_sha" '.evidence.compatibility_sha256=$digest' "$TMP/readiness.json" >"$TMP/legacy-compatibility-readiness.json"
if run create --plan-id plan-legacy-compatibility --requester patch-admin --readiness "$TMP/legacy-compatibility-readiness.json" --reconciliation "$TMP/reconciliation.json" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/legacy-compatibility.json" --policy "$TMP/policy.json" --recovery-evidence "$TMP/grid-recovery.json" --window-start "$start" --window-end "$end" >/dev/null 2>&1; then
  echo 'legacy compatibility without platform applicability was accepted' >&2
  exit 1
fi

jq '.expected_nodes=["node1.site-a","node1.site-b"]' "$TMP/reconciliation.json" >"$TMP/colliding-reconciliation.json"
colliding_reconciliation_sha=$(sha256sum "$TMP/colliding-reconciliation.json" | awk '{print $1}')
jq --arg digest "$colliding_reconciliation_sha" '.evidence.reconciliation_sha256=$digest' "$TMP/readiness.json" >"$TMP/colliding-readiness.json"
if run create --plan-id plan-colliding-nodes --requester patch-admin --readiness "$TMP/colliding-readiness.json" --reconciliation "$TMP/colliding-reconciliation.json" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" --recovery-evidence "$TMP/grid-recovery.json" --window-start "$start" --window-end "$end" >/dev/null 2>&1; then
  echo 'short-host node collision was accepted' >&2
  exit 1
fi
stale_collected=$(date -u -r "$((now-7200))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now-7200)) '+%Y-%m-%dT%H:%M:%SZ')
stale_valid=$(date -u -r "$((now-3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now-3600)) '+%Y-%m-%dT%H:%M:%SZ')
jq --arg collected "$stale_collected" '.collected_at=$collected' "$TMP/node1-snapshot.json" >"$TMP/stale-node1.json"
jq --arg collected "$stale_collected" '.collected_at=$collected' "$TMP/node2-snapshot.json" >"$TMP/stale-node2.json"
stale_node1_sha=$(sha256sum "$TMP/stale-node1.json" | awk '{print $1}'); stale_node2_sha=$(sha256sum "$TMP/stale-node2.json" | awk '{print $1}')
jq --arg node1 "$TMP/stale-node1.json" --arg node1_sha "$stale_node1_sha" --arg node2 "$TMP/stale-node2.json" --arg node2_sha "$stale_node2_sha" \
  '.snapshot_evidence=[{path:$node1,sha256:$node1_sha},{path:$node2,sha256:$node2_sha}]' "$TMP/reconciliation.json" >"$TMP/stale-reconciliation.json"
stale_reconciliation_sha=$(sha256sum "$TMP/stale-reconciliation.json" | awk '{print $1}')
jq --arg evaluated "$stale_collected" --arg valid "$stale_valid" --arg node1 "$TMP/stale-node1.json" --arg node1_sha "$stale_node1_sha" --arg node2 "$TMP/stale-node2.json" --arg node2_sha "$stale_node2_sha" --arg reconciliation "$stale_reconciliation_sha" \
  '.evaluated_at=$evaluated | .valid_until=$valid | .snapshot_evidence=[{path:$node1,sha256:$node1_sha,host:"node1",collected_at:$evaluated,valid_until:$valid},{path:$node2,sha256:$node2_sha,host:"node2",collected_at:$evaluated,valid_until:$valid}] | .evidence.reconciliation_sha256=$reconciliation' \
  "$TMP/readiness.json" >"$TMP/stale-readiness.json"
if run create --plan-id plan-stale-readiness --requester patch-admin --readiness "$TMP/stale-readiness.json" --reconciliation "$TMP/stale-reconciliation.json" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" --recovery-evidence "$TMP/grid-recovery.json" --window-start "$start" --window-end "$end" >/dev/null 2>&1; then
  echo 'expired topology readiness was accepted for a new plan' >&2
  exit 1
fi
future_collected=$(date -u -r "$((now+600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now+600)) '+%Y-%m-%dT%H:%M:%SZ')
future_valid=$(date -u -r "$((now+4200))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '@'$((now+4200)) '+%Y-%m-%dT%H:%M:%SZ')
jq --arg collected "$future_collected" '.collected_at=$collected' "$TMP/node1-snapshot.json" >"$TMP/future-node1.json"
jq --arg collected "$future_collected" '.collected_at=$collected' "$TMP/node2-snapshot.json" >"$TMP/future-node2.json"
future_node1_sha=$(sha256sum "$TMP/future-node1.json" | awk '{print $1}'); future_node2_sha=$(sha256sum "$TMP/future-node2.json" | awk '{print $1}')
jq --arg node1 "$TMP/future-node1.json" --arg node1_sha "$future_node1_sha" --arg node2 "$TMP/future-node2.json" --arg node2_sha "$future_node2_sha" '.snapshot_evidence=[{path:$node1,sha256:$node1_sha},{path:$node2,sha256:$node2_sha}]' "$TMP/reconciliation.json" >"$TMP/future-reconciliation.json"
future_reconciliation_sha=$(sha256sum "$TMP/future-reconciliation.json" | awk '{print $1}')
jq --arg evaluated "$collected" --arg valid "$future_valid" --arg node1 "$TMP/future-node1.json" --arg node1_sha "$future_node1_sha" --arg node2 "$TMP/future-node2.json" --arg node2_sha "$future_node2_sha" --arg reconciliation "$future_reconciliation_sha" '.evaluated_at=$evaluated | .valid_until=$valid | .snapshot_evidence=[{path:$node1,sha256:$node1_sha,host:"node1",collected_at:$collected,valid_until:$valid},{path:$node2,sha256:$node2_sha,host:"node2",collected_at:$collected,valid_until:$valid}] | .evidence.reconciliation_sha256=$reconciliation' --arg collected "$future_collected" "$TMP/readiness.json" >"$TMP/future-readiness.json"
if run create --plan-id plan-future-readiness --requester patch-admin --readiness "$TMP/future-readiness.json" --reconciliation "$TMP/future-reconciliation.json" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" --recovery-evidence "$TMP/grid-recovery.json" --window-start "$start" --window-end "$end" >/dev/null 2>&1; then
  echo 'future-dated topology readiness was accepted for a new plan' >&2
  exit 1
fi
run create --plan-id plan-snapshot-tamper --requester patch-admin --readiness "$TMP/readiness.json" --reconciliation "$TMP/reconciliation.json" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" --recovery-evidence "$TMP/grid-recovery.json" --window-start "$start" --window-end "$end" >/dev/null
run approve --plan-id plan-snapshot-tamper --actor dba-approver --approval-ticket CHG-SNAPSHOT-TAMPER
cp "$TMP/node1-snapshot.json" "$TMP/node1-snapshot.saved"
printf '\n' >>"$TMP/node1-snapshot.json"
if run authorize --plan-id plan-snapshot-tamper --actor patch-operator >/dev/null 2>&1; then
  echo 'changed topology snapshot was accepted at authorization' >&2
  exit 1
fi
mv "$TMP/node1-snapshot.saved" "$TMP/node1-snapshot.json"
run create --plan-id plan-001 --requester patch-admin --readiness "$TMP/readiness.json" --reconciliation "$TMP/reconciliation.json" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" --recovery-evidence "$TMP/grid-recovery.json" --window-start "$start" --window-end "$end" >"$TMP/plan.json"
jq -e '.nodes == ["node1","node2"] and .artifact.path and .procedure.adapter == "grid_rolling_opatch" and .target.coordinator_node == "node1" and .recovery.manifest_sha256 and .plan_sha256' "$TMP/plan.json" >/dev/null
if run approve --plan-id plan-001 --actor patch-admin --approval-ticket CHG-123 >/dev/null 2>&1; then echo 'self approval was accepted' >&2; exit 1; fi
run approve --plan-id plan-001 --actor dba-approver --approval-ticket CHG-123
run authorize --plan-id plan-001 --actor patch-operator
run dispatch --plan-id plan-001 --actor patch-operator
run next --plan-id plan-001 | jq -e '.task_id == "001-grid-precheck-node1" and .adapter == "grid_rolling_opatch"' >/dev/null
task_count=$(find "$TMP/state/plans/plan-001/tasks" -name '*.json' | wc -l | tr -d ' ')
[ "$task_count" -eq 13 ]
claimed=$(run claim --plan-id plan-001 --task-id 001-grid-precheck-node1 --actor grid-worker-01 --lease-seconds 30)
jq -e '.status == "running" and .claimed_by == "grid-worker-01"' <<<"$claimed" >/dev/null
renewed=$(run renew --plan-id plan-001 --task-id 001-grid-precheck-node1 --actor grid-worker-01 --lease-seconds 60)
jq -e '.status == "running" and (.lease_renewed_at_epoch | type == "number")' <<<"$renewed" >/dev/null
if run claim --plan-id plan-001 --task-id 002-grid-rootcrs-prepatch-node1 --actor grid-worker-02 >/dev/null 2>&1; then echo 'parallel rolling claim was accepted' >&2; exit 1; fi
grid_task_dir="$TMP/grid-task-evidence"
mkdir "$grid_task_dir"
printf 'grid stdout\n' >"$grid_task_dir/stdout.log"; printf 'grid stderr\n' >"$grid_task_dir/stderr.log"
printf '{"claim":"sealed"}\n' >"$grid_task_dir/claim.json"
printf 'outside artifact\n' >"$TMP/outside-grid-artifact.log"
grid_stdout_sha=$(sha256sum "$grid_task_dir/stdout.log" | awk '{print $1}'); grid_stderr_sha=$(sha256sum "$grid_task_dir/stderr.log" | awk '{print $1}')
grid_claim_sha=$(sha256sum "$grid_task_dir/claim.json" | awk '{print $1}'); outside_artifact_sha=$(sha256sum "$TMP/outside-grid-artifact.log" | awk '{print $1}')
grid_plan_sha=$(jq -r '.plan_sha256' "$TMP/plan.json"); grid_recovery_file_sha=$(jq -r '.recovery.manifest_sha256' "$TMP/plan.json")
jq -cn --arg plan_sha "$grid_plan_sha" --arg grid_path "$grid_path" --arg owner "$grid_owner" --arg stdout "$grid_task_dir/stdout.log" --arg stderr "$grid_task_dir/stderr.log" --arg stdout_sha "$grid_stdout_sha" --arg stderr_sha "$grid_stderr_sha" --arg claim "$grid_task_dir/claim.json" --arg claim_sha "$grid_claim_sha" --arg recovery_sha "$grid_recovery_file_sha" --arg recovery_record "$grid_recovery_record" --arg readiness "$readyhash" --arg reconciliation "$rhash" --arg artifact_manifest "$ahash" --arg procedure "$phash" --arg compatibility "$chash" --arg policy "$yhash" --arg time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg digest "$digest" '
  {schema_version:"1.0",collector:{name:"oracle.grid.node.executor",version:"1"},intent:"patch_apply",plan_id:"plan-001",task_id:"001-grid-precheck-node1",plan_sha256:$plan_sha,status:"succeeded",postcondition:{status:"passed",detail:"sealed Grid precheck passed"},outcome_class:"no_mutation",started_at:$time,finished_at:$time,exit_code:0,actor:"grid-worker-01",stage:"grid_precheck",target:{grid_home:$grid_path,owner:$owner,coordinator_node:"node1",node:"node1"},patch:{patch_id:"12345678",artifact_sha256:$digest},source_documents:{readiness:$readiness,reconciliation:$reconciliation,artifact_manifest:$artifact_manifest,procedure_validation:$procedure,compatibility:$compatibility,policy:$policy},recovery:{manifest_sha256:$recovery_sha,record_sha256:$recovery_record},cluster:{active_version:"19.0.0.0.0",upgrade_state:"NORMAL",active_patch_level:"1",release_patch_level:"1"},artifacts:[{path:$claim,sha256:$claim_sha}],logs:{stdout:{path:$stdout,sha256:$stdout_sha},stderr:{path:$stderr,sha256:$stderr_sha}}}
  ' >"$TMP/grid-execution.tmp"
jq --arg path "$TMP/outside-grid-artifact.log" --arg sha "$outside_artifact_sha" '.artifacts[0]={path:$path,sha256:$sha}' "$TMP/grid-execution.tmp" >"$TMP/grid-execution-outside.tmp"
outside_execution_record=$(jq -cS . "$TMP/grid-execution-outside.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$outside_execution_record" '.record_sha256=$record' "$TMP/grid-execution-outside.tmp" >"$grid_task_dir/evidence.json"
if run complete --plan-id plan-001 --task-id 001-grid-precheck-node1 --actor grid-worker-01 --status succeeded --evidence "$grid_task_dir/evidence.json" >/dev/null 2>&1; then
  echo 'Grid evidence accepted an artifact outside its sealed task directory' >&2
  exit 1
fi
grid_execution_record=$(jq -cS . "$TMP/grid-execution.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$grid_execution_record" '.record_sha256=$record' "$TMP/grid-execution.tmp" >"$grid_task_dir/evidence.json"
run complete --plan-id plan-001 --task-id 001-grid-precheck-node1 --actor grid-worker-01 --status succeeded --evidence "$grid_task_dir/evidence.json"
completed_grid_task="$TMP/state/plans/plan-001/tasks/001-grid-precheck-node1.json"
jq -e --arg prefix "$TMP/state/plans/plan-001/evidence/001-grid-precheck-node1/" '
  .status == "succeeded" and (.task_result_sha256 | test("^[a-f0-9]{64}$")) and
  (.evidence_path | startswith($prefix)) and
  (.evidence_custody.manifest_path | startswith($prefix)) and
  (.evidence_custody.manifest_sha256 | test("^[a-f0-9]{64}$")) and
  (.evidence_custody.record_sha256 | test("^[a-f0-9]{64}$"))
' "$completed_grid_task" >/dev/null
[ -f "$(jq -r '.evidence_path' "$completed_grid_task")" ]
[ -f "$(jq -r '.evidence_custody.manifest_path' "$completed_grid_task")" ]
cp "$completed_grid_task" "$TMP/completed-grid-task.original"
jq '.evidence_sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' "$completed_grid_task" >"$completed_grid_task.tmp" && mv "$completed_grid_task.tmp" "$completed_grid_task"
if run next --plan-id plan-001 >/dev/null 2>&1; then
  echo 'tampered completed task result was accepted' >&2
  exit 1
fi
mv "$TMP/completed-grid-task.original" "$completed_grid_task"
run next --plan-id plan-001 | jq -e '.task_id == "002-grid-rootcrs-prepatch-node1"' >/dev/null
run status --plan-id plan-001 | jq -e '.state == "running"' >/dev/null

jq -n --arg collected "$collected" '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,host:{name:"node1.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},oracle_homes:[],databases:[],warnings:[]}' >"$TMP/standalone-snapshot.json"
standalone_snapshot_sha=$(sha256sum "$TMP/standalone-snapshot.json" | awk '{print $1}')
jq -n --arg snapshot "$TMP/standalone-snapshot.json" --arg snapshot_sha "$standalone_snapshot_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["node1"],oracle_homes:[{path:"/opt/oracle/dbhome",owner:"oracle",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"}}],databases:[{db_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome"}],snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha}]}' >"$TMP/standalone-reconciliation.json"
jq -n --arg digest "$digest" --arg path "$TMP/artifact-stage" '{artifact:{status:"ready_for_catalog",path:$path,sha256:$digest,patch_ids:["12345678"],platforms:[{id:"226",name:"Linux x86-64",source:"etc/config/inventory.xml",source_sha256:$digest}]}}' >"$TMP/standalone-artifact.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]},required_opatch_version:"12.2.0.1.51",rollback:{mode:"opatch_rollback",precondition:"README"}}}' >"$TMP/standalone-procedure.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"node1",home:"/opt/oracle/dbhome",owner:"oracle",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.51",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}}]}' >"$TMP/standalone-compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/standalone-policy.json"
jq -cn --arg digest "$digest" --arg snapshot "$TMP/standalone-snapshot.json" --arg snapshot_sha "$standalone_snapshot_sha" --arg observed "$collected" '{schema_version:"1.0",collector:{name:"oracle.recovery.evidence",version:"1"},status:"passed",target:{database_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome",owner:"oracle",oracle_sid:"ORCL"},source_snapshot:{path:$snapshot,sha256:$snapshot_sha},backup:{checksum_manifest:{sha256:$digest},selected_recovery_set:{observed_at:$observed,oldest_datafile_backup_completed_at:$observed,age_seconds_at_collection:0,restore_piece_handles:["/tmp/backup/piece"],datafile_backup_sets:[11,12]}},verification:{rman_log:{sha256:$digest}}}' >"$TMP/recovery.tmp"
recovery_record=$(jq -cS . "$TMP/recovery.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$recovery_record" '.record_sha256=$record' "$TMP/recovery.tmp" >"$TMP/recovery.json"
srhash=$(sha256sum "$TMP/standalone-reconciliation.json" | awk '{print $1}'); sahash=$(sha256sum "$TMP/standalone-artifact.json" | awk '{print $1}'); sphash=$(sha256sum "$TMP/standalone-procedure.json" | awk '{print $1}'); schash=$(sha256sum "$TMP/standalone-compatibility.json" | awk '{print $1}'); syhash=$(sha256sum "$TMP/standalone-policy.json" | awk '{print $1}')
jq -n --arg r "$srhash" --arg a "$sahash" --arg p "$sphash" --arg c "$schash" --arg y "$syhash" \
  --arg evaluated "$collected" --arg valid "$end" --arg snapshot "$TMP/standalone-snapshot.json" --arg snapshot_sha "$standalone_snapshot_sha" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"12345678",target:{family:"database",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha,host:"node1",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$r,artifact_manifest_sha256:$a,procedure_validation_sha256:$p,compatibility_sha256:$c,policy_sha256:$y}}' >"$TMP/standalone-readiness.json"
run create --plan-id standalone-001 --requester patch-admin --readiness "$TMP/standalone-readiness.json" --reconciliation "$TMP/standalone-reconciliation.json" --artifact-manifest "$TMP/standalone-artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/standalone-policy.json" --recovery-evidence "$TMP/recovery.json" --window-start "$start" --window-end "$end" >"$TMP/standalone-plan.json"
jq -e '.target == {family:"database",method:"opatch",platform_id:"226",database_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome",owner:"oracle",platform_name:"Linux x86-64"} and .artifact.platforms[0].id == "226" and .procedure.platform_id == "226" and .recovery.manifest_path and .recovery.manifest_sha256 and (.snapshot_evidence | length == 1)' "$TMP/standalone-plan.json" >/dev/null
run approve --plan-id standalone-001 --actor dba-approver --approval-ticket CHG-STANDALONE
run authorize --plan-id standalone-001 --actor patch-operator
run dispatch --plan-id standalone-001 --actor patch-operator
standalone_task=$(run next --plan-id standalone-001)
standalone_task_id=$(jq -r '.task_id' <<<"$standalone_task")
standalone_plan_sha=$(jq -r '.plan_sha256' "$TMP/standalone-plan.json")
run claim --plan-id standalone-001 --task-id "$standalone_task_id" --actor standalone-worker --lease-seconds 60 >/dev/null
printf 'stdout\n' >"$TMP/execution-stdout.log"; printf 'stderr\n' >"$TMP/execution-stderr.log"
execution_stdout_sha=$(sha256sum "$TMP/execution-stdout.log" | awk '{print $1}'); execution_stderr_sha=$(sha256sum "$TMP/execution-stderr.log" | awk '{print $1}')
standalone_recovery_sha=$(jq -r '.recovery.manifest_sha256' "$TMP/standalone-plan.json"); standalone_recovery_record=$(jq -r '.recovery.record_sha256' "$TMP/standalone-plan.json")
jq -cn --arg task "$standalone_task_id" --arg plan_sha "$standalone_plan_sha" --arg stdout "$TMP/execution-stdout.log" --arg stderr "$TMP/execution-stderr.log" --arg stdout_sha "$execution_stdout_sha" --arg stderr_sha "$execution_stderr_sha" --arg recovery_sha "$standalone_recovery_sha" --arg recovery_record "$standalone_recovery_record" --arg time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{schema_version:"1.0",collector:{name:"oracle.database.single_instance.executor",version:"1"},intent:"patch_apply",plan_id:"standalone-001",task_id:$task,plan_sha256:$plan_sha,status:"succeeded",postcondition:{status:"passed"},outcome_class:"no_mutation",started_at:$time,finished_at:$time,exit_code:0,actor:"standalone-worker",stage:"precheck",target:{database_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome",owner:"oracle",oracle_sid:"ORCL"},patch:{patch_id:"12345678",artifact_sha256:"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},recovery:{manifest_sha256:$recovery_sha,record_sha256:$recovery_record},logs:{stdout:{path:$stdout,sha256:$stdout_sha},stderr:{path:$stderr,sha256:$stderr_sha}}}' >"$TMP/execution.tmp"
execution_record=$(jq -cS . "$TMP/execution.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$execution_record" '.record_sha256=$record' "$TMP/execution.tmp" >"$TMP/execution.json"
run complete --plan-id standalone-001 --task-id "$standalone_task_id" --actor standalone-worker --status succeeded --evidence "$TMP/execution.json"
standalone_next=$(run next --plan-id standalone-001 | jq -r '.task_id')
jq '.node="tampered"' "$TMP/state/plans/standalone-001/tasks/$standalone_next.json" >"$TMP/task-tampered.json" && mv "$TMP/task-tampered.json" "$TMP/state/plans/standalone-001/tasks/$standalone_next.json"
if run next --plan-id standalone-001 >/dev/null 2>&1; then echo 'tampered sealed task was accepted' >&2; exit 1; fi

run create --plan-id standalone-late --requester patch-admin --readiness "$TMP/standalone-readiness.json" --reconciliation "$TMP/standalone-reconciliation.json" --artifact-manifest "$TMP/standalone-artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/standalone-policy.json" --recovery-evidence "$TMP/recovery.json" --window-start "$start" --window-end "$end" >"$TMP/standalone-late-plan.json"
run approve --plan-id standalone-late --actor dba-approver --approval-ticket CHG-STANDALONE-LATE
run authorize --plan-id standalone-late --actor patch-operator
run dispatch --plan-id standalone-late --actor patch-operator
late_task=$(run next --plan-id standalone-late | jq -r '.task_id'); late_plan_sha=$(jq -r '.plan_sha256' "$TMP/standalone-late-plan.json")
run claim --plan-id standalone-late --task-id "$late_task" --actor late-worker --lease-seconds 60 >/dev/null
late_task_file="$TMP/state/plans/standalone-late/tasks/$late_task.json"; expired_at=$(($(date -u +%s)-1))
jq --argjson expired "$expired_at" '.lease_expires_epoch=$expired' "$late_task_file" >"$late_task_file.tmp" && mv "$late_task_file.tmp" "$late_task_file"
printf 'late stdout\n' >"$TMP/late-stdout.log"; printf 'late stderr\n' >"$TMP/late-stderr.log"
late_stdout_sha=$(sha256sum "$TMP/late-stdout.log" | awk '{print $1}'); late_stderr_sha=$(sha256sum "$TMP/late-stderr.log" | awk '{print $1}')
late_recovery_sha=$(jq -r '.recovery.manifest_sha256' "$TMP/standalone-late-plan.json"); late_recovery_record=$(jq -r '.recovery.record_sha256' "$TMP/standalone-late-plan.json")
jq -cn --arg task "$late_task" --arg plan_sha "$late_plan_sha" --arg stdout "$TMP/late-stdout.log" --arg stderr "$TMP/late-stderr.log" --arg stdout_sha "$late_stdout_sha" --arg stderr_sha "$late_stderr_sha" --arg recovery_sha "$late_recovery_sha" --arg recovery_record "$late_recovery_record" --arg time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{schema_version:"1.0",collector:{name:"oracle.database.single_instance.executor",version:"1"},intent:"patch_apply",plan_id:"standalone-late",task_id:$task,plan_sha256:$plan_sha,status:"succeeded",postcondition:{status:"passed"},outcome_class:"no_mutation",started_at:$time,finished_at:$time,exit_code:0,actor:"late-worker",stage:"precheck",target:{database_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome",owner:"oracle",oracle_sid:"ORCL"},patch:{patch_id:"12345678",artifact_sha256:"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},recovery:{manifest_sha256:$recovery_sha,record_sha256:$recovery_record},logs:{stdout:{path:$stdout,sha256:$stdout_sha},stderr:{path:$stderr,sha256:$stderr_sha}}}' >"$TMP/late-execution.tmp"
late_record=$(jq -cS . "$TMP/late-execution.tmp" | tr -d '\n' | sha256sum | awk '{print $1}'); jq --arg record "$late_record" '.record_sha256=$record' "$TMP/late-execution.tmp" >"$TMP/late-execution.json"
run complete --plan-id standalone-late --task-id "$late_task" --actor late-worker --status succeeded --evidence "$TMP/late-execution.json"
run status --plan-id standalone-late | jq -e '.state == "paused"' >/dev/null
jq -e '.status == "succeeded" and .completed_after_lease == true' "$late_task_file" >/dev/null

# retry-task: only the exact task that paused the plan may be resumed, and
# only while the plan is paused for that reason. A successful retry resets
# the task to pending and reopens the plan for the normal next/claim flow.
if run retry-task --plan-id plan-001 --task-id 001-grid-precheck-node1 --actor retry-operator >/dev/null 2>&1; then
  echo 'retry-task was accepted on a plan that is not paused' >&2
  exit 1
fi
run create --plan-id standalone-retry --requester patch-admin --readiness "$TMP/standalone-readiness.json" --reconciliation "$TMP/standalone-reconciliation.json" --artifact-manifest "$TMP/standalone-artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/standalone-policy.json" --recovery-evidence "$TMP/recovery.json" --window-start "$start" --window-end "$end" >"$TMP/standalone-retry-plan.json"
run approve --plan-id standalone-retry --actor dba-approver --approval-ticket CHG-STANDALONE-RETRY
run authorize --plan-id standalone-retry --actor patch-operator
run dispatch --plan-id standalone-retry --actor patch-operator
retry_plan_sha=$(jq -r '.plan_sha256' "$TMP/standalone-retry-plan.json")
retry_recovery_sha=$(jq -r '.recovery.manifest_sha256' "$TMP/standalone-retry-plan.json"); retry_recovery_record=$(jq -r '.recovery.record_sha256' "$TMP/standalone-retry-plan.json")
retry_first_task=$(run next --plan-id standalone-retry | jq -r '.task_id')
other_task=$(find "$TMP/state/plans/standalone-retry/tasks" -name '*.json' -exec basename {} \; | sed 's/\.json$//' | sort | grep -v "^${retry_first_task}\$" | head -1)
run claim --plan-id standalone-retry --task-id "$retry_first_task" --actor retry-worker --lease-seconds 60 >/dev/null
printf 'fail stdout\n' >"$TMP/retry-fail-stdout.log"; printf 'fail stderr\n' >"$TMP/retry-fail-stderr.log"
retry_fail_stdout_sha=$(sha256sum "$TMP/retry-fail-stdout.log" | awk '{print $1}'); retry_fail_stderr_sha=$(sha256sum "$TMP/retry-fail-stderr.log" | awk '{print $1}')
jq -cn --arg task "$retry_first_task" --arg plan_sha "$retry_plan_sha" --arg stdout "$TMP/retry-fail-stdout.log" --arg stderr "$TMP/retry-fail-stderr.log" --arg stdout_sha "$retry_fail_stdout_sha" --arg stderr_sha "$retry_fail_stderr_sha" --arg recovery_sha "$retry_recovery_sha" --arg recovery_record "$retry_recovery_record" --arg time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{schema_version:"1.0",collector:{name:"oracle.database.single_instance.executor",version:"1"},intent:"patch_apply",plan_id:"standalone-retry",task_id:$task,plan_sha256:$plan_sha,status:"failed",postcondition:{status:"failed",detail:"simulated datapatch failure"},outcome_class:"binary_state_known",started_at:$time,finished_at:$time,exit_code:1,actor:"retry-worker",stage:"precheck",target:{database_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome",owner:"oracle",oracle_sid:"ORCL"},patch:{patch_id:"12345678",artifact_sha256:"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},recovery:{manifest_sha256:$recovery_sha,record_sha256:$recovery_record},logs:{stdout:{path:$stdout,sha256:$stdout_sha},stderr:{path:$stderr,sha256:$stderr_sha}}}' >"$TMP/retry-fail-execution.tmp"
retry_fail_record=$(jq -cS . "$TMP/retry-fail-execution.tmp" | tr -d '\n' | sha256sum | awk '{print $1}'); jq --arg record "$retry_fail_record" '.record_sha256=$record' "$TMP/retry-fail-execution.tmp" >"$TMP/retry-fail-execution.json"
run complete --plan-id standalone-retry --task-id "$retry_first_task" --actor retry-worker --status failed --evidence "$TMP/retry-fail-execution.json"
run status --plan-id standalone-retry | jq -e '.state == "paused"' >/dev/null
jq -e '.status == "failed"' "$TMP/state/plans/standalone-retry/tasks/$retry_first_task.json" >/dev/null
if run retry-task --plan-id standalone-retry --task-id "$other_task" --actor retry-operator >/dev/null 2>&1; then
  echo 'retry-task accepted a task other than the one that failed' >&2
  exit 1
fi
run status --plan-id standalone-retry | jq -e '.state == "paused"' >/dev/null
retried=$(run retry-task --plan-id standalone-retry --task-id "$retry_first_task" --actor retry-operator)
jq -e '.status == "pending" and (.claimed_by | not) and (.evidence_sha256 | not) and (.task_result_sha256 | not)' <<<"$retried" >/dev/null
run status --plan-id standalone-retry | jq -e '.state == "running"' >/dev/null
run next --plan-id standalone-retry | jq -e --arg task "$retry_first_task" '.task_id == $task' >/dev/null
run claim --plan-id standalone-retry --task-id "$retry_first_task" --actor retry-worker-02 --lease-seconds 60 >/dev/null
jq -cn --arg task "$retry_first_task" --arg plan_sha "$retry_plan_sha" --arg stdout "$TMP/retry-fail-stdout.log" --arg stderr "$TMP/retry-fail-stderr.log" --arg stdout_sha "$retry_fail_stdout_sha" --arg stderr_sha "$retry_fail_stderr_sha" --arg recovery_sha "$retry_recovery_sha" --arg recovery_record "$retry_recovery_record" --arg time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{schema_version:"1.0",collector:{name:"oracle.database.single_instance.executor",version:"1"},intent:"patch_apply",plan_id:"standalone-retry",task_id:$task,plan_sha256:$plan_sha,status:"succeeded",postcondition:{status:"passed"},outcome_class:"no_mutation",started_at:$time,finished_at:$time,exit_code:0,actor:"retry-worker-02",stage:"precheck",target:{database_unique_name:"ORCL",oracle_home:"/opt/oracle/dbhome",owner:"oracle",oracle_sid:"ORCL"},patch:{patch_id:"12345678",artifact_sha256:"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},recovery:{manifest_sha256:$recovery_sha,record_sha256:$recovery_record},logs:{stdout:{path:$stdout,sha256:$stdout_sha},stderr:{path:$stderr,sha256:$stderr_sha}}}' >"$TMP/retry-succeed-execution.tmp"
retry_succeed_record=$(jq -cS . "$TMP/retry-succeed-execution.tmp" | tr -d '\n' | sha256sum | awk '{print $1}'); jq --arg record "$retry_succeed_record" '.record_sha256=$record' "$TMP/retry-succeed-execution.tmp" >"$TMP/retry-succeed-execution.json"
run complete --plan-id standalone-retry --task-id "$retry_first_task" --actor retry-worker-02 --status succeeded --evidence "$TMP/retry-succeed-execution.json"
run status --plan-id standalone-retry | jq -e '.state == "running"' >/dev/null
run next --plan-id standalone-retry | jq -e --arg task "$retry_first_task" '.task_id != $task' >/dev/null
if run retry-task --plan-id standalone-retry --task-id "$retry_first_task" --actor retry-operator >/dev/null 2>&1; then
  echo 'retry-task was accepted on a running plan for an already-succeeded task' >&2
  exit 1
fi

# Production mode refuses authorize/dispatch without a certification marker.
run create --plan-id plan-prod-gate --requester patch-admin --readiness "$TMP/standalone-readiness.json" --reconciliation "$TMP/standalone-reconciliation.json" --artifact-manifest "$TMP/standalone-artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/standalone-policy.json" --recovery-evidence "$TMP/recovery.json" --window-start "$start" --window-end "$end" >/dev/null
run approve --plan-id plan-prod-gate --actor dba-approver --approval-ticket CHG-PROD-GATE >/dev/null
if OPU_PRODUCTION_MODE=1 run authorize --plan-id plan-prod-gate --actor patch-operator >/dev/null 2>&1; then
  echo 'production mode authorized a plan without a certification marker' >&2
  exit 1
fi
printf 'OPU_PRODUCTION_CERTIFIED=1\n' >"$TMP/production.cert"
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/production.cert" run authorize --plan-id plan-prod-gate --actor patch-operator >/dev/null
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/production.cert" run dispatch --plan-id plan-prod-gate --actor patch-operator >/dev/null

jq '.patch_id = "tampered"' "$TMP/state/plans/plan-001/plan.json" >"$TMP/tampered.json" && mv "$TMP/tampered.json" "$TMP/state/plans/plan-001/plan.json"
if run status --plan-id plan-001 >/dev/null 2>&1; then echo 'tampered immutable plan was accepted' >&2; exit 1; fi
printf '%s\n' 'patch plan control test passed'
