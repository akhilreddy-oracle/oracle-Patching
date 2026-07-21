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

# Missing FRA used bytes must fail closed (never inflate free = limit - (-1)).
jq '.databases[0].runtime |= del(.fra_space_used_bytes)' "$TMP/standalone.json" >"$TMP/standalone-missing-fra-used.json"
missing_fra_sha=$(sha256sum "$TMP/standalone-missing-fra-used.json" | awk '{print $1}')
jq -n --arg snapshot "$TMP/standalone-missing-fra-used.json" --arg sha "$missing_fra_sha" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["standalone"],snapshot_evidence:[{path:$snapshot,sha256:$sha}]}' >"$TMP/standalone-missing-fra-reconciliation.json"
jq '.recovery.minimum_fra_free_bytes = 1' "$TMP/policy.json" >"$TMP/fra-policy.json"
set +e
"$ROOT/bin/opu-readiness-evaluate" --reconciliation "$TMP/standalone-missing-fra-reconciliation.json" --snapshot "$TMP/standalone-missing-fra-used.json" --artifact "$TMP/artifact.json" --procedure-validation "$TMP/standalone-procedure.json" --compatibility "$TMP/standalone-compatibility.json" --policy "$TMP/fra-policy.json" --output "$TMP/standalone-missing-fra-result.json" >/dev/null
missing_fra_rc=$?
set -e
[ "$missing_fra_rc" -eq 2 ]
jq -e '.status == "blocked" and any(.gates[]; .name == "recovery_fra" and .status == "blocker")' "$TMP/standalone-missing-fra-result.json" >/dev/null

printf '%s\n' 'readiness evaluation test passed'
