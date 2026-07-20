#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-compatibility.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
me=$(id -un); digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
mkdir -p "$TMP/grid/OPatch" "$TMP/artifact/etc/config"
cp "$ROOT/tests/fixtures/opatch-compatibility/actions.xml" "$TMP/artifact/etc/config/actions.xml"
printf '<patch patchID="12345678"><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n' >"$TMP/artifact/etc/config/inventory.xml"
cp "$ROOT/tests/fixtures/opatch-compatibility/opatch" "$TMP/grid/OPatch/opatch"
chmod 700 "$TMP/grid/OPatch/opatch"
"$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/artifact" --output "$TMP/artifact.json" >/dev/null
jq -n --arg home "$TMP/grid" --arg me "$me" --arg digest "$digest" '{schema_version:"1.0",collector:{name:"oracle.topology.discover"},host:{name:"node1.example"},cluster:{grid_home:$home},oracle_homes:[{path:$home,owner:$me,platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest}}],databases:[]}' >"$TMP/snapshot.json"
sha=$(jq -r '.artifact.sha256' "$TMP/artifact.json")
jq -n --arg sha "$sha" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$sha,required_opatch_version:"12.2.0.1",target:{family:"grid",method:"opatch",platform_id:"226"},execution:{adapter:"grid_rolling_opatch",operations:["grid_rootcrs_prepatch","grid_opatch_apply","grid_rootadd_rdbms","grid_rootcrs_postpatch"]}}}' >"$TMP/procedure.json"
"$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/snapshot.json" --artifact "$TMP/artifact" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --evidence-dir "$TMP/evidence" --output "$TMP/result.json" >/dev/null
jq -e '.status == "passed" and .target.platform_id == "226" and .checks[0].platform.status == "passed" and .checks[0].applicability_check.name == "CheckPatchApplicableOnCurrentPlatform" and .checks[0].applicability_check.status == "passed" and .checks[0].applicability_check.exit_code == 0 and (.checks[0].applicability_check.evidence_sha256 | test("^[a-f0-9]{64}$")) and .checks[0].conflict_check.name == "CheckConflictAgainstOHWithDetail" and .checks[0].conflict_check.status == "passed" and .checks[0].conflict_check.exit_code == 0 and (.checks[0].conflict_check.evidence_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/result.json" >/dev/null
mkdir -p "$TMP/database/OPatch"
cp "$ROOT/tests/fixtures/opatch-compatibility/opatch" "$TMP/database/OPatch/opatch"
chmod 700 "$TMP/database/OPatch/opatch"
mkdir -p "$TMP/staged/12345678"
cp "$ROOT/tests/fixtures/opatch-compatibility/actions.xml" "$TMP/staged/12345678/actions.xml"
mkdir -p "$TMP/staged/12345678/etc/config"
cp "$ROOT/tests/fixtures/opatch-compatibility/actions.xml" "$TMP/staged/12345678/etc/config/actions.xml"
printf '<patch patchID="12345678"><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n' >"$TMP/staged/12345678/etc/config/inventory.xml"
"$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/staged/12345678" --output "$TMP/database-artifact.json" >/dev/null
jq -n --arg home "$TMP/database" --arg me "$me" --arg digest "$digest" '{schema_version:"1.0",collector:{name:"oracle.topology.discover"},host:{name:"standalone.example"},cluster:{grid_home:null},oracle_homes:[{path:$home,owner:$me,platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest}}],databases:[{db_unique_name:"ORCL",oracle_home:$home}]}' >"$TMP/database-snapshot.json"
database_sha=$(jq -r '.artifact.sha256' "$TMP/database-artifact.json")
jq -n --arg sha "$database_sha" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$sha,required_opatch_version:"12.2.0.1",target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]}}}' >"$TMP/database-procedure.json"
"$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/database-snapshot.json" --artifact "$TMP/staged/12345678" --artifact-manifest "$TMP/database-artifact.json" --procedure-validation "$TMP/database-procedure.json" --evidence-dir "$TMP/database-evidence" --output "$TMP/database-result.json" >/dev/null
jq -e '.status == "passed" and .checks[0].home == $home and .checks[0].conflict_check.patch_option == "-ph" and .checks[0].conflict_check.patch_source == $source' --arg home "$TMP/database" --arg source "$TMP/staged/12345678" "$TMP/database-result.json" >/dev/null
grep -F -- "CheckPatchApplicableOnCurrentPlatform -ph $TMP/staged/12345678" "$TMP/database-evidence/standalone-database-opatch-platform-12345678.log" >/dev/null
grep -F -- "CheckConflictAgainstOHWithDetail -ph $TMP/staged/12345678" "$TMP/database-evidence/standalone-database-opatch-conflict-12345678.log" >/dev/null
printf '%s\n' 'OPatch compatibility collector test passed'
