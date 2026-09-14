#!/usr/bin/env bash
# Lab release signing stub (S13). Not a substitute for production provenance.
set -euo pipefail
# Do not add macOS AppleDouble sidecar entries to portable Oracle media.
export COPYFILE_DISABLE=1
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
DIST=${1:-"$ROOT/dist"}
mkdir -p "$DIST"

(
  cd "$ROOT"
  tar -czf "$DIST/opu-source.tgz" \
    --exclude .git --exclude .venv --exclude webapp/var --exclude dist \
    --exclude __pycache__ --exclude "*.pyc" \
    bin lib operations webapp contracts scripts docs tests Makefile README.md
)

# Inventory the captured archive so source edits cannot desynchronize its SBOM.
"$ROOT/scripts/generate_sbom.sh" "$DIST/sbom.json" "$DIST/opu-source.tgz"

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
