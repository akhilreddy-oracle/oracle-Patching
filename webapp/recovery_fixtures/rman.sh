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
