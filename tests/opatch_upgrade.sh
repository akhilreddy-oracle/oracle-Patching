#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-opatch-upgrade.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
mkdir -p "$TMP/home/OPatch" "$TMP/home/jdk/bin" "$TMP/stage/OPatch" "$TMP/backups"
cp /usr/bin/true "$TMP/home/jdk/bin/java"
chmod 700 "$TMP/home/jdk/bin/java"
make_opatch() { local path=$1 version=$2; printf '#!/bin/bash\nprintf "OPatch Version: %%s\\n" "%s"\n' "$version" >"$path"; chmod 700 "$path"; }
make_opatch "$TMP/home/OPatch/opatch" 12.2.0.1.17
make_opatch "$TMP/stage/OPatch/opatch" 12.2.0.1.51
printf 'approved-opatch-zip\n' >"$TMP/p6880880.zip"
sha=$(sha256sum "$TMP/p6880880.zip" | awk '{print $1}')
tool() { OPU_OPATCH_UPGRADE_STATE_DIR="$TMP/state" OPU_OPATCH_UPGRADE_TEST_ALLOW_NONROOT=1 "$ROOT/bin/opu-opatch-upgrade" "$@"; }
if tool create --request-id bad --requester requester --oracle-home "$TMP/home" --stage-dir "$TMP/stage" --artifact "$TMP/p6880880.zip" --artifact-sha256 "${sha%?}0" --required-version 12.2.0.1.51 --backup-root "$TMP/backups" >/dev/null 2>&1; then echo 'bad artifact hash was accepted' >&2; exit 1; fi
tool create --request-id opatch-001 --requester requester --oracle-home "$TMP/home" --stage-dir "$TMP/stage" --artifact "$TMP/p6880880.zip" --artifact-sha256 "$sha" --required-version 12.2.0.1.51 --backup-root "$TMP/backups" >"$TMP/request.json"
tool analyze --request-id opatch-001 >"$TMP/analyze.json"
jq -e '.status == "passed" and .target.current_opatch_version == "12.2.0.1.17" and .artifact.staged_opatch_version == "12.2.0.1.51"' "$TMP/analyze.json" >/dev/null
tool approve --request-id opatch-001 --actor approver --approval-ticket CHG-001 >/dev/null
tool apply --request-id opatch-001 --actor executor >"$TMP/applied.json"
jq -e '.state == "applied" and .apply.backup_path' "$TMP/applied.json" >/dev/null
"$TMP/home/OPatch/opatch" version | grep -q 12.2.0.1.51
cp "$TMP/backups/opatch-001-OPatch-before/opatch" "$TMP/approved-backup-opatch"
printf '\nchanged after approval\n' >>"$TMP/backups/opatch-001-OPatch-before/opatch"
if tool rollback --request-id opatch-001 --actor executor >"$TMP/changed-backup.out" 2>"$TMP/changed-backup.err"; then
  echo 'changed backup was accepted for rollback' >&2; exit 1
fi
grep -q 'sealed digest' "$TMP/changed-backup.err"
"$TMP/home/OPatch/opatch" version | grep -q 12.2.0.1.51
[ ! -e "$TMP/backups/opatch-001-OPatch-before.replaced" ]
cp "$TMP/approved-backup-opatch" "$TMP/backups/opatch-001-OPatch-before/opatch"
tool rollback --request-id opatch-001 --actor executor >"$TMP/rollback.json"
jq -e '.state == "rolled_back"' "$TMP/rollback.json" >/dev/null
"$TMP/home/OPatch/opatch" version | grep -q 12.2.0.1.17
# Exercise the actual restoration helper with a partial replacement.
# shellcheck disable=SC1090
. <(sed -n '/^restore_original_opatch() {/,/^}/p' "$ROOT/bin/opu-opatch-upgrade")
# Called by the production helper extracted above.
# shellcheck disable=SC2329,SC2317 # Called indirectly by the extracted native helpers.
as_owner() { shift; "$@"; }
mkdir -p "$TMP/partial-home/OPatch" "$TMP/original"
printf 'partial copy' >"$TMP/partial-home/OPatch/partial"
printf 'original bytes' >"$TMP/original/opatch"
restore_original_opatch fixture "$TMP/partial-home" "$TMP/original"
[ "$(cat "$TMP/partial-home/OPatch/opatch")" = 'original bytes' ]
[ ! -d "$TMP/partial-home/OPatch/OPatch" ]
[ "$(cat "$TMP/original.failed-replacement/partial")" = 'partial copy' ]
mkdir -p "$TMP/failed-home/OPatch" "$TMP/failed-original"
printf 'preserved original' >"$TMP/failed-original/opatch"
# Called by the production helper extracted above.
# shellcheck disable=SC2329
as_owner() { shift; if [ "$3" = "$TMP/failed-original" ]; then return 1; fi; "$@"; }
if restore_original_opatch fixture "$TMP/failed-home" "$TMP/failed-original"; then
  echo 'failed original restoration was reported as successful' >&2; exit 1
fi
[ "$(cat "$TMP/failed-original/opatch")" = 'preserved original' ]
printf '%s\n' 'opatch upgrade test passed'
