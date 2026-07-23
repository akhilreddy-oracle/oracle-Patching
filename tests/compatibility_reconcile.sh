#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-compatibility-reconcile.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
jq -n '{schema_version:"1.0",status:"consistent",expected_nodes:["node1","node2"],cluster:{grid_home:"/u01/grid"},oracle_homes:[],databases:[]}' >"$TMP/reconciliation.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$digest,target:{family:"grid",method:"opatch",platform_id:"226"},execution:{adapter:"grid_rolling_opatch",operations:["grid_rootcrs_prepatch","grid_opatch_apply","grid_rootadd_rdbms","grid_rootcrs_postpatch"]}}}' >"$TMP/procedure.json"
for node in node1 node2; do jq -n --arg node "$node" --arg digest "$digest" '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"grid",platform_id:"226"},checks:[{node:$node,home:"/u01/grid",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,evidence_path:("/tmp/"+$node+"-platform.log"),evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,evidence_path:("/tmp/"+$node+"-conflict.log"),evidence_sha256:$digest}}]}' >"$TMP/$node.json"; done
"$ROOT/bin/opu-compatibility-reconcile" --reconciliation "$TMP/reconciliation.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/node1.json" --compatibility "$TMP/node2.json" --output "$TMP/result.json" >/dev/null
jq -e '.status == "passed" and .target.platform_id == "226" and (.checks | length == 2)' "$TMP/result.json" >/dev/null
if "$ROOT/bin/opu-compatibility-reconcile" --reconciliation "$TMP/reconciliation.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/node1.json" >/dev/null 2>&1; then echo 'missing node compatibility was accepted' >&2; exit 1; fi
jq 'del(.checks[0].applicability_check)' "$TMP/node2.json" >"$TMP/node2-no-platform-check.json"
if "$ROOT/bin/opu-compatibility-reconcile" --reconciliation "$TMP/reconciliation.json" --procedure-validation "$TMP/procedure.json" --compatibility "$TMP/node1.json" --compatibility "$TMP/node2-no-platform-check.json" >/dev/null 2>&1; then echo 'missing platform applicability evidence was accepted' >&2; exit 1; fi

# Standalone databases have no Grid Infrastructure home: cluster.grid_home is
# legitimately null. The database-family path must not require it.
jq -n '{schema_version:"1.0",status:"consistent",expected_nodes:["standalone1"],cluster:{grid_home:null},oracle_homes:[],databases:[{db_unique_name:"ORCL",oracle_home:"/u01/app/oracle/product/19c/dbhome_1"}]}' >"$TMP/db-reconciliation.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",method:"opatch",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]}}}' >"$TMP/db-procedure.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",status:"passed",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"standalone1",home:"/u01/app/oracle/product/19c/dbhome_1",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,evidence_path:"/tmp/standalone1-platform.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,evidence_path:"/tmp/standalone1-conflict.log",evidence_sha256:$digest}}]}' >"$TMP/db-node1.json"
"$ROOT/bin/opu-compatibility-reconcile" --reconciliation "$TMP/db-reconciliation.json" --procedure-validation "$TMP/db-procedure.json" --compatibility "$TMP/db-node1.json" --output "$TMP/db-result.json" >/dev/null
jq -e '.status == "passed" and .target.platform_id == "226" and (.checks | length == 1)' "$TMP/db-result.json" >/dev/null

printf '%s\n' 'compatibility reconciliation test passed'
