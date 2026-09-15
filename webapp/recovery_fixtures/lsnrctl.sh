#!/usr/bin/env bash
set -euo pipefail
if { : >&6; } 2>/dev/null; then request_lock=open; else request_lock=closed; fi
printf 'request-lock:listener:%s\n' "$request_lock" >>"$OPU_TEST_RUNTIME/order.log"
printf 'listener:%s\n' "$*" >>"$OPU_TEST_RUNTIME/order.log"
case "${1:-}" in
  services|status)
    printf '%s\n' \
      'Services Summary...' \
      'Service "ORCL" has 1 instance(s).' \
      '  Instance "ORCL", status READY, has 1 handler(s) for this service...'
    ;;
esac
printf 'The command completed successfully\n'
