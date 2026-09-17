#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-compatibility.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
me=$(id -un); digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
mkdir -p "$TMP/grid/OPatch" "$TMP/artifact/etc/config" "$TMP/artifact/files"
cp "$ROOT/tests/fixtures/opatch-compatibility/actions.xml" "$TMP/artifact/etc/config/actions.xml"
printf '<patch patchID="12345678"><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n' >"$TMP/artifact/etc/config/inventory.xml"
printf 'payload\n' >"$TMP/artifact/files/placeholder.bin"
cp "$ROOT/tests/fixtures/opatch-compatibility/opatch" "$TMP/grid/OPatch/opatch"
chmod 700 "$TMP/grid/OPatch/opatch"
"$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/artifact" --output "$TMP/artifact.json" >/dev/null
artifact_path=$(jq -r '.artifact.path' "$TMP/artifact.json")
jq -n --arg home "$TMP/grid" --arg me "$me" --arg digest "$digest" '{schema_version:"1.0",collector:{name:"oracle.topology.discover"},host:{name:"node1.example"},cluster:{grid_home:$home},oracle_homes:[{path:$home,owner:$me,platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest}}],databases:[]}' >"$TMP/snapshot.json"
sha=$(jq -r '.artifact.sha256' "$TMP/artifact.json")
jq -n --arg sha "$sha" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$sha,required_opatch_version:"12.2.0.1",target:{family:"grid",method:"opatch",platform_id:"226"},execution:{adapter:"grid_rolling_opatch",operations:["grid_rootcrs_prepatch","grid_opatch_apply","grid_rootadd_rdbms","grid_rootcrs_postpatch"]}}}' >"$TMP/procedure.json"
"$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/snapshot.json" --artifact "$artifact_path" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --evidence-dir "$TMP/evidence" --output "$TMP/result.json" >/dev/null
jq -e '.status == "passed" and .target.platform_id == "226" and .checks[0].platform.status == "passed" and .checks[0].applicability_check.name == "CheckPatchApplicableOnCurrentPlatform" and .checks[0].applicability_check.status == "passed" and .checks[0].applicability_check.exit_code == 0 and (.checks[0].applicability_check.evidence_sha256 | test("^[a-f0-9]{64}$")) and .checks[0].conflict_check.name == "CheckConflictAgainstOHWithDetail" and .checks[0].conflict_check.status == "passed" and .checks[0].conflict_check.exit_code == 0 and (.checks[0].conflict_check.evidence_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/result.json" >/dev/null
# A preexisting evidence path must never redirect root collector logs.
mkdir "$TMP/planted"
printf 'unrelated original' >"$TMP/victim"
ln -s "$TMP/victim" "$TMP/planted/node1-grid-opatch-platform-12345678.log"
if "$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/snapshot.json" --artifact "$artifact_path" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" --evidence-dir "$TMP/planted" >/dev/null 2>&1; then
  echo 'preexisting compatibility log directory accepted' >&2; exit 1
fi
[ "$(cat "$TMP/victim")" = 'unrelated original' ]
# The old predictable default is ignored; each collection preserves its own logs.
mkdir "$TMP/opu-opatch-compatibility-12345678"
ln -s "$TMP/victim" "$TMP/opu-opatch-compatibility-12345678/node1-grid-opatch-platform-12345678.log"
for attempt in one two; do
  TMPDIR="$TMP" "$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/snapshot.json" --artifact "$artifact_path" --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" >"$TMP/default-$attempt.json"
done
first_log=$(jq -r '.checks[0].applicability_check.evidence_path' "$TMP/default-one.json")
second_log=$(jq -r '.checks[0].applicability_check.evidence_path' "$TMP/default-two.json")
[ "$first_log" != "$second_log" ]
[ -f "$first_log" ] && [ -f "$second_log" ]
[ "$(sha256sum "$first_log" | awk '{print $1}')" = "$(jq -r '.checks[0].applicability_check.evidence_sha256' "$TMP/default-one.json")" ]
[ "$(cat "$TMP/victim")" = 'unrelated original' ]
mkdir -p "$TMP/database/OPatch"
cp "$ROOT/tests/fixtures/opatch-compatibility/opatch" "$TMP/database/OPatch/opatch"
chmod 700 "$TMP/database/OPatch/opatch"
mkdir -p "$TMP/staged/12345678/etc/config" "$TMP/staged/12345678/files"
cp "$ROOT/tests/fixtures/opatch-compatibility/actions.xml" "$TMP/staged/12345678/actions.xml"
cp "$ROOT/tests/fixtures/opatch-compatibility/actions.xml" "$TMP/staged/12345678/etc/config/actions.xml"
printf '<patch patchID="12345678"><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n' >"$TMP/staged/12345678/etc/config/inventory.xml"
printf 'payload\n' >"$TMP/staged/12345678/files/placeholder.bin"
"$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/staged/12345678" --output "$TMP/database-artifact.json" >/dev/null
staged_path=$(jq -r '.artifact.path' "$TMP/database-artifact.json")
jq -n --arg home "$TMP/database" --arg me "$me" --arg digest "$digest" '{schema_version:"1.0",collector:{name:"oracle.topology.discover"},host:{name:"standalone.example"},cluster:{grid_home:null},oracle_homes:[{path:$home,owner:$me,platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:$digest}}],databases:[{db_unique_name:"ORCL",oracle_home:$home}]}' >"$TMP/database-snapshot.json"
database_sha=$(jq -r '.artifact.sha256' "$TMP/database-artifact.json")
jq -n --arg sha "$database_sha" '{schema_version:"1.0",status:"ready_for_planning",procedure:{patch_id:"12345678",artifact_sha256:$sha,required_opatch_version:"12.2.0.1",target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]}}}' >"$TMP/database-procedure.json"
"$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/database-snapshot.json" --artifact "$staged_path" --artifact-manifest "$TMP/database-artifact.json" --procedure-validation "$TMP/database-procedure.json" --evidence-dir "$TMP/database-evidence" --output "$TMP/database-result.json" >/dev/null
jq -e '.status == "passed" and .checks[0].home == $home and .checks[0].conflict_check.patch_option == "-ph" and .checks[0].conflict_check.patch_source == $source' --arg home "$TMP/database" --arg source "$staged_path" "$TMP/database-result.json" >/dev/null
grep -F -- "CheckPatchApplicableOnCurrentPlatform -ph $staged_path" "$TMP/database-evidence/standalone-database-opatch-platform-12345678.log" >/dev/null
grep -F -- "CheckConflictAgainstOHWithDetail -ph $staged_path" "$TMP/database-evidence/standalone-database-opatch-conflict-12345678.log" >/dev/null
jq -e '.findings == [] and (.checks[0].applicability_check | has("detail") | not)' "$TMP/database-result.json" >/dev/null

# A failed prerequisite must carry OPatch's own reason in the evidence
# document (detail + findings) so the block is explainable without a login.
set +e
OPU_TEST_OPATCH_PREREQ_FAIL='Unable to create Patch Object. Exception occured : PatchObject constructor: Current patch location is one-level down. Please point patch location to "/u01/stage/12345678" and retry.' \
  "$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/database-snapshot.json" --artifact "$staged_path" --artifact-manifest "$TMP/database-artifact.json" --procedure-validation "$TMP/database-procedure.json" --evidence-dir "$TMP/database-evidence-fail" --output "$TMP/database-fail.json" >/dev/null
fail_rc=$?
set -e
[ "$fail_rc" -eq 2 ]
jq -e '.status == "blocked" and .checks[0].applicability_check.status == "failed" and .checks[0].applicability_check.exit_code == 2 and (.checks[0].applicability_check.detail | test("one-level down")) and (.checks[0].conflict_check.detail | test("one-level down")) and (.findings | length == 2) and (.findings[0] | test("^CheckPatchApplicableOnCurrentPlatform failed on .* \\(exit 2\\): Unable to create Patch Object"))' "$TMP/database-fail.json" >/dev/null

# Metadata-only media (no files/ payload) is diagnosed before OPatch runs.
rm -rf "$TMP/staged/12345678/files"
set +e
"$ROOT/bin/opu-opatch-compatibility-collect" --snapshot "$TMP/database-snapshot.json" --artifact "$staged_path" --artifact-manifest "$TMP/database-artifact.json" --procedure-validation "$TMP/database-procedure.json" --evidence-dir "$TMP/database-evidence-nopayload" --output "$TMP/database-nopayload.json" >/dev/null
nopayload_rc=$?
set -e
[ "$nopayload_rc" -eq 2 ]
jq -e '.status == "blocked" and (.findings | length == 1) and (.findings[0] | test("Incomplete patch media: missing files/ payload")) and .checks[0].applicability_check.exit_code == 66 and (.checks[0].applicability_check.detail | test("missing files/ payload"))' "$TMP/database-nopayload.json" >/dev/null
printf '%s\n' 'OPatch compatibility collector test passed'
