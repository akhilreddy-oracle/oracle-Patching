#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-artifact.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
mkdir -p "$TMP/artifact/etc/config"
printf '<patch patchID="12345678">Database Release Update</patch>\n' >"$TMP/artifact/etc/config/actions.xml"
printf '<patch patchID="12345678"><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n' >"$TMP/artifact/etc/config/inventory.xml"
printf 'README for patch 12345678\n' >"$TMP/artifact/README.txt"
"$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/artifact" --output "$TMP/manifest.json" >/dev/null
jq -e '.artifact.status == "ready_for_catalog" and .artifact.patch_ids == ["12345678"] and .artifact.platforms == [{id:"226",name:"Linux x86-64",source:"etc/config/inventory.xml",source_sha256:.artifact.platforms[0].source_sha256}] and (.artifact.platforms[0].source_sha256 | test("^[a-f0-9]{64}$")) and (.artifact.sha256 | test("^[a-f0-9]{64}$")) and (.artifact.metadata_files | length == 2) and (.artifact.readme_files | length == 1)' "$TMP/manifest.json" >/dev/null
mkdir -p "$TMP/modern/etc/config"
printf '<oneoff_inventory><patch_id number="87654321"/><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></oneoff_inventory>\n' >"$TMP/modern/etc/config/inventory.xml"
"$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/modern" --output "$TMP/modern.json" >/dev/null
jq -e '.artifact.status == "ready_for_catalog" and .artifact.patch_ids == ["87654321"] and .artifact.platforms[0].id == "226"' "$TMP/modern.json" >/dev/null
mkdir -p "$TMP/no-platform/etc/config"
printf '<patch patchID="99999999"><os_platforms/></patch>\n' >"$TMP/no-platform/etc/config/inventory.xml"
if "$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/no-platform" >/dev/null 2>&1; then echo 'artifact without authoritative platform was accepted' >&2; exit 1; fi
mkdir "$TMP/empty"
if "$ROOT/bin/opu-artifact-inspect" --artifact "$TMP/empty" >/dev/null 2>&1; then echo 'empty artifact was accepted' >&2; exit 1; fi
if "$ROOT/bin/opu-artifact-inspect" --artifact relative >/dev/null 2>&1; then echo 'relative artifact path was accepted' >&2; exit 1; fi
printf '%s\n' 'artifact inspection test passed'
