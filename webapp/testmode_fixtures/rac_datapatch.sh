#!/usr/bin/env bash
set -euo pipefail
if [ -f "$OPU_TEST_RAC_RUNTIME/patch-node1.state" ] && [ -f "$OPU_TEST_RAC_RUNTIME/patch-node2.state" ]; then
  printf 'APPLY\n' >"$OPU_TEST_RAC_RUNTIME/datapatch.state"
elif [ ! -f "$OPU_TEST_RAC_RUNTIME/patch-node1.state" ] && [ ! -f "$OPU_TEST_RAC_RUNTIME/patch-node2.state" ]; then
  printf 'ROLLBACK\n' >"$OPU_TEST_RAC_RUNTIME/datapatch.state"
else
  printf 'mixed binary inventory refused\n' >&2
  exit 65
fi
printf '%s\n' 'SQL Patching tool complete.'
