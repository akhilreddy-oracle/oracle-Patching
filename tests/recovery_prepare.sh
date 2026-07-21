#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-recovery-prepare.XXXXXX")
trap 'if [ "${OPU_KEEP_TEST_TMP:-0}" = 1 ]; then printf "test_tmp=%s\\n" "$TMP" >&2; else rm -rf -- "$TMP"; fi' EXIT

TOOL="$ROOT/bin/opu-database-recovery-prepare"
STATE_ROOT="$TMP/state"
ORACLE_HOME_TARGET="$TMP/oracle/dbhome_1"
BACKUP_PARENT="$TMP/backups"
INVENTORY="$TMP/oraInventory"
RUNTIME="$TMP/runtime"
OWNER=$(id -un)

mkdir -p "$ORACLE_HOME_TARGET/bin" "$ORACLE_HOME_TARGET/OPatch" "$INVENTORY" "$BACKUP_PARENT" "$RUNTIME"
BACKUP_PARENT_CANONICAL=$(CDPATH= cd -- "$BACKUP_PARENT" && pwd -P)
printf 'OPEN\n' >"$RUNTIME/database.state"
printf 'inventory_loc=%s\ninst_group=oinstall\n' "$INVENTORY" >"$ORACLE_HOME_TARGET/oraInst.loc"
printf 'inventory content\n' >"$INVENTORY/ContentsXML"
printf 'home content\n' >"$ORACLE_HOME_TARGET/bin/oracle"

cat >"$ORACLE_HOME_TARGET/bin/sqlplus" <<'FAKE_SQLPLUS'
#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
state_file="$OPU_TEST_RUNTIME/database.state"
printf 'sql:%s\n' "$(printf '%s' "$input" | tr '\n' ' ')" >>"$OPU_TEST_RUNTIME/order.log"
case "$input" in
  *OPU_RECOVERY_PREP_PROBE*)
    state=$(cat "$state_file")
    if [ "$state" = OPEN ]; then
      printf 'ORCL|ORCL|PRIMARY|READ WRITE|NOARCHIVELOG|OPEN|12345|/tmp/spfileORCL.ora|1024\n'
    else
      printf 'ORCL|ORCL|PRIMARY|MOUNTED|NOARCHIVELOG|MOUNTED|12345|/tmp/spfileORCL.ora|1024\n'
    fi
    ;;
  *OPU_RECOVERY_COVERAGE*)
    printf '1|1|1|1|1|0|0|%s|0\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    ;;
  *OPU_RECOVERY_PREP_DATABASE_FILES*) printf '%s\n' "$OPU_TEST_RUNTIME/oradata/system01.dbf";;
  *'select distinct bs.recid'*) printf '1\n2\n3\n';;
  *'shutdown immediate;'*) printf 'DOWN\n' >"$state_file";;
  *'startup mount;'*) printf 'MOUNT\n' >"$state_file";;
  *"select dbid || '|' || open_mode"*) printf '12345|MOUNTED|NOARCHIVELOG\n';;
  *'alter database open;'*)
    if [ -e "$OPU_TEST_RUNTIME/fail-open" ]; then printf 'ORA-01034\n' >&2; exit 1; fi
    printf 'OPEN\n' >"$state_file"
    ;;
  *'startup;'*)
    if [ -e "$OPU_TEST_RUNTIME/fail-open" ]; then printf 'ORA-01034\n' >&2; exit 1; fi
    printf 'OPEN\n' >"$state_file"
    ;;
  *'alter system register;'*) :;;
esac
FAKE_SQLPLUS

cat >"$ORACLE_HOME_TARGET/bin/rman" <<'FAKE_RMAN'
#!/usr/bin/env bash
set -euo pipefail
command_file=''
check_syntax=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    CHECKSYNTAX|checksyntax) check_syntax=1; shift ;;
    CMDFILE|cmdfile)
      command_file=${2:-}
      shift 2
      ;;
    CMDFILE=*|cmdfile=*) command_file=${1#*=}; shift ;;
    @*) command_file=${1#@}; shift ;;
    *) shift ;;
  esac
done
[ -n "$command_file" ] && [ -r "$command_file" ] || {
  printf 'RMAN-00558: command file is missing or unreadable\n' >&2
  exit 1
}
input=$(cat "$command_file")
if grep -Eiq '^[[:space:]]*whenever([[:space:]]|$)' "$command_file"; then
  printf '%s\n' \
    'RMAN-00569: =============== ERROR MESSAGE STACK FOLLOWS ===============' \
    'RMAN-00558: error encountered while parsing input commands' \
    'RMAN-01008: the bad identifier was: whenever' >&2
  exit 1
fi
printf 'rman:%s:%s\n' "$check_syntax" "$(printf '%s' "$input" | tr '\n' ' ')" >>"$OPU_TEST_RUNTIME/order.log"
if [ "$check_syntax" -eq 1 ]; then
  printf 'The cmdfile has no syntax errors\n'
  exit 0
fi
if printf '%s\n' "$input" | grep -q 'validate backupset'; then
  for piece in \
    database_ORCL_1_1.bkp controlfile_ORCL_2_1.bkp spfile_ORCL_3_1.bkp; do
    printf 'channel ORA_DISK_1: reading from backup piece %s/rman/%s\n' "$OPU_TEST_SUCCESS_ROOT" "$piece"
    printf 'channel ORA_DISK_1: validation complete\n'
  done
  printf 'Finished restore at TEST\n'
  exit 0
fi
if [ -e "$OPU_TEST_RUNTIME/fail-rman" ]; then
  printf 'RMAN-03002: failure of backup command\n' >&2
  exit 42
fi
backup_root=$(printf '%s\n' "$input" | sed -nE "s#.*format '([^']+)/rman/database_.*#\1#p" | head -n1)
[ -n "$backup_root" ]
printf 'database backup\n' >"$backup_root/rman/database_ORCL_1_1.bkp"
printf 'controlfile backup\n' >"$backup_root/rman/controlfile_ORCL_2_1.bkp"
printf 'spfile backup\n' >"$backup_root/rman/spfile_ORCL_3_1.bkp"
printf 'Finished backup at TEST\n'
FAKE_RMAN

cat >"$ORACLE_HOME_TARGET/bin/lsnrctl" <<'FAKE_LSNRCTL'
#!/usr/bin/env bash
set -euo pipefail
printf 'listener:%s\n' "$*" >>"$OPU_TEST_RUNTIME/order.log"
case "${1:-}" in
  services|status)
    printf '%s\n' \
      'Services Summary...' \
      'Service "ORCL" has 1 instance(s).' \
      '  Instance "ORCL", status READY, has 1 handler(s) for this service...'
    ;;
esac
printf 'The command completed successfully\n'
FAKE_LSNRCTL
chmod 750 "$ORACLE_HOME_TARGET/bin/sqlplus" "$ORACLE_HOME_TARGET/bin/rman" "$ORACLE_HOME_TARGET/bin/lsnrctl"

cat >"$RUNTIME/fake-topology" <<'FAKE_TOPOLOGY'
#!/usr/bin/env bash
set -euo pipefail
output=''
while [ "$#" -gt 0 ]; do
  case "$1" in --output) output=${2:-}; shift 2;; --pretty) shift;; *) exit 64;; esac
done
[ -n "$output" ]
jq --arg collected "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '.collected_at=$collected' "$OPU_TEST_SNAPSHOT" >"$output"
cat "$output"
FAKE_TOPOLOGY
chmod 750 "$RUNTIME/fake-topology"

NOW=$(date -u +%s)
COLLECTED=$(date -u -r "$NOW" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$NOW" '+%Y-%m-%dT%H:%M:%SZ')
WINDOW_START=$(date -u -r "$((NOW - 60))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW - 60))" '+%Y-%m-%dT%H:%M:%SZ')
WINDOW_END=$(date -u -r "$((NOW + 1800))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$((NOW + 1800))" '+%Y-%m-%dT%H:%M:%SZ')

jq -n --arg collected "$COLLECTED" --arg oracle_home "$ORACLE_HOME_TARGET" --arg owner "$OWNER" '
  {schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},collected_at:$collected,
   host:{name:"standalone.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},
   oracle_homes:[{path:$oracle_home,owner:$owner,version:"19.0.0.0.0",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},patches:[]}],
   databases:[{db_unique_name:"ORCL",oracle_home:$oracle_home,runtime:{status:"complete",instance:"ORCL",database_role:"PRIMARY",open_mode:"READ WRITE",log_mode:"NOARCHIVELOG",instance_state:"OPEN"}}],warnings:[]}
' >"$TMP/snapshot.json"
jq -n '{schema_version:"1.0",maximum_snapshot_age_seconds:3600,recovery:{require_backup:true,max_backup_age_minutes:1440}}' >"$TMP/policy.json"

tool() {
  OPU_RECOVERY_PREP_STATE_DIR="$STATE_ROOT" \
  OPU_RECOVERY_PREP_TEST_ALLOW_NONROOT=1 \
  OPU_RECOVERY_PREP_TEST_MODE=1 \
  OPU_TEST_MODE=1 \
  OPU_RECOVERY_PREP_TEST_LISTENER=LISTENER \
  OPU_RECOVERY_PREP_TEST_TOPOLOGY_TOOL="$RUNTIME/fake-topology" \
  OPU_RECOVERY_TEST_ALLOW_NONROOT=1 \
  OPU_TEST_RUNTIME="$RUNTIME" \
  OPU_TEST_SUCCESS_ROOT="$BACKUP_PARENT_CANONICAL/recovery-ok" \
  OPU_TEST_SNAPSHOT="$TMP/snapshot.json" \
    bash "$TOOL" "$@"
}

create_request() {
  tool create --request-id "$1" --requester patch-admin \
    --snapshot "$TMP/snapshot.json" --policy "$TMP/policy.json" \
    --database ORCL --backup-parent "$BACKUP_PARENT" \
    --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
}
approve_authorize() {
  tool approve --request-id "$1" --actor dba-approver --approval-ticket "TEST-$1" >/dev/null
  tool authorize --request-id "$1" --actor patch-operator >/dev/null
}

mkdir -p "$INVENTORY/unsafe-backups"
tool create --request-id recovery-inventory-overlap --requester patch-admin \
  --snapshot "$TMP/snapshot.json" --policy "$TMP/policy.json" \
  --database ORCL --backup-parent "$INVENTORY/unsafe-backups" \
  --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
approve_authorize recovery-inventory-overlap
if tool execute --request-id recovery-inventory-overlap --actor patch-operator >/dev/null 2>&1; then
  printf '%s\n' 'Central Inventory overlap was accepted' >&2; exit 1
fi
tool status --request-id recovery-inventory-overlap | jq -e '.state == "authorized"' >/dev/null
[ "$(cat "$RUNTIME/database.state")" = OPEN ]

create_request recovery-ok
tool analyze --request-id recovery-ok | jq -e '.status == "passed"' >/dev/null
if tool approve --request-id recovery-ok --actor patch-admin --approval-ticket SELF >/dev/null 2>&1; then
  printf '%s\n' 'self-approval was accepted' >&2; exit 1
fi
approve_authorize recovery-ok
tool execute --request-id recovery-ok --actor patch-operator >"$TMP/completed.json"
jq -e '.state == "completed" and .result.preparation_manifest.record_sha256 and .result.checksum_manifest.sha256 and .result.recovery_evidence.record_sha256' "$TMP/completed.json" >/dev/null
jq -e '.execution.phase == "completed"' "$TMP/completed.json" >/dev/null
[ "$(cat "$RUNTIME/database.state")" = OPEN ]
[ ! -e "$BACKUP_PARENT/recovery-ok/INCOMPLETE" ]
[ "$(wc -l <"$BACKUP_PARENT/recovery-ok/SHA256SUMS" | tr -d ' ')" -ge 7 ]
(cd / && sha256sum -c "$BACKUP_PARENT/recovery-ok/SHA256SUMS") >/dev/null
jq -e '.collector.name == "oracle.database.recovery.preparation" and .target.dbid == "12345"' "$BACKUP_PARENT/recovery-ok/PREPARATION.json" >/dev/null
if grep -Eiq '^[[:space:]]*whenever([[:space:]]|$)' "$STATE_ROOT/recovery-ok/evidence/backup.rman"; then
  echo 'RMAN backup evidence must not contain a WHENEVER directive' >&2
  exit 1
fi
if grep -Eiq '^[[:space:]]*whenever([[:space:]]|$)' "$BACKUP_PARENT/recovery-ok/rman/backup.rman.cmd"; then
  echo 'RMAN backup command file must not contain a WHENEVER directive' >&2
  exit 1
fi
[ "$(cat "$STATE_ROOT/recovery-ok/evidence/backup-rman-syntax.exit-code")" = 0 ]
[ "$(cat "$STATE_ROOT/recovery-ok/evidence/backup-rman.exit-code")" = 0 ]
grep -q 'listener:stop LISTENER' "$RUNTIME/order.log"
grep -q 'listener:start LISTENER' "$RUNTIME/order.log"
[ "$(grep -c 'listener:services LISTENER' "$RUNTIME/order.log")" -ge 2 ]
grep -q 'incremental level 0 check logical database force' "$RUNTIME/order.log"
recovery_evidence_path=$(jq -r '.result.recovery_evidence.path' "$TMP/completed.json")
jq -e '.status == "passed" and .backup.preparation.manifest.record_sha256 and .backup.preparation.central_inventory_archive.sha256' "$recovery_evidence_path" >/dev/null

create_request recovery-fail-restored
approve_authorize recovery-fail-restored
touch "$RUNTIME/fail-rman"
if tool execute --request-id recovery-fail-restored --actor patch-operator >/dev/null 2>&1; then
  printf '%s\n' 'RMAN failure unexpectedly succeeded' >&2; exit 1
fi
rm "$RUNTIME/fail-rman"
tool status --request-id recovery-fail-restored | jq -e '.state == "failed_services_restored" and .execution.phase == "backup" and .failure.phase == "backup" and .failure.exit_code == 42 and .failure.services_restored == true' >/dev/null
[ "$(cat "$RUNTIME/database.state")" = OPEN ]
[ -e "$BACKUP_PARENT/recovery-fail-restored/INCOMPLETE" ]

create_request recovery-required
approve_authorize recovery-required
touch "$RUNTIME/fail-rman" "$RUNTIME/fail-open"
if tool execute --request-id recovery-required --actor patch-operator >/dev/null 2>&1; then
  printf '%s\n' 'unrecoverable service failure unexpectedly succeeded' >&2; exit 1
fi
rm "$RUNTIME/fail-rman" "$RUNTIME/fail-open"
tool status --request-id recovery-required | jq -e '.state == "recovery_required" and .failure.services_restored == false' >/dev/null

create_request recovery-lock
approve_authorize recovery-lock
lock_key=$(printf '%s|%s' "$ORACLE_HOME_TARGET" ORCL | sha256sum | awk '{print $1}')
mkdir -p "$STATE_ROOT/locks/$lock_key.lock"
printf 'request_id=other\npid=%s\n' "$$" >"$STATE_ROOT/locks/$lock_key.lock/owner"
if tool execute --request-id recovery-lock --actor patch-operator >/dev/null 2>&1; then
  printf '%s\n' 'concurrent database-home execution was accepted' >&2; exit 1
fi
tool status --request-id recovery-lock | jq -e '.state == "authorized"' >/dev/null
[ ! -e "$BACKUP_PARENT_CANONICAL/recovery-lock" ]
unlink "$STATE_ROOT/locks/$lock_key.lock/owner"
rmdir "$STATE_ROOT/locks/$lock_key.lock"

create_request recovery-source-tamper
jq '.requester="changed"' "$STATE_ROOT/recovery-source-tamper/request-input.json" >"$TMP/source-tamper.tmp"
mv "$TMP/source-tamper.tmp" "$STATE_ROOT/recovery-source-tamper/request-input.json"
if tool approve --request-id recovery-source-tamper --actor dba-approver --approval-ticket SOURCE-TAMPER >/dev/null 2>&1; then
  printf '%s\n' 'tampered immutable source request was accepted' >&2; exit 1
fi

cp "$TMP/snapshot.json" "$TMP/tampered-snapshot.json"
tool create --request-id recovery-tamper --requester patch-admin \
  --snapshot "$TMP/tampered-snapshot.json" --policy "$TMP/policy.json" \
  --database ORCL --backup-parent "$BACKUP_PARENT" \
  --window-start "$WINDOW_START" --window-end "$WINDOW_END" >/dev/null
jq '.host.name="changed.example"' "$TMP/tampered-snapshot.json" >"$TMP/tampered.tmp"
mv "$TMP/tampered.tmp" "$TMP/tampered-snapshot.json"
if tool approve --request-id recovery-tamper --actor dba-approver --approval-ticket TAMPER >/dev/null 2>&1; then
  printf '%s\n' 'tampered snapshot was accepted' >&2; exit 1
fi

printf '%s\n' 'database recovery preparation test passed'
