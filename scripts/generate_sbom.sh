#!/usr/bin/env bash
# Generate a CycloneDX-ish SBOM of tracked repository artifacts (lab S13 stub).
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
OUT=${1:-"$ROOT/dist/sbom.json"}
mkdir -p "$(dirname "$OUT")"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

{
  printf '['
  first=1
  # shellcheck disable=SC2162
  while IFS= read -r path; do
    [ -f "$ROOT/$path" ] || continue
    if command -v sha256sum >/dev/null 2>&1; then
      sha=$(sha256sum "$ROOT/$path" | awk '{print $1}')
    else
      sha=$(shasum -a 256 "$ROOT/$path" | awk '{print $1}')
    fi
    [ "$first" -eq 1 ] || printf ','
    first=0
    jq -cn --arg path "$path" --arg sha "$sha" \
      '{type:"file",name:$path,"bom-ref":$path,hashes:[{alg:"SHA-256",content:$sha}]}'
  done < <(cd "$ROOT" && git ls-files 'bin/*' 'lib/**' 'webapp/**/*.py' 'webapp/static/**' 'contracts/**' 'scripts/*' 'Makefile' 'README.md' 2>/dev/null)
  printf ']'
} >"$TMP"

jq -n --arg now "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --slurpfile comps "$TMP" \
  '{bomFormat:"CycloneDX",specVersion:"1.5",version:1,metadata:{timestamp:$now,component:{type:"application",name:"oracle-patching-utility",version:"0.1.0"}},components:$comps[0]}' >"$OUT"
printf 'wrote %s (%s components)\n' "$OUT" "$(jq '.components | length' "$OUT")"
