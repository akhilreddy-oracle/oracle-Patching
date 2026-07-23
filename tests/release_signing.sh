#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-release.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
chmod +x "$ROOT/scripts/generate_sbom.sh" "$ROOT/scripts/sign_release.sh" "$ROOT/scripts/verify_release.sh"
"$ROOT/scripts/sign_release.sh" "$TMP"
"$ROOT/scripts/verify_release.sh" "$TMP" "$TMP/lab-signing.pub"
jq -e '.bomFormat == "CycloneDX" and (.components | length) > 10' "$TMP/sbom.json" >/dev/null
printf '%s\n' 'release SBOM/sign/verify stub test passed'
