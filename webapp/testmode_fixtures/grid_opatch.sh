#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_GRID_NODE_TEST_HOST:-node1}
patch_state="$OPU_TEST_GRID_RUNTIME/patch-$node_name.state"
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$patch_state" ] && printf '%s\n' '39034528;Grid Release Update' || true ;;
  prereq) printf '%s\n' 'Prereq "checkConflictAgainstOHWithDetail" passed.' ;;
  apply)
    [ ! -f "$OPU_TEST_GRID_RUNTIME/fail-opatch" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf 'installed\n' >"$patch_state"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    [ ! -f "$OPU_TEST_GRID_RUNTIME/fail-rollback" ] || { printf 'simulated OPatch rollback failure\n' >&2; exit 74; }
    rm -f "$patch_state"
    printf '%s\n' 'OPatch rollback succeeded.'
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
