#!/usr/bin/env bash
set -euo pipefail
case "${1:-}:${2:-}" in
  status:asm) printf 'ASM is running on node %s\n' "${OPU_GRID_NODE_TEST_HOST:-node1}" ;;
  status:listener) printf 'Listener LISTENER is running on node %s\n' "${OPU_GRID_NODE_TEST_HOST:-node1}" ;;
  *) exit 64 ;;
esac
