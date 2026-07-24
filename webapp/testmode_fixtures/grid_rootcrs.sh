#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_GRID_NODE_TEST_HOST:-node1}
case "${1:-}" in
  -prepatch)
    [ ! -f "$OPU_TEST_GRID_RUNTIME/fail-prepatch" ] || { printf 'simulated prepatch failure\n' >&2; exit 72; }
    printf 'quiesced\n' >"$OPU_TEST_GRID_RUNTIME/prepatch-$node_name.state"
    ;;
  -postpatch)
    printf 'online\n' >"$OPU_TEST_GRID_RUNTIME/postpatch-$node_name.state"
    ;;
  *) exit 64 ;;
esac
