#!/usr/bin/env bash
# Verify an S13 production certification marker (lab/prod gate helper).
# Exit 0 when valid; 2 when mode is on but cert fails; 0 when mode is off.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
. "$ROOT/lib/opu/common.sh"

MODE=$(opu_boolean_value "${OPU_PRODUCTION_MODE:-0}" OPU_PRODUCTION_MODE) || exit 2
CERT=${OPU_PRODUCTION_CERT_FILE:-/etc/oracle-patching/production.cert}

case "$MODE" in
  1) ;;
  *)
    printf '%s\n' 'production mode off; certification check skipped'
    exit 0
    ;;
esac

opu_require_production_certified || exit 2

printf '%s\n' "production certification verified: $CERT"
exit 0
