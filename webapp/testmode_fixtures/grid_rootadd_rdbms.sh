#!/usr/bin/env bash
set -euo pipefail
node_name=${OPU_GRID_NODE_TEST_HOST:-node1}
printf 'complete\n' >"$OPU_TEST_GRID_RUNTIME/rootadd-$node_name.state"
