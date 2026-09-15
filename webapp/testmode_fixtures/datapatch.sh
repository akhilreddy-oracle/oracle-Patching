#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -eq 1 ] && [ "$1" = -help ]; then
  printf '%s\n' '  -local_inventory [xml_filename]'
  exit 0
fi
[ "${1:-}" = -verbose ] || exit 64
if [ "$#" -gt 1 ]; then
  [ "$#" -eq 3 ] && [ "$2" = -local_inventory ] && [ -s "$3" ] || exit 64
  xmllint --nonet --noout "$3" || exit 65
fi
[ ! -f "$OPU_TEST_FAIL_DATAPATCH" ] || { printf 'simulated datapatch failure\n' >&2; exit 74; }
[ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ]
if [ -f "$OPU_TEST_PATCH_STATE" ]; then printf 'APPLY\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; else printf 'ROLLBACK\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; fi
printf 'complete\n' >"$OPU_TEST_DATAPATCH_STATE"
printf '%s\n' 'SQL Patching tool complete.'
