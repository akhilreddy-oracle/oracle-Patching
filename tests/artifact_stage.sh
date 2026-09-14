#!/usr/bin/env bash
# opu-artifact-stage: fail-closed staging of complete patch media from a tar
# stream or a local zip, with metadata-only stages moved aside (never deleted)
# and complete stages protected unless --replace.
set -euo pipefail
# Do not add macOS AppleDouble sidecar entries to portable Oracle media.
export COPYFILE_DISABLE=1
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-artifact-stage.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
me=$(id -un)
TOOL="$ROOT/bin/opu-artifact-stage"

# Source host layout: complete media under /stage/12345678.
mkdir -p "$TMP/src/12345678/etc/config" "$TMP/src/12345678/files/lib"
printf '<patch patchID="12345678"><os_platforms><platform id="226" name="Linux x86-64"/></os_platforms></patch>\n' >"$TMP/src/12345678/etc/config/inventory.xml"
printf '<actions/>\n' >"$TMP/src/12345678/etc/config/actions.xml"
printf 'payload\n' >"$TMP/src/12345678/files/lib/libpatch.so"
printf 'readme\n' >"$TMP/src/12345678/README.txt"

# Destination host layout: metadata-only stage (the real-world blocker).
mkdir -p "$TMP/dst/12345678/etc/config"
cp "$TMP/src/12345678/etc/config/"*.xml "$TMP/dst/12345678/etc/config/"

# 1. Replicate over a tar stream; metadata-only stage is moved aside.
tar -C "$TMP/src" -cf - 12345678 | "$TOOL" --artifact "$TMP/dst/12345678" --owner "$me" --from-tar-stdin --output "$TMP/stage1.json" >/dev/null
jq -e --arg a "$TMP/dst/12345678" '.status == "staged" and .artifact == $a and .patch_id == "12345678" and .source.kind == "tar_stdin" and .files == 4 and (.previous | test("/\\.12345678\\.replaced-[0-9TZ]+$"))' "$TMP/stage1.json" >/dev/null
[ -f "$TMP/dst/12345678/files/lib/libpatch.so" ]
prev=$(jq -r '.previous' "$TMP/stage1.json"); [ -f "$prev/etc/config/inventory.xml" ] && [ ! -d "$prev/files" ]
[ -z "$(ls -d "$TMP/dst"/.opu-stage-* 2>/dev/null)" ]

# 2. An existing complete stage is protected unless --replace.
if tar -C "$TMP/src" -cf - 12345678 | "$TOOL" --artifact "$TMP/dst/12345678" --owner "$me" --from-tar-stdin >/dev/null 2>"$TMP/err2"; then
  echo 'complete media was overwritten without --replace' >&2; exit 1
fi
grep -q 'already complete' "$TMP/err2"
tar -C "$TMP/src" -cf - 12345678 | "$TOOL" --artifact "$TMP/dst/12345678" --owner "$me" --from-tar-stdin --replace --output "$TMP/stage2.json" >/dev/null
jq -e '.status == "staged"' "$TMP/stage2.json" >/dev/null

# 3. Incomplete incoming media (no files/) is rejected and nothing changes.
mkdir -p "$TMP/bad/12345678/etc/config"; cp "$TMP/src/12345678/etc/config/"*.xml "$TMP/bad/12345678/etc/config/"
before=$(find "$TMP/dst/12345678" -type f | sort | sha256sum)
if tar -C "$TMP/bad" -cf - 12345678 | "$TOOL" --artifact "$TMP/dst/12345678" --owner "$me" --from-tar-stdin --replace >/dev/null 2>"$TMP/err3"; then
  echo 'incomplete media was accepted' >&2; exit 1
fi
grep -q 'incomplete' "$TMP/err3"
[ "$before" = "$(find "$TMP/dst/12345678" -type f | sort | sha256sum)" ]

# 4. patchID must match the directory name.
mkdir -p "$TMP/wrong/99999999"; cp -R "$TMP/src/12345678/." "$TMP/wrong/99999999/"
if tar -C "$TMP/wrong" -cf - 99999999 | "$TOOL" --artifact "$TMP/dst/99999999" --owner "$me" --from-tar-stdin >/dev/null 2>"$TMP/err4"; then
  echo 'patchID mismatch was accepted' >&2; exit 1
fi
grep -q 'does not match' "$TMP/err4"; [ ! -e "$TMP/dst/99999999" ]

# 5. Zip mode (Oracle download layout: patch dir at zip root).
if command -v zip >/dev/null 2>&1; then
  (cd "$TMP/src" && zip -qr "$TMP/p12345678.zip" 12345678)
  mkdir -p "$TMP/dst2"
  "$TOOL" --artifact "$TMP/dst2/12345678" --owner "$me" --from-zip "$TMP/p12345678.zip" --output "$TMP/stage5.json" >/dev/null
  jq -e --arg z "$TMP/p12345678.zip" '.status == "staged" and .source.kind == "zip" and .source.path == $z and .previous == null' "$TMP/stage5.json" >/dev/null
  [ -f "$TMP/dst2/12345678/files/lib/libpatch.so" ]
fi

# 6. Real RU metadata uses <patch_id number="..."/> rather than a patchID attribute.
mkdir -p "$TMP/ru/12345678/etc/config" "$TMP/ru/12345678/files"
printf '<oneoff_inventory><patch_description>Database Release Update (12345678)</patch_description><patch_id number="12345678"/></oneoff_inventory>\n' >"$TMP/ru/12345678/etc/config/inventory.xml"
printf '<actions/>\n' >"$TMP/ru/12345678/etc/config/actions.xml"; printf 'p\n' >"$TMP/ru/12345678/files/x.bin"
mkdir -p "$TMP/dst3"
tar -C "$TMP/ru" -cf - 12345678 | "$TOOL" --artifact "$TMP/dst3/12345678" --owner "$me" --from-tar-stdin --output "$TMP/stage6.json" >/dev/null
jq -e '.status == "staged" and .patch_id == "12345678"' "$TMP/stage6.json" >/dev/null

# 7. Unknown owner and relative paths are rejected before touching disk.
if "$TOOL" --artifact "$TMP/dst/12345678" --owner no-such-user-xyz --from-tar-stdin </dev/null >/dev/null 2>&1; then echo 'unknown owner accepted' >&2; exit 1; fi
if "$TOOL" --artifact relative/12345678 --owner "$me" --from-tar-stdin </dev/null >/dev/null 2>&1; then echo 'relative path accepted' >&2; exit 1; fi

printf '%s\n' 'artifact stage test passed'
