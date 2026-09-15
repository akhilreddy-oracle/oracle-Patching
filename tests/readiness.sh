#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-readiness.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
for node in node1 node2; do
  jq -n --arg node "$node" --arg now "$now" --arg digest "$digest" '{schema_version:"1.0",collector:{name:"oracle.topology.discover"},collected_at:$now,host:{name:($node+".example")},cluster:{grid_home:"/u01/grid",runtime:{status:"healthy",upgrade_state:"NORMAL"},nodes:[{name:"node1",status:"Active"},{name:"node2",status:"Active"}]},oracle_homes:[{path:"/u01/grid",owner:"grid",version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest},patch_inventory_source:"opatch_lsinventory_xml",opatch_inventory_xml_sha256:$digest,patches:[]}],databases:[],warnings:[]}' >"$TMP/$node.json"
done
node1_sha=$(sha256sum "$TMP/node1.json" | awk '{print $1}')
node2_sha=$(sha256sum "$TMP/node2.json" | awk '{print $1}')
jq -n --arg node1 "$TMP/node1.json" --arg node1_sha "$node1_sha" --arg node2 "$TMP/node2.json" --arg node2_sha "$node2_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["node1","node2"],snapshot_evidence:[{path:$node1,sha256:$node1_sha},{path:$node2,sha256:$node2_sha}]}' >"$TMP/reconciliation.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",artifact:{status:"ready_for_catalog",sha256:$digest,patch_ids:["12345678"],platforms:[{id:"226",name:"Linux x86-64",source:"etc/config/inventory.xml",source_sha256:$digest}]}}' >"$TMP/artifact.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$digest,target:{family:"grid",method:"opatch",platform_id:"226"}}}' >"$TMP/procedure.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"grid",platform_id:"226"},checks:["node1","node2"] | map({node:.,home:"/u01/grid",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,evidence_path:("/tmp/"+.+"-platform.log"),evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,evidence_path:("/tmp/"+.+"-conflict.log"),evidence_sha256:$digest}})}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:false,max_backup_age_minutes:0,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"
"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/reconciliation.json" --snapshot "$TMP/node1.json" --snapshot "$TMP/node2.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" --output "$TMP/result.json" >/dev/null
jq -e '.status == "ready_for_approval" and (.gates | length == 1)' "$TMP/result.json" >/dev/null
jq '.checks[1].conflict_check.status = "failed"' "$TMP/compatibility.json" >"$TMP/bad-compatibility.json"
if "$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/reconciliation.json" --snapshot "$TMP/node1.json" --snapshot "$TMP/node2.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/bad-compatibility.json" --policy "$TMP/policy.json" >/dev/null 2>&1; then echo 'failed compatibility evidence was accepted' >&2; exit 1; fi
if "$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/reconciliation.json" --snapshot "$TMP/node1.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" >/dev/null 2>&1; then echo 'missing active-node snapshot was accepted' >&2; exit 1; fi
jq -n --arg now "$now" --arg digest "$digest" '{schema_version:"1.0",collector:{name:"oracle.topology.discover"},collected_at:$now,host:{name:"standalone.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},oracle_homes:[{path:"/u01/db",owner:"oracle",version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest},patch_inventory_source:"opatch_lsinventory_xml",opatch_inventory_xml_sha256:$digest,patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:"/u01/db",runtime:{status:"complete",database_role:"PRIMARY",open_mode:"READ WRITE",instance_state:"OPEN",invalid_objects:0,sqlpatch_non_success:0,pdb_not_read_write:0,backup_age_minutes:1,fra_space_limit_bytes:0,fra_space_used_bytes:0,guaranteed_restore_points:0}}],warnings:[]}' >"$TMP/standalone.json"
standalone_sha=$(sha256sum "$TMP/standalone.json" | awk '{print $1}')
jq -n --arg snapshot "$TMP/standalone.json" --arg sha "$standalone_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["standalone"],snapshot_evidence:[{path:$snapshot,sha256:$sha}]}' >"$TMP/standalone-reconciliation.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"}}}' >"$TMP/standalone-procedure.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"standalone",home:"/u01/db",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,evidence_path:"/tmp/standalone-platform.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,evidence_path:"/tmp/standalone-conflict.log",evidence_sha256:$digest}}]}' >"$TMP/standalone-compatibility.json"
"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-reconciliation.json" --snapshot "$TMP/standalone.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/policy.json" --output "$TMP/standalone-result.json" >/dev/null
jq -e '.status == "ready_for_approval" and .target.platform_id == "226"' "$TMP/standalone-result.json" >/dev/null

# Absent, null, negative and fractional invalid-object counts are unknown, never zero.
for invalid_expr in 'del(.databases[0].runtime.invalid_objects)' '.databases[0].runtime.invalid_objects = null' '.databases[0].runtime.invalid_objects = -1' '.databases[0].runtime.invalid_objects = 0.5' '.databases[0].runtime.invalid_objects = "0"'; do
  jq "$invalid_expr" "$TMP/standalone.json" >"$TMP/invalid-count.json"
  count_sha=$(sha256sum "$TMP/invalid-count.json" | awk '{print $1}')
  jq -n --arg snapshot "$TMP/invalid-count.json" --arg sha "$count_sha" '{schema_version:"1.0",status:"consistent",expected_nodes:["standalone"],snapshot_evidence:[{path:$snapshot,sha256:$sha}]}' >"$TMP/count-reconciliation.json"
  if "$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/count-reconciliation.json" --snapshot "$TMP/invalid-count.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/policy.json" --output "$TMP/count-result.json" >/dev/null; then
    echo 'unknown or invalid invalid-object count was accepted' >&2; exit 1
  fi
  jq -e '.status == "blocked" and any(.gates[]; .name == "database_invalid_objects" and .status == "blocker")' "$TMP/count-result.json" >/dev/null
done

# Missing FRA used bytes must fail closed (never inflate free = limit - (-1)).
jq '.databases[0].runtime |= del(.fra_space_used_bytes)' "$TMP/standalone.json" >"$TMP/standalone-missing-fra-used.json"
missing_fra_sha=$(sha256sum "$TMP/standalone-missing-fra-used.json" | awk '{print $1}')
jq -n --arg snapshot "$TMP/standalone-missing-fra-used.json" --arg sha "$missing_fra_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["standalone"],snapshot_evidence:[{path:$snapshot,sha256:$sha}]}' >"$TMP/standalone-missing-fra-reconciliation.json"
# FRA fail-closed applies only when backup is required. Waiver (require_backup=false) skips recovery gates.
jq '.recovery.require_backup = true | .recovery.minimum_fra_free_bytes = 1' "$TMP/policy.json" >"$TMP/fra-policy.json"
set +e
"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-missing-fra-reconciliation.json" --snapshot "$TMP/standalone-missing-fra-used.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/fra-policy.json" --output "$TMP/standalone-missing-fra-result.json" >/dev/null
missing_fra_rc=$?
set -e
[ "$missing_fra_rc" -eq 2 ]
jq -e '.status == "blocked" and any(.gates[]; .name == "recovery_fra" and .status == "blocker")' "$TMP/standalone-missing-fra-result.json" >/dev/null

# Data Guard standby roles must fail closed until S11 exists.
jq '.databases[0].runtime.database_role = "PHYSICAL STANDBY"' "$TMP/standalone.json" >"$TMP/standalone-standby.json"
standby_sha=$(sha256sum "$TMP/standalone-standby.json" | awk '{print $1}')
jq -n --arg snapshot "$TMP/standalone-standby.json" --arg sha "$standby_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["standalone"],snapshot_evidence:[{path:$snapshot,sha256:$sha}]}' >"$TMP/standalone-standby-reconciliation.json"
set +e
"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-standby-reconciliation.json" --snapshot "$TMP/standalone-standby.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/policy.json" --output "$TMP/standalone-standby-result.json" >/dev/null
standby_rc=$?
set -e
[ "$standby_rc" -eq 2 ]
jq -e '.status == "blocked" and any(.gates[]; .name == "dataguard_unsupported" and .status == "blocker")' "$TMP/standalone-standby-result.json" >/dev/null

# Stale reconciliation digests must fail closed with an actionable binding detail.
jq '.oracle_homes[0].patches += ["99999999"]' "$TMP/standalone.json" >"$TMP/standalone-drift.json"
set +e
"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-reconciliation.json" --snapshot "$TMP/standalone-drift.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/policy.json" --output "$TMP/standalone-drift-result.json" >/dev/null
drift_rc=$?
set -e
[ "$drift_rc" -eq 2 ]
jq -e '
  .status == "blocked" and
  any(.gates[];
    .name == "snapshot_binding" and .status == "blocker" and
    (.detail | test("supplied=\\[") and test("reconciled=\\[") and test("Re-run Discover then Reconcile"))
  )
' "$TMP/standalone-drift-result.json" >/dev/null


# Filesystem recovery substitutes a fully validated selected set for the FRA
# gate, never a backup waiver. The same verifier is used again by patch plans.
jq '.recovery |= (. + {require_backup:true,storage_mode:"filesystem",capacity_basis:"rman_unused_blocks",minimum_filesystem_free_bytes:100,max_backup_age_minutes:1440})' "$TMP/policy.json" >"$TMP/filesystem-policy.json"
filesystem_eval() {
  "$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-reconciliation.json" --snapshot "$TMP/standalone.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/filesystem-policy.json" "$@"
}
if filesystem_eval --output "$TMP/no-recovery.json" >/dev/null; then echo 'filesystem backup without selected evidence was accepted' >&2; exit 1; fi
jq -e 'any(.gates[]; .name == "recovery_filesystem" and .status == "blocker") and all(.gates[]; .name != "recovery_fra")' "$TMP/no-recovery.json" >/dev/null
jq -n --arg snapshot "$TMP/standalone.json" --arg snapshot_sha "$standalone_sha" --arg now "$now" --arg digest "$digest" '
 {schema_version:"1.0",collector:{name:"oracle.recovery.evidence",version:"1"},status:"passed",target:{database_unique_name:"ORCL",oracle_home:"/u01/db",owner:"oracle"},source_snapshot:{path:$snapshot,sha256:$snapshot_sha},backup:{root:"/backup/selected",checksum_manifest:{sha256:$digest},preparation:{manifest:{sha256:$digest,record_sha256:$digest},central_inventory_archive:{sha256:$digest},oraInst_loc:{sha256:$digest}},selected_recovery_set:{observed_at:$now,oldest_datafile_backup_completed_at:$now,age_seconds_at_collection:0,restore_piece_handles:["/backup/selected/db.bkp"],datafile_backup_sets:[1]},coverage:{datafiles_current:2,base_datafiles:2,controlfile_records:1,spfile_records:1,outside_root_pieces:0,unavailable_pieces:0},storage:{type:"filesystem",path:"/backup/selected",device_id:"1",total_bytes:1000,available_bytes:500,observed_at:$now}},verification:{checksum_log:{sha256:$digest},oracle_home_archive_log:{sha256:$digest},rman_syntax:{log:{sha256:$digest},exit_code:{value:0,sha256:$digest}},rman_log:{sha256:$digest,exit_code:{value:0,sha256:$digest}}}}' >"$TMP/filesystem-recovery-base.json"
seal_recovery() {
  local input=$1 output=$2 canonical sha
  canonical=$(jq -cS 'del(.record_sha256)' "$input")
  sha=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
  jq --arg sha "$sha" '.record_sha256=$sha' "$input" >"$output"
}
seal_recovery "$TMP/filesystem-recovery-base.json" "$TMP/filesystem-recovery.json"
filesystem_eval --recovery-evidence "$TMP/filesystem-recovery.json" --output "$TMP/filesystem-result.json" >/dev/null
jq -e '.status == "ready_for_approval" and any(.gates[]; .name == "recovery_filesystem" and .status == "pass") and (.evidence.recovery_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/filesystem-result.json" >/dev/null
for mutation in '.backup.storage.available_bytes=99' '.backup.storage.available_bytes=-1' '.backup.storage.observed_at="2000-01-01T00:00:00Z"' '.target.database_unique_name="OTHER"' '.source_snapshot.sha256="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"' 'del(.backup.preparation)' '.verification.rman_log.exit_code.value=1' '.backup.coverage.base_datafiles=1'; do
  jq "$mutation" "$TMP/filesystem-recovery-base.json" >"$TMP/recovery-mutated.json"
  seal_recovery "$TMP/recovery-mutated.json" "$TMP/recovery-mutated-sealed.json"
  if filesystem_eval --recovery-evidence "$TMP/recovery-mutated-sealed.json" >/dev/null 2>&1; then echo "unsafe filesystem evidence accepted: $mutation" >&2; exit 1; fi
done
jq '.backup.storage.available_bytes=501' "$TMP/filesystem-recovery.json" >"$TMP/recovery-tampered.json"
if filesystem_eval --recovery-evidence "$TMP/recovery-tampered.json" >/dev/null 2>&1; then echo 'tampered filesystem evidence accepted' >&2; exit 1; fi
for mutation in '.recovery.storage_mode=null' '.recovery.storage_mode="typo"' '.recovery.capacity_basis="ratio"' '.recovery.minimum_filesystem_free_bytes=-1' '.recovery.minimum_filesystem_free_bytes=null' '.recovery.require_backup=false'; do
  jq "$mutation" "$TMP/filesystem-policy.json" >"$TMP/bad-filesystem-policy.json"
  if "$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-reconciliation.json" --snapshot "$TMP/standalone.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/bad-filesystem-policy.json" >/dev/null 2>&1; then echo "invalid filesystem policy accepted: $mutation" >&2; exit 1; fi
done

printf '%s\n' 'readiness evaluation test passed'
