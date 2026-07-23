#!/usr/bin/env bash
# Lab release signing stub (S13). Not a substitute for production provenance.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
DIST=${1:-"$ROOT/dist"}
mkdir -p "$DIST"

"$ROOT/scripts/generate_sbom.sh" "$DIST/sbom.json"
(
  cd "$ROOT"
  tar -czf "$DIST/opu-source.tgz" \
    --exclude .git --exclude .venv --exclude webapp/var --exclude dist \
    bin lib webapp contracts scripts docs tests Makefile README.md
)

(
  cd "$DIST"
  shasum -a 256 sbom.json opu-source.tgz > SHA256SUMS
)

KEY=${OPU_RELEASE_SIGNING_KEY:-"$DIST/lab-signing.key"}
PUB=${OPU_RELEASE_SIGNING_PUB:-"$DIST/lab-signing.pub"}
if [ ! -f "$KEY" ]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$KEY" 2>/dev/null
  openssl pkey -in "$KEY" -pubout -out "$PUB" 2>/dev/null
  chmod 600 "$KEY"
fi

openssl dgst -sha256 -sign "$KEY" -out "$DIST/SHA256SUMS.sig" "$DIST/SHA256SUMS"
printf 'signed release artifacts in %s\n' "$DIST"
