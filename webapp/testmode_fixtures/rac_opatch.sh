#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_RAC_DATABASE_TEST_HOSTNAME:-node1}
patch_state="$OPU_TEST_RAC_RUNTIME/patch-$node_name.state"
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$patch_state" ] && printf '%s\n' '39034528;Database Release Update' || true ;;
  prereq) printf '%s\n' 'Prereq "checkConflictAgainstOHWithDetail" passed.' ;;
  apply)
    [ ! -f "$OPU_TEST_RAC_RUNTIME/fail-opatch" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf 'installed\n' >"$patch_state"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    [ ! -f "$OPU_TEST_RAC_RUNTIME/fail-rollback" ] || { printf 'simulated OPatch rollback failure\n' >&2; exit 74; }
    rm -f "$patch_state"
    printf '%s\n' "$node_name" >>"$OPU_TEST_RAC_RUNTIME/rollback-order.log"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  lsinventory)
    output_path=""
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = -xml ]; then output_path=${2:-}; break; fi
      shift
    done
    [ -n "$output_path" ] || exit 64
    printf '<InventoryInstance/>\n' >"$output_path"
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
