#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
if grep -q 'shutdown immediate' <<<"$input"; then
  [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
  printf 'down\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q '^startup;' <<<"$input"; then
  printf 'up\n' >"$OPU_TEST_DATABASE_STATE"
elif grep -q 'SQLPATCH_LATEST_ACTION=' <<<"$input"; then
  action=$(cat "$OPU_TEST_SQLPATCH_ACTION_STATE" 2>/dev/null || true)
  [ -n "$action" ] || exit 1
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
    'DATABASE_ROLE=PRIMARY' \
    'OPEN_MODE=READ WRITE' \
    'LOG_MODE=ARCHIVELOG' \
    'INVALID_OBJECTS=0'
fi
