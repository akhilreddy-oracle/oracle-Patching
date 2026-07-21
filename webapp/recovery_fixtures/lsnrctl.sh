#!/usr/bin/env bash
set -euo pipefail
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
