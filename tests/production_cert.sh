#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-prod-cert.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# Mode off → skip
OPU_PRODUCTION_MODE=0 "$ROOT/scripts/verify_production_cert.sh" >/dev/null

# Mode on without cert → fail
set +e
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/missing.cert" \
  "$ROOT/scripts/verify_production_cert.sh" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 2 ]

printf 'OPU_PRODUCTION_CERTIFIED=1\n' >"$TMP/ok.cert"
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/ok.cert" \
  "$ROOT/scripts/verify_production_cert.sh" >/dev/null

# Checklist required
set +e
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_REQUIRE_CHECKLIST=1 OPU_PRODUCTION_CERT_FILE="$TMP/ok.cert" \
  "$ROOT/scripts/verify_production_cert.sh" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 2 ]

cat >"$TMP/full.cert" <<'EOF'
OPU_PRODUCTION_CERTIFIED=1
OPU_SBOM_VERIFIED=1
OPU_RELEASE_SIGNED=1
OPU_THREAT_MODEL_SIGNED=1
EOF
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_REQUIRE_CHECKLIST=1 OPU_PRODUCTION_CERT_FILE="$TMP/full.cert" \
  "$ROOT/scripts/verify_production_cert.sh" >/dev/null

printf '%s\n' 'production certification verify test passed'
