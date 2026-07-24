#!/usr/bin/env bash
set -euo pipefail
input=$(cat)
case "${ORACLE_SID:-}" in ORCL1) node_name=node1 ;; ORCL2) node_name=node2 ;; *) exit 65 ;; esac
[ "$(cat "$OPU_TEST_RAC_RUNTIME/$node_name.state")" = running ] || exit 1
if grep -q 'SQLPATCH_LATEST_ACTION=' <<<"$input"; then
  [ -f "$OPU_TEST_RAC_RUNTIME/datapatch.state" ] || exit 1
  printf 'SQLPATCH_LATEST_ACTION=%s\n' "$(cat "$OPU_TEST_RAC_RUNTIME/datapatch.state")"
  printf '%s\n' 'SQLPATCH_LATEST_STATUS=SUCCESS'
else
  printf '%s\n' \
    "INSTANCE_NAME=$ORACLE_SID" \
    'INSTANCE_STATUS=OPEN' \
    'DATABASE_UNIQUE_NAME=ORCL' \
    'DATABASE_ROLE=PRIMARY' \
    'OPEN_MODE=READ WRITE' \
    'INVALID_OBJECTS=0'
fi
