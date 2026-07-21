#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  start) printf 'up\n' >"$OPU_TEST_LISTENER_STATE"; printf 'listener started\n' ;;
  stop) printf 'down\n' >"$OPU_TEST_LISTENER_STATE"; printf 'listener stopped\n' ;;
  status)
    [ "$(cat "$OPU_TEST_LISTENER_STATE")" = up ] || exit 1
    [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
    printf '%s\n' 'Instance "ORCL", status READY, has 1 handler(s) for this service...'
    ;;
  *) exit 64 ;;
esac
