#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-recovery.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

oracle_home="$TMP/oracle-home"
backup_root="$TMP/backup/ORCL/run-001"
mkdir -p "$oracle_home/bin" "$backup_root/rman" "$backup_root/oracle-home"

cat >"$oracle_home/bin/sqlplus" <<'EOF'
#!/usr/bin/env bash
input=$(cat)
if printf '%s' "$input" | grep -q OPU_RECOVERY_COVERAGE; then
  printf '%s\n' "${OPU_TEST_COVERAGE:-1|1|1|1|1|0|0|2026-07-17T00:00:00Z|120}"
else
  printf '%s\n' 11 12
fi
EOF
cat >"$oracle_home/bin/rman" <<'EOF'
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
if grep -Eiq '^[[:space:]]*whenever([[:space:]]|$)' "$command_file"; then
  printf '%s\n' \
    'RMAN-00569: =============== ERROR MESSAGE STACK FOLLOWS ===============' \
    'RMAN-00558: error encountered while parsing input commands' \
    'RMAN-01008: the bad identifier was: whenever' >&2
  exit 1
fi
if [ "$check_syntax" -eq 1 ]; then
  printf 'The cmdfile has no syntax errors\n'
  exit 0
fi
printf '%s\n' \
  'channel ORA_DISK_1: validation complete, elapsed time: 00:00:01' \
  'channel ORA_DISK_1: validation complete, elapsed time: 00:00:01' \
  "channel ORA_DISK_1: reading from backup piece ${OPU_TEST_RMAN_PIECE:-${OPU_TEST_BACKUP_ROOT:?}/rman/piece-01}" \
  'Finished restore at 17-JUL-26'
EOF
chmod 750 "$oracle_home/bin/sqlplus" "$oracle_home/bin/rman"

printf 'database-backup\n' >"$backup_root/rman/piece-01"
mkdir "$TMP/archive-source"
printf 'oracle-home-backup\n' >"$TMP/archive-source/file"
tar -czf "$backup_root/oracle-home/dbhome.tar.gz" -C "$TMP/archive-source" .
sha256sum \
  "$backup_root/rman/piece-01" \
  "$backup_root/oracle-home/dbhome.tar.gz" >"$backup_root/SHA256SUMS"

owner=$(id -un)
jq -n --arg oracle_home "$oracle_home" --arg owner "$owner" \
  '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},host:{name:"testhost"},oracle_homes:[{path:$oracle_home,owner:$owner}],databases:[{db_unique_name:"ORCL",oracle_home:$oracle_home,runtime:{status:"complete",instance:"ORCL"}}]}' \
  >"$TMP/snapshot.json"

tool() {
  OPU_RECOVERY_TEST_ALLOW_NONROOT=1 OPU_TEST_BACKUP_ROOT="$backup_root" \
    OPU_TEST_COVERAGE="${OPU_TEST_COVERAGE:-1|1|1|1|1|0|0|2026-07-17T00:00:00Z|120}" \
    "$ROOT/bin/opu-recovery-evidence-collect" "$@"
}

tool \
  --snapshot "$TMP/snapshot.json" \
  --database ORCL \
  --backup-root "$backup_root" \
  --output "$TMP/recovery.json" >"$TMP/result.json"

jq -e --arg backup_root "$backup_root" --arg oracle_home "$oracle_home" '
  .status == "passed" and
  .target.database_unique_name == "ORCL" and
  .target.oracle_home == $oracle_home and
  .backup.root == $backup_root and
  .backup.rman_backup_set_keys == [11,12] and
  .backup.selected_recovery_set.oldest_datafile_backup_completed_at == "2026-07-16T23:58:00Z" and
  .backup.selected_recovery_set.age_seconds_at_collection == 120 and
  (.backup.selected_recovery_set.restore_piece_handles | length) == 1 and
  .backup.coverage == {datafiles_backed:1,base_datafiles:1,datafiles_current:1,controlfile_records:1,spfile_records:1,outside_root_pieces:0,unavailable_pieces:0} and
  (.backup.files | length) == 2 and
  .verification.rman_syntax.exit_code.value == 0 and
  .verification.rman_log.exit_code.value == 0 and
  (.record_sha256 | test("^[a-f0-9]{64}$"))
' "$TMP/result.json" >/dev/null

if grep -Eiq '^[[:space:]]*whenever([[:space:]]|$)' "$TMP/recovery.d/rman-validate.cmd"; then
  echo 'RMAN validate command must not contain a WHENEVER directive' >&2
  exit 1
fi
[ "$(cat "$TMP/recovery.d/rman-validate-syntax.exit-code")" = 0 ]
[ "$(cat "$TMP/recovery.d/rman-validate.exit-code")" = 0 ]

expected=$(jq -r '.record_sha256' "$TMP/result.json")
actual=$(jq -cS 'del(.record_sha256)' "$TMP/result.json" | tr -d '\n' | sha256sum | awk '{print $1}')
[ "$expected" = "$actual" ]

if OPU_TEST_COVERAGE='1|0|1|1|1|0|0' tool \
  --snapshot "$TMP/snapshot.json" \
  --database ORCL \
  --backup-root "$backup_root" \
  --output "$TMP/incomplete.json" >/dev/null 2>&1; then
  echo 'recovery set without a base for every datafile was accepted' >&2
  exit 1
fi

printf 'unlisted backup piece\n' >"$backup_root/rman/unlisted-piece"
if OPU_TEST_RMAN_PIECE="$backup_root/rman/unlisted-piece" tool \
  --snapshot "$TMP/snapshot.json" \
  --database ORCL \
  --backup-root "$backup_root" \
  --output "$TMP/unlisted.json" >/dev/null 2>&1; then
  echo 'RMAN piece missing from SHA256SUMS was accepted' >&2
  exit 1
fi

printf 'tampered\n' >>"$backup_root/rman/piece-01"
if tool \
  --snapshot "$TMP/snapshot.json" \
  --database ORCL \
  --backup-root "$backup_root" \
  --output "$TMP/tampered.json" >/dev/null 2>&1; then
  echo 'tampered backup was accepted' >&2
  exit 1
fi

printf '%s\n' 'recovery evidence test passed'
