#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
. "$ROOT/tests/fixtures/retire_plans.sh"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-single-instance.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# macOS does not provide util-linux flock. Supply a narrow test double that
# acquires a real BSD flock on the inherited descriptor so this suite still
# exercises executor-lock descriptor inheritance exactly as Oracle Linux does.
if ! command -v flock >/dev/null 2>&1; then
  mkdir -p "$TMP/test-bin"
  FLOCK_PROBE="$TMP/flock-called"
  OPU_SINGLE_INSTANCE_TEST_FLOCK="$TMP/test-bin/flock"
  export FLOCK_PROBE OPU_SINGLE_INSTANCE_TEST_FLOCK
  cat >"$TMP/test-bin/flock" <<'PY'
#!/usr/bin/python3
import fcntl
import os
import sys

if sys.argv[1:] != ["-n", "8"]:
    raise SystemExit(64)

try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)

with open(os.environ["FLOCK_PROBE"], "a", encoding="utf-8") as marker:
    marker.write("acquired\n")
PY
  chmod 0750 "$TMP/test-bin/flock"
fi

PLAN_TOOL="$ROOT/bin/opu-patch-plan"
EXECUTOR="$ROOT/bin/opu-database-single-instance-patch"
PLAN_STATE="$TMP/plan-state"
EXECUTION_STATE="$TMP/execution-state"
ROLLBACK_STATE="$TMP/rollback-state"
TEST_HOME="$TMP/oracle/dbhome_1"
PATCH_DIR="$TMP/patch/39034528"
BACKUP_ROOT="$TMP/backup/ORCL/run-001"
PATCH_STATE="$TMP/patch.state"
DATABASE_STATE="$TMP/database.state"
LISTENER_STATE="$TMP/listener.state"
DATAPATCH_STATE="$TMP/datapatch.state"
FAIL_OPATCH="$TMP/fail-opatch"
FAIL_APPLICABILITY="$TMP/fail-applicability"
FAIL_DATAPATCH="$TMP/fail-datapatch"
FAIL_ROLLBACK="$TMP/fail-rollback"
SQLPATCH_ACTION_STATE="$TMP/sqlpatch-action.state"
OPATCH_CALLS="$TMP/opatch-calls.log"
CAPACITY_BYTES=""
DATAPATCH_CALLS="$TMP/datapatch-calls.log"
FD_CALLS="$TMP/fd-calls.log"
SQL_FLAGS="$TMP/sql-flags"
mkdir "$SQL_FLAGS"
export OPU_TEST_SQL_FLAGS="$SQL_FLAGS" OPU_TEST_DATAPATCH_CALLS="$DATAPATCH_CALLS" OPU_TEST_FD_CALLS="$FD_CALLS"

mkdir -p "$TEST_HOME/bin" "$TEST_HOME/OPatch" "$TEST_HOME/jdk/bin" \
  "$PATCH_DIR/etc/config" "$BACKUP_ROOT/rman" "$BACKUP_ROOT/oracle-home"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"

cat >"$TEST_HOME/bin/sqlplus" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
fd=closed; if { : >&7; } 2>/dev/null; then fd=open; fi
if grep -q '^startup;' <<<"$input"; then
  [ "$fd" = closed ] || { echo 'startup inherited the host lock' >&2; exit 91; }
  printf 'startup:%s\n' "$fd" >>"$OPU_TEST_FD_CALLS"
else
  [ "$fd" = open ] || { echo 'SQL probe or shutdown lost the host lock' >&2; exit 92; }
  printf 'sql:%s\n' "$fd" >>"$OPU_TEST_FD_CALLS"
fi
if grep -q 'shutdown immediate' <<<"$input"; then
  [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
  printf 'down\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q '^startup;' <<<"$input"; then
  printf 'up\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q 'NONVALID_COMPONENTS=' <<<"$input"; then
  invalid=0; components=0
  [ ! -f "$OPU_TEST_SQL_FLAGS/invalid-objects" ] || invalid=1
  [ ! -f "$OPU_TEST_SQL_FLAGS/invalid-component" ] || components=1
  printf '%s\n' "INVALID_OBJECTS=$invalid" 'ENABLED_COMPONENTS=2' "NONVALID_COMPONENTS=$components" 'COMPONENT=CATALOG|VALID' 'COMPONENT=RAC|OPTION OFF'
elif grep -q 'SQLPATCH_LATEST_ACTION=' <<<"$input"; then
  action=$(cat "$OPU_TEST_SQLPATCH_ACTION_STATE" 2>/dev/null || true)
  [ -n "$action" ] || exit 0
  printf '%s\n' "SQLPATCH_LATEST_ACTION=$action" 'SQLPATCH_LATEST_STATUS=SUCCESS'
elif grep -q 'SQLPATCH_SUCCESS=' <<<"$input"; then
  if [ -f "$OPU_TEST_DATAPATCH_STATE" ]; then
    printf '%s\n' 'SQLPATCH_SUCCESS=1' 'SQLPATCH_NON_SUCCESS=0'
  else
    printf '%s\n' 'SQLPATCH_SUCCESS=0' 'SQLPATCH_NON_SUCCESS=0'
  fi
elif grep -q 'alter system register' <<<"$input"; then
  :
else
  [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
  printf '%s\n' \
    'INSTANCE_NAME=ORCL' \
    'INSTANCE_STATUS=OPEN' \
    'DATABASE_UNIQUE_NAME=ORCL' \
    "CDB=${OPU_TEST_CDB-NO}" \
    'DATABASE_ROLE=PRIMARY' \
    'OPEN_MODE=READ WRITE' \
    'LOG_MODE=ARCHIVELOG' \
    'INVALID_OBJECTS=0'
fi
EOF

cat >"$TEST_HOME/bin/lsnrctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
fd=closed; if { : >&7; } 2>/dev/null; then fd=open; fi
case "${1:-}" in start) [ "$fd" = closed ] || exit 93;; *) [ "$fd" = open ] || exit 94;; esac
printf 'listener-%s:%s\n' "${1:-}" "$fd" >>"$OPU_TEST_FD_CALLS"
if [ "${1:-}" = stop ] && [ -f "$OPU_TEST_SQL_FLAGS/fail-listener-stop" ]; then exit 76; fi
case "${1:-}" in
  start) printf 'up\n' >"$OPU_TEST_LISTENER_STATE"; printf 'listener started\n' ;;
  stop) printf 'down\n' >"$OPU_TEST_LISTENER_STATE"; printf 'listener stopped\n' ;;
  status)
    [ "$(cat "$OPU_TEST_LISTENER_STATE")" = up ] || exit 1
    [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
    printf '%s\n' 'Instance "ORCL", status READY, has 1 handler(s) for this service...'
    ;;
  *) exit 64 ;;
esac
EOF

cat >"$TEST_HOME/OPatch/opatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$OPU_TEST_OPATCH_CALLS"
{ : >&7; } 2>/dev/null || { echo 'OPatch lost the shared host lock' >&2; exit 95; }
printf 'opatch:open\n' >>"$OPU_TEST_FD_CALLS"
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$OPU_TEST_PATCH_STATE" ] && printf '%s\n' '39034528;Database Release Update' || true ;;
  lsinventory)
    [ "${2:-}" = -xml ] && [ -n "${3:-}" ] || exit 64
    if [ -f "$OPU_TEST_SQL_FLAGS/malformed-xml" ]; then printf '<broken' >"$3"; else
      printf '<inventory><home>%s</home><patches>%s</patches></inventory>\n' "$ORACLE_HOME" "$(cat "$OPU_TEST_PATCH_STATE" 2>/dev/null || true)" >"$3"
    fi
    ;;
  prereq)
    case "${2:-}" in
      CheckPatchApplicableOnCurrentPlatform)
        [ ! -f "$OPU_TEST_FAIL_APPLICABILITY" ] || { printf 'Prerequisite check "CheckPatchApplicableOnCurrentPlatform" failed.\nOPatch failed with error code 73\n' >&2; exit 73; }
        printf '%s\n' 'Prereq "CheckPatchApplicableOnCurrentPlatform" passed.'
        ;;
      CheckConflictAgainstOHWithDetail) printf '%s\n' 'Prereq "CheckConflictAgainstOHWithDetail" passed.' ;;
      *) printf 'unsupported fake OPatch prerequisite: %s\n' "${2:-}" >&2; exit 64 ;;
    esac
    ;;
  apply)
    [ ! -f "$OPU_TEST_FAIL_OPATCH" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf '39034528\n' >"$OPU_TEST_PATCH_STATE"
    if [ -f "$OPU_TEST_SQL_FLAGS/extjob-reset-by-opatch" ]; then chmod 0700 "$ORACLE_HOME/bin/extjob"; fi
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    # Real OPatch prompts "Is the local system ready for patching? [y|n]" and
    # exits 73 under non-interactive automation unless -silent is passed.
    case " $* " in *' -silent '*) ;; *) printf 'Is the local system ready for patching? [y|n]\nOPatch failed with error code 73\n'; exit 73 ;; esac
    [ ! -f "$OPU_TEST_FAIL_ROLLBACK" ] || { printf 'simulated OPatch rollback failure\n' >&2; exit 73; }
    rm -f "$OPU_TEST_PATCH_STATE"
    printf '%s\n' 'OPatch rollback succeeded.'
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
EOF

cat >"$TEST_HOME/OPatch/datapatch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$OPU_TEST_DATAPATCH_CALLS"
{ : >&7; } 2>/dev/null || { echo 'datapatch lost the host lock' >&2; exit 96; }
printf 'datapatch:open\n' >>"$OPU_TEST_FD_CALLS"
if [ "${1:-}" = -help ]; then
  if [ ! -f "$OPU_TEST_SQL_FLAGS/no-local-inventory" ]; then printf '%s\n' '  -local_inventory [xml_filename]'; fi
  exit 0
fi
case " $* " in *' -noqi '*|*' -apply '*|*' -rollback '*|*' -force '*) echo 'inventory bypass or forced patch selection' >&2; exit 97;; esac
[ "${1:-}" = -verbose ] || exit 64
if [ "$#" -gt 1 ]; then
  [ "$#" -eq 3 ] && [ "$2" = -local_inventory ] && [ -s "$3" ] || exit 64
  xmllint --nonet --noout "$3" || exit 65
fi
[ ! -f "$OPU_TEST_FAIL_DATAPATCH" ] || { printf 'simulated datapatch failure\n' >&2; exit 74; }
[ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ]
if [ -f "$OPU_TEST_PATCH_STATE" ]; then action=APPLY; else action=ROLLBACK; fi
if [ -f "$OPU_TEST_SQL_FLAGS/opposite-latest" ]; then
  if [ "$action" = APPLY ]; then action=ROLLBACK; else action=APPLY; fi
fi
printf '%s\n' "$action" >"$OPU_TEST_SQLPATCH_ACTION_STATE"
printf 'complete\n' >"$OPU_TEST_DATAPATCH_STATE"
if [ -f "$OPU_TEST_SQL_FLAGS/datapatch-log-error" ]; then printf 'ORA-20001: simulated SQL error\n'; fi
printf '%s\n' 'SQL Patching tool complete.'
EOF

mkdir -p "$TEST_HOME/perl/bin" "$TEST_HOME/rdbms/admin"
printf 'fixture Oracle catcon\n' >"$TEST_HOME/rdbms/admin/catcon.pl"
printf 'fixture Oracle utlrp\n' >"$TEST_HOME/rdbms/admin/utlrp.sql"
printf 'fixture extjob; never executed\n' >"$TEST_HOME/bin/extjob"
# TEST_MODE models the permission check using this UID and ordinary 0750;
# fixtures do not need or create a setuid executable.
chmod 0750 "$TEST_HOME/bin/extjob"
cat >"$TEST_HOME/perl/bin/perl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
{ : >&7; } 2>/dev/null || exit 98
[ "$*" = "$ORACLE_HOME/rdbms/admin/catcon.pl -n 1 -e -b utlrp -d $ORACLE_HOME/rdbms/admin utlrp.sql" ] || exit 64
printf 'recompile:open\n' >>"$OPU_TEST_FD_CALLS"
printf '%s\n' 'SQL> Rem add support for ORA-30552' 'SQL> Rem add support for ORA-38301' ' 49  -- due to ORA-30552 during ALTER INDEX...ENABLE command' 'ERRORS DURING RECOMPILATION' '0' >utlrp0.log
if [ -f "$OPU_TEST_SQL_FLAGS/utlrp-log-error" ]; then printf 'ORA-00604: simulated recompile failure\n' >>utlrp0.log; fi
if [ -f "$OPU_TEST_SQL_FLAGS/utlrp-exit-error" ]; then exit 78; fi
EOF
chmod 750 "$TEST_HOME/perl/bin/perl"

cat >"$TEST_HOME/jdk/bin/java" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 750 "$TEST_HOME/bin/sqlplus" "$TEST_HOME/bin/lsnrctl" \
  "$TEST_HOME/OPatch/opatch" "$TEST_HOME/OPatch/datapatch" "$TEST_HOME/jdk/bin/java"

cat >"$PATCH_DIR/etc/config/inventory.xml" <<'EOF'
<patch patchID="39034528"><description>Database Release Update</description><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>
EOF
# artifact-inspect fails closed on metadata-only stage dirs; give OPatch a payload tree.
mkdir -p "$PATCH_DIR/files/lib" && printf 'test patch payload\n' >"$PATCH_DIR/files/lib/libtestpatch.so"
printf '%s\n' \
  'Database Release Update test README' \
  'opatch rollback -id 39034528' \
  'datapatch -verbose' \
  'chown root $ORACLE_HOME/bin/extjob' \
  'chmod 4750 $ORACLE_HOME/bin/extjob' >"$PATCH_DIR/README.txt"
"$ROOT/bin/opu-artifact-inspect" --artifact "$PATCH_DIR" --output "$TMP/artifact.json" >/dev/null
ARTIFACT_SHA=$(jq -r '.artifact.sha256' "$TMP/artifact.json")
README_SHA=$(sha256sum "$PATCH_DIR/README.txt" | awk '{print $1}')

printf 'backup piece\n' >"$BACKUP_ROOT/rman/piece-01"
printf 'Oracle home archive\n' >"$BACKUP_ROOT/oracle-home/dbhome.tar.gz"
sha256sum "$BACKUP_ROOT/rman/piece-01" "$BACKUP_ROOT/oracle-home/dbhome.tar.gz" >"$BACKUP_ROOT/SHA256SUMS"
printf '%s\n' 'RMAN validation complete' >"$TMP/rman-validate.log"
CHECKSUM_SHA=$(sha256sum "$BACKUP_ROOT/SHA256SUMS" | awk '{print $1}')
RMAN_SHA=$(sha256sum "$TMP/rman-validate.log" | awk '{print $1}')
OWNER=$(id -un)

jq -cn --arg database ORCL --arg oracle_home "$TEST_HOME" --arg owner "$OWNER" \
  --arg backup_root "$BACKUP_ROOT" --arg checksum_path "$BACKUP_ROOT/SHA256SUMS" \
  --arg checksum_sha "$CHECKSUM_SHA" --arg rman_log "$TMP/rman-validate.log" --arg rman_sha "$RMAN_SHA" \
  '{schema_version:"1.0",collector:{name:"oracle.recovery.evidence",version:"1"},status:"passed",target:{database_unique_name:$database,oracle_home:$oracle_home,owner:$owner,oracle_sid:"ORCL"},backup:{root:$backup_root,checksum_manifest:{path:$checksum_path,sha256:$checksum_sha},files:[],oracle_home_archive:($backup_root+"/oracle-home/dbhome.tar.gz"),rman_backup_set_keys:[1],selected_recovery_set:{observed_at:(now|todateiso8601),oldest_datafile_backup_completed_at:(now|todateiso8601),age_seconds_at_collection:0,restore_piece_handles:[($backup_root+"/backup-piece")],datafile_backup_sets:[1]},coverage:{datafiles_backed:1,base_datafiles:1,datafiles_current:1,controlfile_records:1,spfile_records:1,outside_root_pieces:0,unavailable_pieces:0}},verification:{rman_log:{path:$rman_log,sha256:$rman_sha}}}' >"$TMP/recovery.tmp"
RECOVERY_RECORD=$(jq -cS . "$TMP/recovery.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$RECOVERY_RECORD" '.record_sha256=$record' "$TMP/recovery.tmp" >"$TMP/recovery.json"

EVIDENCE_NOW=$(date -u +%s)
EVIDENCE_COLLECTED=$(date -u -r "$EVIDENCE_NOW" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$EVIDENCE_NOW" '+%Y-%m-%dT%H:%M:%SZ')
EVIDENCE_VALID=$(date -u -r "$((EVIDENCE_NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((EVIDENCE_NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ')
jq -n --arg collected "$EVIDENCE_COLLECTED" '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,host:{name:"testnode.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},oracle_homes:[],databases:[],warnings:[]}' >"$TMP/topology-snapshot.json"
SNAPSHOT_SHA=$(sha256sum "$TMP/topology-snapshot.json" | awk '{print $1}')
jq --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" '.source_snapshot={path:$snapshot,sha256:$snapshot_sha}' "$TMP/recovery.json" >"$TMP/recovery.with-snapshot.json"
RECOVERY_RECORD=$(jq -cS 'del(.record_sha256)' "$TMP/recovery.with-snapshot.json" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$RECOVERY_RECORD" '.record_sha256=$record' "$TMP/recovery.with-snapshot.json" >"$TMP/recovery.json"
jq -n --arg oracle_home "$TEST_HOME" --arg owner "$OWNER" --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" \
  '{schema_version:"1.0",status:"consistent",expected_nodes:["testnode"],oracle_homes:[{path:$oracle_home,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml"},patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:$oracle_home}],snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha}]}' >"$TMP/reconciliation.json"
jq -n --arg artifact_sha "$ARTIFACT_SHA" --arg readme_sha "$README_SHA" \
  '{schema_version:"1.0",status:"ready_for_planning",procedure:{schema_version:"1.0",patch_id:"39034528",artifact_sha256:$artifact_sha,target:{family:"database",method:"opatch",topology:"single_instance",database_unique_name:"ORCL",platform_id:"226"},execution:{adapter:"database_single_instance_opatch",operations:["database_shutdown","database_opatch_apply","database_startup","database_datapatch"]},required_opatch_version:"12.2.0.1.49",oracle_references:[{kind:"patch_readme",identifier:"README rollback sections",sha256:$readme_sha}],rollback:{mode:"opatch_rollback",precondition:"separate approved rollback plan required"}}}' >"$TMP/procedure.json"
jq -n --arg home "$TEST_HOME" --arg digest "$ARTIFACT_SHA" '{schema_version:"1.0",status:"passed",patch_id:"39034528",artifact_sha256:$digest,target:{family:"database",platform_id:"226"},checks:[{node:"testnode",home:$home,owner:"test",status:"passed",platform:{host_id:"226",host_name:"Linux x86-64",artifact_ids:["226"],procedure_id:"226",status:"passed"},opatch:{actual_version:"12.2.0.1.51",required_version:"12.2.0.1.49",status:"passed"},applicability_check:{name:"CheckPatchApplicableOnCurrentPlatform",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/applicability.log",evidence_sha256:$digest},conflict_check:{name:"CheckConflictAgainstOHWithDetail",status:"passed",exit_code:0,patch_option:"-ph",patch_source:"/tmp/patch",evidence_path:"/tmp/conflict.log",evidence_sha256:$digest}}]}' >"$TMP/compatibility.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,require_xml_inventory:true,recovery:{require_backup:true,max_backup_age_minutes:1440,minimum_fra_free_bytes:0,require_guaranteed_restore_point:false},database:{require_primary_read_write:true,maximum_invalid_objects:0}}' >"$TMP/policy.json"

RECONCILIATION_SHA=$(sha256sum "$TMP/reconciliation.json" | awk '{print $1}')
ARTIFACT_MANIFEST_SHA=$(sha256sum "$TMP/artifact.json" | awk '{print $1}')
PROCEDURE_SHA=$(sha256sum "$TMP/procedure.json" | awk '{print $1}')
COMPATIBILITY_SHA=$(sha256sum "$TMP/compatibility.json" | awk '{print $1}')
POLICY_SHA=$(sha256sum "$TMP/policy.json" | awk '{print $1}')
jq -n --arg reconciliation "$RECONCILIATION_SHA" --arg artifact "$ARTIFACT_MANIFEST_SHA" --arg procedure "$PROCEDURE_SHA" --arg compatibility "$COMPATIBILITY_SHA" --arg policy "$POLICY_SHA" \
  --arg evaluated "$EVIDENCE_COLLECTED" --arg valid "$EVIDENCE_VALID" --arg snapshot "$TMP/topology-snapshot.json" --arg snapshot_sha "$SNAPSHOT_SHA" \
  '{schema_version:"1.0",status:"ready_for_approval",patch_id:"39034528",target:{family:"database",method:"opatch",platform_id:"226"},evaluated_at:$evaluated,valid_until:$valid,snapshot_evidence:[{path:$snapshot,sha256:$snapshot_sha,host:"testnode",collected_at:$evaluated,valid_until:$valid}],evidence:{reconciliation_sha256:$reconciliation,artifact_manifest_sha256:$artifact,procedure_validation_sha256:$procedure,compatibility_sha256:$compatibility,policy_sha256:$policy}}' >"$TMP/readiness.json"

NOW=$(date -u +%s)
WINDOW_START=$(date -u -r "$((NOW-60))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW-60))" '+%Y-%m-%dT%H:%M:%SZ')
WINDOW_END=$(date -u -r "$((NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW+3600))" '+%Y-%m-%dT%H:%M:%SZ')
printf 'ORCL:%s:N\n' "$TEST_HOME" >"$TMP/oratab"

plan() { OPU_PLAN_STATE_DIR="$PLAN_STATE" "$PLAN_TOOL" "$@"; }
create_plan() {
  local plan_id=$1
  plan create --plan-id "$plan_id" --requester patch-admin \
    --readiness "$TMP/readiness.json" --reconciliation "$TMP/reconciliation.json" \
    --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" \
    --compatibility "$TMP/compatibility.json" --policy "$TMP/policy.json" \
    --recovery-evidence "$TMP/recovery.json" --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$plan_id" --actor dba-approver --approval-ticket TEST-39034528 >/dev/null
  plan authorize --plan-id "$plan_id" --actor patch-operator >/dev/null
  # Independent simulated target scenario; lifecycle tests cover retained reservations.
  retire_fixture_plans "$PLAN_STATE"
  plan dispatch --plan-id "$plan_id" --actor patch-operator >/dev/null
}

execute() {
  local plan_id=$1 task_id=$2 actor=${3:-standalone-worker}
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_SINGLE_INSTANCE_STATE_DIR="$EXECUTION_STATE" \
  OPU_SINGLE_INSTANCE_TEST_MODE=1 \
  OPU_SINGLE_INSTANCE_ORATAB="$TMP/oratab" \
  OPU_SINGLE_INSTANCE_TEST_LISTENER=LISTENER \
  OPU_SINGLE_INSTANCE_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_LISTENER_STATE="$LISTENER_STATE" \
  OPU_TEST_PATCH_STATE="$PATCH_STATE" \
  OPU_TEST_FAIL_APPLICABILITY="$FAIL_APPLICABILITY" \
  OPU_TEST_OPATCH_CALLS="$OPATCH_CALLS" \
  OPU_TEST_DATAPATCH_STATE="$DATAPATCH_STATE" \
  OPU_TEST_FAIL_OPATCH="$FAIL_OPATCH" \
  OPU_TEST_FAIL_DATAPATCH="$FAIL_DATAPATCH" \
  OPU_TEST_FAIL_ROLLBACK="$FAIL_ROLLBACK" \
  OPU_TEST_SQLPATCH_ACTION_STATE="$SQLPATCH_ACTION_STATE" \
  OPU_SINGLE_INSTANCE_TEST_APPLY_CAPACITY_BYTES="$CAPACITY_BYTES" \
    "$EXECUTOR" execute --plan-id "$plan_id" --task-id "$task_id" --actor "$actor" --lease-seconds 30
}

execute_rollback() {
  local plan_id=$1 task_id=$2
  OPU_PLAN_STATE_DIR="$PLAN_STATE" \
  OPU_SINGLE_INSTANCE_ROLLBACK_STATE_DIR="$ROLLBACK_STATE" \
  OPU_SINGLE_INSTANCE_TEST_MODE=1 \
  OPU_SINGLE_INSTANCE_ORATAB="$TMP/oratab" \
  OPU_SINGLE_INSTANCE_TEST_LISTENER=LISTENER \
  OPU_SINGLE_INSTANCE_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_DATABASE_STATE="$DATABASE_STATE" \
  OPU_TEST_LISTENER_STATE="$LISTENER_STATE" \
  OPU_TEST_PATCH_STATE="$PATCH_STATE" \
  OPU_TEST_FAIL_APPLICABILITY="$FAIL_APPLICABILITY" \
  OPU_TEST_OPATCH_CALLS="$OPATCH_CALLS" \
  OPU_TEST_DATAPATCH_STATE="$DATAPATCH_STATE" \
  OPU_TEST_FAIL_OPATCH="$FAIL_OPATCH" \
  OPU_TEST_FAIL_DATAPATCH="$FAIL_DATAPATCH" \
  OPU_TEST_FAIL_ROLLBACK="$FAIL_ROLLBACK" \
  OPU_TEST_SQLPATCH_ACTION_STATE="$SQLPATCH_ACTION_STATE" \
    "$ROOT/bin/opu-database-single-instance-rollback" execute --plan-id "$plan_id" --task-id "$task_id" --actor rollback-worker --lease-seconds 30
}

# Current adapters cannot verify every PDB and seed SQL state. Prove the real
# precheck rejects CDB/unknown scope while leaving database and binaries alone.
for scope in YES ''; do
  scope_plan=standalone-cdb-${scope:-unknown}
  create_plan "$scope_plan"
  scope_task=$(plan next --plan-id "$scope_plan" | jq -r '.task_id')
  if OPU_TEST_CDB="$scope" execute "$scope_plan" "$scope_task" >"$TMP/$scope_plan.out" 2>&1; then
    echo 'unsupported or unknown CDB scope passed native precheck' >&2; exit 1
  fi
  grep -F 'non-CDB databases only' "$EXECUTION_STATE/plans/$scope_plan/tasks/$scope_task/stderr.log" >/dev/null
  [ "$(cat "$DATABASE_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
  plan status --plan-id "$scope_plan" | jq -e '.state == "paused"' >/dev/null
done

# Artifact tampering must fail before any database mutation.
create_plan standalone-tamper
cp "$PATCH_DIR/README.txt" "$TMP/README.original"
printf 'tamper\n' >>"$PATCH_DIR/README.txt"
TAMPER_TASK=$(plan next --plan-id standalone-tamper | jq -r '.task_id')
if execute standalone-tamper "$TAMPER_TASK" >/dev/null 2>&1; then
  echo 'tampered artifact was accepted by the standalone executor' >&2
  exit 1
fi
plan status --plan-id standalone-tamper | jq -e '.state == "paused"' >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
cp "$TMP/README.original" "$PATCH_DIR/README.txt"

# Insufficient Oracle-home free space must block precheck before any
# database mutation, same as the other precheck-stage guards above.
create_plan standalone-nospace
CAPACITY_TASK=$(plan next --plan-id standalone-nospace | jq -r '.task_id')
CAPACITY_BYTES=1
if execute standalone-nospace "$CAPACITY_TASK" >/dev/null 2>&1; then
  echo 'insufficient Oracle home capacity was accepted by the standalone executor' >&2
  exit 1
fi
CAPACITY_BYTES=""
plan status --plan-id standalone-nospace | jq -e '.state == "paused"' >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]

# Platform applicability must also fail closed in the first precheck, preserve
# OPatch's native return code, and leave all services and binaries untouched.
create_plan standalone-platform-precheck-failure
touch "$FAIL_APPLICABILITY"
PLATFORM_INITIAL_PRECHECK=$(plan next --plan-id standalone-platform-precheck-failure | jq -r '.task_id')
set +e
execute standalone-platform-precheck-failure "$PLATFORM_INITIAL_PRECHECK" >/dev/null 2>&1
PLATFORM_INITIAL_RC=$?
set -e
[ "$PLATFORM_INITIAL_RC" -eq 73 ]
plan status --plan-id standalone-platform-precheck-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.exit_code == 73 and .outcome_class == "no_mutation"' \
  "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK/evidence.json" >/dev/null
[ "$(cat "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK/precheck-platform-applicability.log.exit-code")" -eq 73 ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
rm -f "$FAIL_APPLICABILITY"

# Retry the actual executor without deleting its first evidence directory.
FIRST_ATTEMPT_SHA=$(sha256sum "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK/evidence.json" | awk '{print $1}')
plan retry-task --plan-id standalone-platform-precheck-failure --task-id "$PLATFORM_INITIAL_PRECHECK" --actor patch-operator >/dev/null
execute standalone-platform-precheck-failure "$PLATFORM_INITIAL_PRECHECK" >"$TMP/precheck-retry-result.json"
jq -e '.status == "succeeded" and .retry_count == 1' "$TMP/precheck-retry-result.json" >/dev/null
[ -f "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK-retry1/evidence.json" ]
[ "$(sha256sum "$EXECUTION_STATE/plans/standalone-platform-precheck-failure/tasks/$PLATFORM_INITIAL_PRECHECK/evidence.json" | awk '{print $1}')" = "$FIRST_ATTEMPT_SHA" ]
jq -e '.status == "failed"' "$PLAN_STATE/plans/standalone-platform-precheck-failure/attempts/$PLATFORM_INITIAL_PRECHECK/attempt-0.json" >/dev/null
plan task-status --plan-id standalone-platform-precheck-failure --task-id "$PLATFORM_INITIAL_PRECHECK" | jq -e '.status == "succeeded" and .retry_count == 1' >/dev/null

# The apply task must repeat platform applicability before any outage. If the
# runtime prerequisite changes after precheck, the plan pauses without
# stopping the database or listener and without touching binary inventory.
create_plan standalone-platform-failure
PLATFORM_PRECHECK=$(plan next --plan-id standalone-platform-failure | jq -r '.task_id')
execute standalone-platform-failure "$PLATFORM_PRECHECK" >/dev/null
touch "$FAIL_APPLICABILITY"
PLATFORM_APPLY=$(plan next --plan-id standalone-platform-failure | jq -r '.task_id')
APPLY_CALLS_BEFORE=$(grep -c '^apply ' "$OPATCH_CALLS" 2>/dev/null || true)
set +e
execute standalone-platform-failure "$PLATFORM_APPLY" >/dev/null 2>&1
PLATFORM_APPLY_RC=$?
set -e
if [ "$PLATFORM_APPLY_RC" -eq 0 ]; then
  echo 'failed platform applicability was accepted by the apply task' >&2
  exit 1
fi
[ "$PLATFORM_APPLY_RC" -eq 73 ]
plan status --plan-id standalone-platform-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.exit_code == 73 and .outcome_class == "no_mutation"' \
  "$EXECUTION_STATE/plans/standalone-platform-failure/tasks/$PLATFORM_APPLY/evidence.json" >/dev/null
[ "$(cat "$EXECUTION_STATE/plans/standalone-platform-failure/tasks/$PLATFORM_APPLY/apply-platform-applicability.log.exit-code")" -eq 73 ]
APPLY_CALLS_AFTER=$(grep -c '^apply ' "$OPATCH_CALLS" 2>/dev/null || true)
[ "$APPLY_CALLS_AFTER" -eq "$APPLY_CALLS_BEFORE" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
rm -f "$FAIL_APPLICABILITY"

# A binary-apply failure must pause without pretending that Oracle recovered.
create_plan standalone-opatch-failure
FAILURE_PRECHECK=$(plan next --plan-id standalone-opatch-failure | jq -r '.task_id')
execute standalone-opatch-failure "$FAILURE_PRECHECK" >/dev/null
touch "$FAIL_OPATCH"
FAILURE_APPLY=$(plan next --plan-id standalone-opatch-failure | jq -r '.task_id')
set +e
execute standalone-opatch-failure "$FAILURE_APPLY" >/dev/null 2>&1
FAILURE_APPLY_RC=$?
set -e
if [ "$FAILURE_APPLY_RC" -eq 0 ]; then
  echo 'simulated OPatch failure was reported as successful' >&2
  exit 1
fi
[ "$FAILURE_APPLY_RC" -eq 73 ]
plan status --plan-id standalone-opatch-failure | jq -e '.state == "paused"' >/dev/null
jq -e '.exit_code == 73 and .outcome_class == "binary_state_unknown"' \
  "$EXECUTION_STATE/plans/standalone-opatch-failure/tasks/$FAILURE_APPLY/evidence.json" >/dev/null
[ "$(cat "$EXECUTION_STATE/plans/standalone-opatch-failure/tasks/$FAILURE_APPLY/opatch-apply.exit-code")" -eq 73 ]
[ "$(cat "$DATABASE_STATE")" = down ] && [ "$(cat "$LISTENER_STATE")" = down ] && [ ! -f "$PATCH_STATE" ]
if plan retry-task --plan-id standalone-opatch-failure --task-id "$FAILURE_APPLY" --actor patch-operator >/dev/null 2>&1; then
  echo 'unknown binary outcome was made retryable' >&2
  exit 1
fi

# A paused mutation task is immutable: replay and deriving a rollback plan from
# the incomplete source plan are both refused.
if execute standalone-opatch-failure "$FAILURE_APPLY" >/dev/null 2>&1; then
  echo 'paused binary task replay was accepted' >&2
  exit 1
fi
if plan create-rollback --plan-id rollback-from-paused-source --requester rollback-admin \
  --source-plan-id standalone-opatch-failure --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null 2>&1; then
  echo 'rollback plan was created from a paused, incomplete apply plan' >&2
  exit 1
fi
rm -f "$FAIL_OPATCH"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"

# Run the complete fixed standalone sequence.
create_plan standalone-success
for expected_stage in precheck apply validate datapatch final_validate; do
  TASK_JSON=$(plan next --plan-id standalone-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  source_worker=standalone-worker
  [ "$expected_stage" != apply ] || source_worker=standalone-apply-worker
  execute standalone-success "$TASK_ID" "$source_worker" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" '.status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.outcome_class | IN("no_mutation","binary_state_known")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done

for report_file in precheck-lspatches.log precheck-health.log precheck-dictionary.log precheck-sqlpatch.log report-listener.log; do
  jq -e --arg name "/$report_file" '[.artifacts[] | select(.path | endswith($name))] | length == 1' "$TMP/precheck-result.json" >/dev/null
done
for report_file in final-lspatches.log final-health.log final-dictionary.log sqlpatch-final.log report-listener.log; do
  jq -e --arg name "/$report_file" '[.artifacts[] | select(.path | endswith($name))] | length == 1' "$TMP/final_validate-result.json" >/dev/null
done

plan status --plan-id standalone-success | jq -e '.state == "succeeded"' >/dev/null
[ -f "$PATCH_STATE" ] && [ -f "$DATAPATCH_STATE" ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ "$(cat "$SQLPATCH_ACTION_STATE")" = APPLY ]
grep -E 'prereq CheckPatchApplicableOnCurrentPlatform -ph .+ -oh .+ -jdk .+' "$OPATCH_CALLS" >/dev/null
grep -E 'prereq CheckConflictAgainstOHWithDetail -ph .+ -oh .+ -jdk .+' "$OPATCH_CALLS" >/dev/null

create_rollback_plan() {
  local rollback_plan_id=$1
  plan create-rollback --plan-id "$rollback_plan_id" --requester rollback-admin \
    --source-plan-id standalone-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
  plan approve --plan-id "$rollback_plan_id" --actor rollback-approver --approval-ticket TEST-ROLLBACK-39034528 >/dev/null
  plan authorize --plan-id "$rollback_plan_id" --actor rollback-operator >/dev/null
  # Independent simulated target scenario; lifecycle tests cover retained reservations.
  retire_fixture_plans "$PLAN_STATE"
  plan dispatch --plan-id "$rollback_plan_id" --actor rollback-operator >/dev/null
}

# Source apply worker cannot approve or authorize the derived rollback plan.
plan create-rollback --plan-id standalone-rollback-sod --requester rollback-admin \
  --source-plan-id standalone-success --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
plan status --plan-id standalone-rollback-sod | jq -e '.source_apply.actors == ["standalone-apply-worker","standalone-worker"]' >/dev/null
for source_worker in standalone-apply-worker standalone-worker; do
  if plan approve --plan-id standalone-rollback-sod --actor "$source_worker" --approval-ticket TEST-ROLLBACK-SOD >/dev/null 2>&1; then
    echo "source apply worker $source_worker was allowed to approve standalone rollback" >&2
    exit 1
  fi
done
plan approve --plan-id standalone-rollback-sod --actor rollback-approver --approval-ticket TEST-ROLLBACK-SOD >/dev/null
for source_worker in standalone-apply-worker standalone-worker; do
  if plan authorize --plan-id standalone-rollback-sod --actor "$source_worker" >/dev/null 2>&1; then
    echo "source apply worker $source_worker was allowed to authorize standalone rollback" >&2
    exit 1
  fi
done

# A failed OPatch rollback is an unknown binary outcome and cannot be retried.
create_rollback_plan standalone-rollback-failure
ROLLBACK_FAILURE_PRECHECK=$(plan next --plan-id standalone-rollback-failure | jq -r '.task_id')
execute_rollback standalone-rollback-failure "$ROLLBACK_FAILURE_PRECHECK" >/dev/null
touch "$FAIL_ROLLBACK"
ROLLBACK_FAILURE_BINARY=$(plan next --plan-id standalone-rollback-failure | jq -r '.task_id')
if execute_rollback standalone-rollback-failure "$ROLLBACK_FAILURE_BINARY" >/dev/null 2>&1; then
  echo 'simulated OPatch rollback failure was reported as successful' >&2
  exit 1
fi
plan status --plan-id standalone-rollback-failure | jq -e '.state == "paused"' >/dev/null
[ "$(jq -r '.outcome_class' "$ROLLBACK_STATE/plans/standalone-rollback-failure/tasks/$ROLLBACK_FAILURE_BINARY/evidence.json")" = binary_state_unknown ]
[ -f "$PATCH_STATE" ] && [ "$(cat "$DATABASE_STATE")" = down ] && [ "$(cat "$LISTENER_STATE")" = down ]
rm -f "$FAIL_ROLLBACK"
printf 'up\n' >"$DATABASE_STATE"; printf 'up\n' >"$LISTENER_STATE"

# A new rollback plan with a fresh approval completes the README-bound flow.
create_rollback_plan standalone-rollback-success
for expected_stage in rollback_precheck rollback_binary rollback_binary_validate rollback_datapatch rollback_final_validate; do
  TASK_JSON=$(plan next --plan-id standalone-rollback-success)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute_rollback standalone-rollback-success "$TASK_ID" >"$TMP/$expected_stage-result.json"
  jq -e --arg stage "$expected_stage" '.intent == "patch_rollback" and .status == "succeeded" and .stage == $stage and .postcondition.status == "passed" and (.outcome_class | IN("no_mutation","binary_state_known")) and (.source_apply.plan_sha256 | test("^[a-f0-9]{64}$")) and (.record_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/$expected_stage-result.json" >/dev/null
done

plan status --plan-id standalone-rollback-success | jq -e '.intent == "patch_rollback" and .state == "succeeded"' >/dev/null
[ ! -f "$PATCH_STATE" ] && [ "$(cat "$SQLPATCH_ACTION_STATE")" = ROLLBACK ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
grep -E '^rollback -id 39034528 -silent( |$)' "$OPATCH_CALLS" >/dev/null

# Backup waiver (policy require_backup=false): the apply plan seals
# recovery.waived with no manifest, and create-rollback must still derive a
# rollback plan from its succeeded final_validate evidence.
jq '.recovery.require_backup = false | .recovery.max_backup_age_minutes = 0' "$TMP/policy.json" >"$TMP/waived-policy.json"
WAIVED_POLICY_SHA=$(sha256sum "$TMP/waived-policy.json" | awk '{print $1}')
jq --arg policy "$WAIVED_POLICY_SHA" '.evidence.policy_sha256 = $policy' "$TMP/readiness.json" >"$TMP/waived-readiness.json"
plan create --plan-id standalone-waived-apply --requester patch-admin \
  --readiness "$TMP/waived-readiness.json" --reconciliation "$TMP/reconciliation.json" \
  --artifact-manifest "$TMP/artifact.json" --procedure-validation "$TMP/procedure.json" \
  --compatibility "$TMP/compatibility.json" --policy "$TMP/waived-policy.json" \
  --window-start "$WINDOW_START" --window-end "$WINDOW_END" \
  | jq -e '.recovery.waived == true and .recovery.require_backup == false and (.recovery.manifest_path | not)' >/dev/null
plan approve --plan-id standalone-waived-apply --actor dba-approver --approval-ticket TEST-WAIVED >/dev/null
plan authorize --plan-id standalone-waived-apply --actor patch-operator >/dev/null
# Independent simulated target scenario; lifecycle tests cover retained reservations.
retire_fixture_plans "$PLAN_STATE"
plan dispatch --plan-id standalone-waived-apply --actor patch-operator >/dev/null
for expected_stage in precheck apply validate datapatch final_validate; do
  TASK_ID=$(plan next --plan-id standalone-waived-apply | jq -r '.task_id')
  execute standalone-waived-apply "$TASK_ID" >"$TMP/waived-$expected_stage-result.json"
  jq -e '.status == "succeeded" and .recovery.manifest_sha256 == null and .recovery.record_sha256 == null' "$TMP/waived-$expected_stage-result.json" >/dev/null
done
plan status --plan-id standalone-waived-apply | jq -e '.state == "succeeded"' >/dev/null
[ -f "$PATCH_STATE" ] && [ "$(cat "$SQLPATCH_ACTION_STATE")" = APPLY ]

plan create-rollback --plan-id standalone-waived-rollback --requester rollback-admin \
  --source-plan-id standalone-waived-apply --window-start "$WINDOW_START" --window-end "$WINDOW_END" \
  | jq -e '.intent == "patch_rollback" and .recovery.waived == true and (.recovery.manifest_path | not)' >/dev/null
plan approve --plan-id standalone-waived-rollback --actor rollback-approver --approval-ticket TEST-WAIVED-ROLLBACK >/dev/null
plan authorize --plan-id standalone-waived-rollback --actor rollback-operator >/dev/null
# Independent simulated target scenario; lifecycle tests cover retained reservations.
retire_fixture_plans "$PLAN_STATE"
plan dispatch --plan-id standalone-waived-rollback --actor rollback-operator >/dev/null
for expected_stage in rollback_precheck rollback_binary rollback_binary_validate rollback_datapatch rollback_final_validate; do
  TASK_JSON=$(plan next --plan-id standalone-waived-rollback)
  TASK_ID=$(jq -r '.task_id' <<<"$TASK_JSON")
  [ "$(jq -r '.stage' <<<"$TASK_JSON")" = "$expected_stage" ]
  execute_rollback standalone-waived-rollback "$TASK_ID" >"$TMP/waived-$expected_stage-result.json"
  jq -e '.intent == "patch_rollback" and .status == "succeeded"' "$TMP/waived-$expected_stage-result.json" >/dev/null
done
plan status --plan-id standalone-waived-rollback | jq -e '.state == "succeeded"' >/dev/null
[ ! -f "$PATCH_STATE" ] && [ "$(cat "$SQLPATCH_ACTION_STATE")" = ROLLBACK ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]

grep -q '^startup:closed$' "$FD_CALLS"
grep -q '^listener-start:closed$' "$FD_CALLS"
grep -q '^listener-stop:open$' "$FD_CALLS"
grep -q '^opatch:open$' "$FD_CALLS"
grep -q '^datapatch:open$' "$FD_CALLS"
grep -q '^recompile:open$' "$FD_CALLS"
if grep -Eq -- '-noqi|-apply|-rollback|-force' "$DATAPATCH_CALLS"; then
  echo 'datapatch was invoked with a bypass or forced patch selection' >&2; exit 1
fi
SQL_EVIDENCE=$(find "$EXECUTION_STATE/plans/standalone-success/tasks" -name evidence.json -exec grep -l 'sql-runtime.sha256' {} \;)
[ -n "$SQL_EVIDENCE" ]
jq -e '(.artifacts | length) > 0 and all(.artifacts[]; .sha256 | test("^[a-f0-9]{64}$")) and all(.artifacts[]; .path | contains("heartbeat") | not)' "$SQL_EVIDENCE" >/dev/null
SQL_MANIFEST=$(jq -r '.artifacts[] | select(.path | endswith("/sql-runtime.sha256")) | .path' "$SQL_EVIDENCE")
(cd "$(dirname "$SQL_MANIFEST")" && sha256sum -c "$SQL_MANIFEST") >/dev/null
SQL_TASK=$(jq -r '.task_id' "$SQL_EVIDENCE")
SQL_CUSTODY="$PLAN_STATE/plans/standalone-success/evidence/$SQL_TASK/custody.json"
jq -e 'any(.files[]; .role == "artifact" and (.source_path | endswith("/utlrp0.log"))) and any(.files[]; .role == "artifact" and (.source_path | endswith("/local-inventory-before.xml")))' "$SQL_CUSTODY" >/dev/null

# Each independent SQL failure pauses the existing managed task after exactly
# one native call, preserving partial evidence without automatic retries.
for flag in opposite-latest datapatch-log-error utlrp-log-error utlrp-exit-error invalid-component invalid-objects; do
  create_plan "sql-failure-$flag"
  for _stage in precheck apply validate; do
    task=$(plan next --plan-id "sql-failure-$flag" | jq -r '.task_id')
    execute "sql-failure-$flag" "$task" >/dev/null
  done
  touch "$SQL_FLAGS/$flag"
  task=$(plan next --plan-id "sql-failure-$flag" | jq -r '.task_id')
  before=$(grep -c '^-verbose' "$DATAPATCH_CALLS" || true)
  if execute "sql-failure-$flag" "$task" >/dev/null 2>&1; then echo "accepted SQL failure: $flag" >&2; exit 1; fi
  after=$(grep -c '^-verbose' "$DATAPATCH_CALLS" || true)
  [ "$after" -eq "$((before + 1))" ]
  plan status --plan-id "sql-failure-$flag" | jq -e '.state == "paused"' >/dev/null
  jq -e '.status == "failed" and .postcondition.status == "failed" and (.artifacts | length > 0)' "$EXECUTION_STATE/plans/sql-failure-$flag/tasks/$task/evidence.json" >/dev/null
  [ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
  rm -f "$SQL_FLAGS/$flag" "$PATCH_STATE" "$DATAPATCH_STATE" "$SQLPATCH_ACTION_STATE"
done

# Installed tools without the documented local-inventory option receive only
# -verbose; a native failure is never retried with another option.
touch "$SQL_FLAGS/no-local-inventory"
create_plan sql-native-inventory
for _stage in precheck apply validate datapatch final_validate; do
  task=$(plan next --plan-id sql-native-inventory | jq -r '.task_id')
  execute sql-native-inventory "$task" >/dev/null
done
[ "$(grep '^-verbose' "$DATAPATCH_CALLS" | tail -n1)" = -verbose ]
rm -f "$SQL_FLAGS/no-local-inventory" "$PATCH_STATE" "$DATAPATCH_STATE" "$SQLPATCH_ACTION_STATE"

# SQL success cannot hide changed extjob metadata or missing README proof.
create_plan final-extjob-metadata
for _stage in precheck apply validate datapatch; do
  task=$(plan next --plan-id final-extjob-metadata | jq -r '.task_id')
  execute final-extjob-metadata "$task" >/dev/null
done
task=$(plan next --plan-id final-extjob-metadata | jq -r '.task_id')
native_sql_calls=$(grep -c '^-verbose' "$DATAPATCH_CALLS")
binary_apply_calls=$(grep -c '^apply ' "$OPATCH_CALLS")
chmod 0700 "$TEST_HOME/bin/extjob"
if execute final-extjob-metadata "$task" >/dev/null 2>&1; then echo 'final validation ignored extjob permissions' >&2; exit 1; fi
final_evidence="$EXECUTION_STATE/plans/final-extjob-metadata/tasks/$task/evidence.json"
jq -e '.stage == "final_validate" and .status == "failed" and .postcondition.status == "failed" and .outcome_class == "binary_state_known"' "$final_evidence" >/dev/null
final_stderr=$(jq -r '.logs.stderr.path' "$final_evidence")
grep -q 'extjob ownership/mode differs from README:.*mode=0700' "$final_stderr"
[ "$(sha256sum "$final_stderr" | awk '{print $1}')" = "$(jq -r '.logs.stderr.sha256' "$final_evidence")" ]
first_final_sha=$(sha256sum "$final_evidence" | awk '{print $1}')
plan status --plan-id final-extjob-metadata | jq -e '.state == "paused"' >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ "$(cat "$SQLPATCH_ACTION_STATE")" = APPLY ] && [ -f "$PATCH_STATE" ]
chmod 0750 "$TEST_HOME/bin/extjob"
cp "$PATCH_DIR/README.txt" "$TMP/final-readme-original"
for missing_proof in changed missing; do
  plan retry-task --plan-id final-extjob-metadata --task-id "$task" --actor patch-operator >/dev/null
  if [ "$missing_proof" = changed ]; then printf 'tamper\n' >>"$PATCH_DIR/README.txt"; else rm "$PATCH_DIR/README.txt"; fi
  if execute final-extjob-metadata "$task" >/dev/null 2>&1; then echo "final validation accepted $missing_proof README" >&2; exit 1; fi
  cp "$TMP/final-readme-original" "$PATCH_DIR/README.txt"
done
plan retry-task --plan-id final-extjob-metadata --task-id "$task" --actor patch-operator >/dev/null
execute final-extjob-metadata "$task" >"$TMP/final-extjob-correct-result.json"
jq -e '.stage == "final_validate" and .status == "succeeded" and .retry_count == 3' "$TMP/final-extjob-correct-result.json" >/dev/null
plan status --plan-id final-extjob-metadata | jq -e '.state == "succeeded"' >/dev/null
[ "$(sha256sum "$final_evidence" | awk '{print $1}')" = "$first_final_sha" ]
[ "$(grep -c '^-verbose' "$DATAPATCH_CALLS")" = "$native_sql_calls" ]
[ "$(grep -c '^apply ' "$OPATCH_CALLS")" = "$binary_apply_calls" ]
rm -f "$PATCH_STATE" "$DATAPATCH_STATE" "$SQLPATCH_ACTION_STATE"

# A historical APPLY success must not hide a newer ROLLBACK at the final gate.
create_plan sql-final-latest-action
for _stage in precheck apply validate datapatch; do
  task=$(plan next --plan-id sql-final-latest-action | jq -r '.task_id')
  execute sql-final-latest-action "$task" >/dev/null
done
printf 'ROLLBACK\n' >"$SQLPATCH_ACTION_STATE"
task=$(plan next --plan-id sql-final-latest-action | jq -r '.task_id')
if execute sql-final-latest-action "$task" >/dev/null 2>&1; then echo 'final validation accepted historical APPLY success' >&2; exit 1; fi
plan status --plan-id sql-final-latest-action | jq -e '.state == "paused"' >/dev/null
jq -e '.stage == "final_validate" and .status == "failed"' "$EXECUTION_STATE/plans/sql-final-latest-action/tasks/$task/evidence.json" >/dev/null
rm -f "$PATCH_STATE" "$DATAPATCH_STATE" "$SQLPATCH_ACTION_STATE"

# Malformed freshly generated inventory stops before any SQL mutation.
create_plan sql-malformed-inventory
for _stage in precheck apply validate; do
  task=$(plan next --plan-id sql-malformed-inventory | jq -r '.task_id')
  execute sql-malformed-inventory "$task" >/dev/null
done
touch "$SQL_FLAGS/malformed-xml"
task=$(plan next --plan-id sql-malformed-inventory | jq -r '.task_id')
before=$(grep -c '^-verbose' "$DATAPATCH_CALLS" || true)
if execute sql-malformed-inventory "$task" >/dev/null 2>&1; then echo 'malformed XML accepted' >&2; exit 1; fi
after=$(grep -c '^-verbose' "$DATAPATCH_CALLS" || true)
[ "$before" -eq "$after" ]
rm -f "$SQL_FLAGS/malformed-xml" "$PATCH_STATE" "$DATAPATCH_STATE" "$SQLPATCH_ACTION_STATE"

# The pre-binary restoration path also launches services without the lock FD.
create_plan startup-recovery-fd
task=$(plan next --plan-id startup-recovery-fd | jq -r '.task_id')
execute startup-recovery-fd "$task" >/dev/null
touch "$SQL_FLAGS/fail-listener-stop"
task=$(plan next --plan-id startup-recovery-fd | jq -r '.task_id')
if execute startup-recovery-fd "$task" >/dev/null 2>&1; then echo 'listener stop failure ignored' >&2; exit 1; fi
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ]
[ ! -f "$PATCH_STATE" ]
rm -f "$SQL_FLAGS/fail-listener-stop"

# Verification must refuse a symlink and must never change its target.
create_plan extjob-symlink
task=$(plan next --plan-id extjob-symlink | jq -r '.task_id')
execute extjob-symlink "$task" >/dev/null
mv "$TEST_HOME/bin/extjob" "$TMP/extjob-regular"
printf 'untouched\n' >"$TMP/outside-extjob"
chmod 0600 "$TMP/outside-extjob"
ln -s "$TMP/outside-extjob" "$TEST_HOME/bin/extjob"
task=$(plan next --plan-id extjob-symlink | jq -r '.task_id')
if execute extjob-symlink "$task" >/dev/null 2>&1; then echo 'symlink extjob accepted' >&2; exit 1; fi
jq -e '.status == "failed" and .outcome_class == "no_mutation"' "$EXECUTION_STATE/plans/extjob-symlink/tasks/$task/evidence.json" >/dev/null
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]
mode=$(stat -c '%a' "$TMP/outside-extjob" 2>/dev/null || stat -f '%Lp' "$TMP/outside-extjob")
[ "$mode" = 600 ]
rm "$TEST_HOME/bin/extjob"
mv "$TMP/extjob-regular" "$TEST_HOME/bin/extjob"
rm -f "$PATCH_STATE" "$DATAPATCH_STATE" "$SQLPATCH_ACTION_STATE"
printf 'up\n' >"$DATABASE_STATE"
printf 'up\n' >"$LISTENER_STATE"

# Inadequate extjob permissions block before outage; the worker must not turn
# existing Oracle-writable content into a root setuid executable.
chmod 0700 "$TEST_HOME/bin/extjob"
create_plan extjob-needs-privilege-repair
task=$(plan next --plan-id extjob-needs-privilege-repair | jq -r '.task_id')
if execute extjob-needs-privilege-repair "$task" >/dev/null 2>&1; then echo 'extjob privilege repair was accepted without provenance' >&2; exit 1; fi
mode=$(stat -c '%a' "$TEST_HOME/bin/extjob" 2>/dev/null || stat -f '%Lp' "$TEST_HOME/bin/extjob")
[ "$mode" = 700 ]
[ "$(cat "$DATABASE_STATE")" = up ] && [ "$(cat "$LISTENER_STATE")" = up ] && [ ! -f "$PATCH_STATE" ]

# If OPatch resets permissions, preserve the known binary result and stopped
# service state. No privileged repair or automatic startup may conceal it.
chmod 0750 "$TEST_HOME/bin/extjob"
create_plan extjob-reset-after-apply
task=$(plan next --plan-id extjob-reset-after-apply | jq -r '.task_id')
execute extjob-reset-after-apply "$task" >/dev/null
touch "$SQL_FLAGS/extjob-reset-by-opatch"
task=$(plan next --plan-id extjob-reset-after-apply | jq -r '.task_id')
if execute extjob-reset-after-apply "$task" >/dev/null 2>&1; then echo 'post-OPatch extjob permissions were ignored' >&2; exit 1; fi
jq -e '.status == "failed" and .outcome_class == "database_down"' "$EXECUTION_STATE/plans/extjob-reset-after-apply/tasks/$task/evidence.json" >/dev/null
[ -f "$EXECUTION_STATE/plans/extjob-reset-after-apply/tasks/$task/binary-state-known" ]
[ -f "$PATCH_STATE" ] && [ "$(cat "$DATABASE_STATE")" = down ] && [ "$(cat "$LISTENER_STATE")" = down ]
mode=$(stat -c '%a' "$TEST_HOME/bin/extjob" 2>/dev/null || stat -f '%Lp' "$TEST_HOME/bin/extjob")
[ "$mode" = 700 ]

[ -z "${FLOCK_PROBE:-}" ] || [ -s "$FLOCK_PROBE" ]
printf '%s\n' 'standalone database patch executor test passed'
