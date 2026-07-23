#!/usr/bin/env bash
# Verify lab release signatures produced by scripts/sign_release.sh.
set -euo pipefail
DIST=${1:-}
PUB=${2:-}
[ -n "$DIST" ] && [ -n "$PUB" ] || {
  printf 'Usage: verify_release.sh DIST_DIR PUBLIC_KEY_PEM\n' >&2
  exit 64
}
[ -f "$DIST/SHA256SUMS" ] && [ -f "$DIST/SHA256SUMS.sig" ] && [ -f "$PUB" ] || {
  printf 'missing SHA256SUMS, signature, or public key\n' >&2
  exit 66
}
openssl dgst -sha256 -verify "$PUB" -signature "$DIST/SHA256SUMS.sig" "$DIST/SHA256SUMS" >/dev/null
(
  cd "$DIST"
  shasum -a 256 -c SHA256SUMS >/dev/null
)
printf 'release verification passed for %s\n' "$DIST"
