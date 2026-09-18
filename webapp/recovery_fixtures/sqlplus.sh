#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
if { : >&6; } 2>/dev/null; then request_lock=open; else request_lock=closed; fi
printf 'request-lock:sqlplus:%s\n' "$request_lock" >>"$OPU_TEST_RUNTIME/order.log"
state_file="$OPU_TEST_RUNTIME/database.state"
printf 'sql:%s\n' "$(printf '%s' "$input" | tr '\n' ' ')" >>"$OPU_TEST_RUNTIME/order.log"
case "$input" in
  *OPU_RECOVERY_PREP_PROBE*)
    state=$(cat "$state_file")
    if [ "$state" = OPEN ]; then
      printf 'ORCL|ORCL|PRIMARY|READ WRITE|NOARCHIVELOG|OPEN|12345|%s/spfileORCL.ora|1024|%s\n' "$OPU_TEST_RUNTIME" "${OPU_TEST_CDB-NO}"
    else
      printf 'ORCL|ORCL|PRIMARY|MOUNTED|NOARCHIVELOG|MOUNTED|12345|%s/spfileORCL.ora|1024|%s\n' "$OPU_TEST_RUNTIME" "${OPU_TEST_CDB-NO}"
    fi
    ;;
  *OPU_RECOVERY_PREP_CAPACITY*)
    printf 'META|19.0.0|0|12345|1|1024|512\nFILE|1|1024|1024|LOCAL|512|AVAILABLE|ONLINE|ONLINE|%s/oradata/system01.dbf|1\n' "$OPU_TEST_RUNTIME"
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
