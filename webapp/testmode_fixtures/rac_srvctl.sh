#!/usr/bin/env bash
set -euo pipefail
command_name=${1:-}; object_name=${2:-}
shift 2 || true
node_name=""; instance_name=""; database_name=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -db) database_name=${2:-}; shift 2 ;;
    -node) node_name=${2:-}; shift 2 ;;
    -instance) instance_name=${2:-}; shift 2 ;;
    *) shift ;;
  esac
done
state_for_node() { printf '%s/%s.state' "$OPU_TEST_RAC_RUNTIME" "$1"; }
node_for_instance() { case "$1" in ORCL1) printf 'node1\n' ;; ORCL2) printf 'node2\n' ;; *) exit 64 ;; esac; }
instance_for_node() { case "$1" in node1) printf 'ORCL1\n' ;; node2) printf 'ORCL2\n' ;; *) exit 64 ;; esac; }
case "$command_name:$object_name" in
  config:database)
    if [ -n "$database_name" ]; then printf 'Oracle home: %s\n' "$OPU_TEST_RAC_DB_PATH"; else printf 'ORCL\n'; fi
    ;;
  status:instance)
    instance_name=$(instance_for_node "$node_name")
    if [ "$(cat "$(state_for_node "$node_name")")" = running ]; then
      printf 'Instance %s is running on node %s\n' "$instance_name" "$node_name"
    else
      printf 'Instance %s is not running on node %s\n' "$instance_name" "$node_name"
    fi
    ;;
  status:service)
    :
    ;;
  relocate:service)
    :
    ;;
  stop:instance)
    node_name=$(node_for_instance "$instance_name")
    printf 'stopped\n' >"$(state_for_node "$node_name")"
    ;;
  start:instance)
    node_name=$(node_for_instance "$instance_name")
    printf 'running\n' >"$(state_for_node "$node_name")"
    ;;
  *)
    printf 'unsupported fake srvctl operation: %s %s\n' "$command_name" "$object_name" >&2
    exit 64
    ;;
esac
