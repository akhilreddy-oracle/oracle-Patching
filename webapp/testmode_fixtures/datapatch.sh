#!/usr/bin/env bash
set -euo pipefail
[ ! -f "$OPU_TEST_FAIL_DATAPATCH" ] || { printf 'simulated datapatch failure\n' >&2; exit 74; }
[ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ]
if [ -f "$OPU_TEST_PATCH_STATE" ]; then printf 'APPLY\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; else printf 'ROLLBACK\n' >"$OPU_TEST_SQLPATCH_ACTION_STATE"; fi
printf 'complete\n' >"$OPU_TEST_DATAPATCH_STATE"
printf '%s\n' 'SQL Patching tool complete.'
