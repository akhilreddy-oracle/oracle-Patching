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

# Substring lookalikes must fail closed (was fail-open with grep -F).
printf 'OPU_PRODUCTION_CERTIFIED=10\n' >"$TMP/suffix.cert"
set +e
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/suffix.cert" \
  "$ROOT/scripts/verify_production_cert.sh" >/dev/null 2>&1
suffix_rc=$?
set -e
[ "$suffix_rc" -eq 2 ]

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

# Shared mutation gate and standalone verifier must interpret flags identically.
check_flags() {
  local expected=$1 mode=$2 checklist=$3 cert=$4 command rc
  shift 4
  for command in shared script; do
    rc=0
    if [ "$command" = shared ]; then
      OPU_PRODUCTION_MODE="$mode" OPU_PRODUCTION_REQUIRE_CHECKLIST="$checklist" OPU_PRODUCTION_CERT_FILE="$cert" \
        bash -c '. "$1"; opu_require_production_certified' check "$ROOT/lib/opu/common.sh" >"$TMP/flag.log" 2>&1 || rc=$?
    else
      OPU_PRODUCTION_MODE="$mode" OPU_PRODUCTION_REQUIRE_CHECKLIST="$checklist" OPU_PRODUCTION_CERT_FILE="$cert" \
        "$ROOT/scripts/verify_production_cert.sh" >"$TMP/flag.log" 2>&1 || rc=$?
    fi
    if { [ "$expected" = accept ] && [ "$rc" -ne 0 ]; } || { [ "$expected" = reject ] && [ "$rc" -eq 0 ]; }; then
      printf 'Unexpected %s gate result for mode=%q checklist=%q: %s\n' "$command" "$mode" "$checklist" "$rc" >&2
      cat "$TMP/flag.log" >&2
      exit 1
    fi
  done
}
for enabled in 1 true TRUE True yes YES on ON $' \tTrUe\r\n'; do
  check_flags reject "$enabled" 0 "$TMP/missing.cert"
  check_flags accept "$enabled" 0 "$TMP/ok.cert"
  check_flags reject 1 "$enabled" "$TMP/ok.cert"
  check_flags accept 1 "$enabled" "$TMP/full.cert"
done
for disabled in '' 0 false FALSE False no NO off OFF $' \tOfF\r\n'; do
  check_flags accept "$disabled" 0 "$TMP/missing.cert"
  check_flags accept 1 "$disabled" "$TMP/ok.cert"
done
for invalid in tru enabled 2 -1 'tr ue'; do
  check_flags reject "$invalid" 0 "$TMP/missing.cert"
  check_flags reject 1 "$invalid" "$TMP/full.cert"
done

printf '%s\n' 'production certification verify test passed'
