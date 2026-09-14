#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-procedure.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
other_digest=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
jq -n --arg digest "$digest" '{schema_version:"1.0",artifact:{status:"ready_for_catalog",sha256:$digest,patch_ids:["12345678"],platforms:[{id:"226",name:"Linux x86-64",source:"etc/config/inventory.xml",source_sha256:$digest}],readme_files:[{path:"README.html",sha256:$digest}]}}' >"$TMP/artifact.json"
jq -n --arg digest "$digest" '{schema_version:"1.0",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",method:"opatch",topology:"rac",database_unique_name:"TESTDB",platform_id:"226"},execution:{adapter:"database_rolling_opatch",operations:["database_stop_instance","database_opatch_apply","database_start_instance","database_datapatch"]},required_opatch_version:"12.2.0.1",oracle_references:[{kind:"patch_readme",identifier:"README",sha256:$digest}],mandatory_prechecks:["artifact_integrity","platform_applicability","opatch_version","conflict_check","backup_or_restore"],mandatory_postchecks:["binary_inventory","service_health"],rollback:{mode:"opatch_rollback",precondition:"Patch README confirms rollback support"}}' >"$TMP/procedure.json"
"$ROOT/bin/opu-procedure-validate" --procedure "$TMP/procedure.json" --artifact "$TMP/artifact.json" --output "$TMP/result.json" >/dev/null
jq -e '.status == "ready_for_planning" and .procedure.patch_id == "12345678"' "$TMP/result.json" >/dev/null
jq -n --arg digest "$digest" '{schema_version:"1.0",patch_id:"12345678",artifact_sha256:$digest,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"TESTDB",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]},required_opatch_version:"12.2.0.1",oracle_references:[{kind:"patch_readme",identifier:"README",sha256:$digest}],mandatory_prechecks:["artifact_integrity","platform_applicability","opatch_version","conflict_check","backup_or_restore"],mandatory_postchecks:["binary_inventory","service_health"],rollback:{mode:"opatch_rollback",precondition:"Patch README confirms rollback support"}}' >"$TMP/standalone.json"
"$ROOT/bin/opu-procedure-validate" --procedure "$TMP/standalone.json" --artifact "$TMP/artifact.json" >/dev/null
jq '.patch_id = "999"' "$TMP/procedure.json" >"$TMP/bad.json"
if "$ROOT/bin/opu-procedure-validate" --procedure "$TMP/bad.json" --artifact "$TMP/artifact.json" >/dev/null 2>&1; then echo 'mismatched procedure was accepted' >&2; exit 1; fi
jq --arg digest "$other_digest" '.oracle_references[0].sha256 = $digest' "$TMP/procedure.json" >"$TMP/wrong-readme.json"
if "$ROOT/bin/opu-procedure-validate" --procedure "$TMP/wrong-readme.json" --artifact "$TMP/artifact.json" >/dev/null 2>&1; then echo 'procedure README digest outside the inspected artifact was accepted' >&2; exit 1; fi
jq '.target.platform_id = "46"' "$TMP/procedure.json" >"$TMP/wrong-platform.json"
if "$ROOT/bin/opu-procedure-validate" --procedure "$TMP/wrong-platform.json" --artifact "$TMP/artifact.json" >/dev/null 2>&1; then echo 'procedure platform differing from artifact inventory was accepted' >&2; exit 1; fi
jq '.artifact.readme_files = []' "$TMP/artifact.json" >"$TMP/no-readme-artifact.json"
if "$ROOT/bin/opu-procedure-validate" --procedure "$TMP/procedure.json" --artifact "$TMP/no-readme-artifact.json" >/dev/null 2>&1; then echo 'artifact without a hashed README was accepted' >&2; exit 1; fi
jq '.required_opatch_version="" | .target.database_unique_name="" | .rollback.precondition="" | .mandatory_prechecks=["artifact_integrity","platform_applicability","opatch_version","conflict_check"]' "$TMP/procedure.json" >"$TMP/incomplete.json"
if "$ROOT/bin/opu-procedure-validate" --procedure "$TMP/incomplete.json" --artifact "$TMP/artifact.json" >"$TMP/incomplete.out" 2>"$TMP/incomplete.err"; then
  echo 'incomplete procedure was accepted' >&2
  exit 1
fi
grep -q 'required_opatch_version must be a dotted OPatch version' "$TMP/incomplete.err" || { echo 'missing required_opatch_version diagnostic' >&2; cat "$TMP/incomplete.err" >&2; exit 1; }
grep -q 'database_unique_name is required' "$TMP/incomplete.err" || { echo 'missing database_unique_name diagnostic' >&2; cat "$TMP/incomplete.err" >&2; exit 1; }
grep -q 'backup_or_restore' "$TMP/incomplete.err" || { echo 'missing backup_or_restore diagnostic' >&2; cat "$TMP/incomplete.err" >&2; exit 1; }
grep -q 'rollback.precondition must be a non-empty' "$TMP/incomplete.err" || { echo 'missing rollback.precondition diagnostic' >&2; cat "$TMP/incomplete.err" >&2; exit 1; }
printf '%s\n' 'procedure validation test passed'
