#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-topology.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
"$ROOT/bin/opu-topology-discover" --output "$TMP/snapshot.json" >/dev/null
jq -e '.schema_version == "1.0" and .collector.name == "oracle.topology.discover" and (.oracle_homes | type == "array") and (.databases | type == "array")' "$TMP/snapshot.json" >/dev/null
if "$ROOT/bin/opu-topology-discover" --output relative.json >/dev/null 2>&1; then echo 'relative output path was accepted' >&2; exit 1; fi
printf '%s\n' 'topology snapshot test passed'
