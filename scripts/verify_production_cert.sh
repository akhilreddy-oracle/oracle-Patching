#!/usr/bin/env bash
# Verify an S13 production certification marker (lab/prod gate helper).
# Exit 0 when valid; 2 when mode is on but cert fails; 0 when mode is off.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
. "$ROOT/lib/opu/common.sh"

MODE=${OPU_PRODUCTION_MODE:-0}
CERT=${OPU_PRODUCTION_CERT_FILE:-/etc/oracle-patching/production.cert}
REQUIRE_CHECKLIST=${OPU_PRODUCTION_REQUIRE_CHECKLIST:-0}

case "$MODE" in
  1|true|yes|on) ;;
  *)
    printf '%s\n' 'production mode off; certification check skipped'
    exit 0
    ;;
esac

[ -f "$CERT" ] && [ ! -L "$CERT" ] || {
  opu_error "certification marker missing: $CERT"
  exit 2
}
grep -Fq 'OPU_PRODUCTION_CERTIFIED=1' "$CERT" || {
  opu_error "certification marker invalid: $CERT"
  exit 2
}

if [ "$REQUIRE_CHECKLIST" = 1 ]; then
  for key in OPU_SBOM_VERIFIED=1 OPU_RELEASE_SIGNED=1 OPU_THREAT_MODEL_SIGNED=1; do
    grep -Fq "$key" "$CERT" || {
      opu_error "checklist incomplete: missing $key in $CERT"
      exit 2
    }
  done
fi

printf '%s\n' "production certification verified: $CERT"
exit 0
