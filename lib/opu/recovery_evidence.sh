#!/usr/bin/env bash
# Shared selected-backup integrity and freshness checks for readiness and plans.
# Callers provide common.sh, require_file(), epoch(), and iso_from_epoch().
verify_filesystem_recovery_current() {
  local recovery_file=$1 policy_file=$2 reference_epoch=$3 mode observed observed_epoch maximum_age reserve
  mode=$(jq -r '.recovery | if has("storage_mode") then .storage_mode else "fra" end' "$policy_file")
  case "$mode" in fra) return 0;; filesystem) ;; *) opu_error 'invalid recovery storage mode'; return 65;; esac
  reserve=$(jq -r '.recovery.minimum_filesystem_free_bytes // empty' "$policy_file")
  [[ "$reserve" =~ ^[0-9]+$ ]] || { opu_error 'filesystem recovery requires a nonnegative free-space reserve'; return 65; }
  jq -e --argjson reserve "$reserve" '
    .backup as $backup |
    ($backup.preparation.manifest.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    ($backup.preparation.manifest.record_sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    ($backup.preparation.central_inventory_archive.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    ($backup.preparation.oraInst_loc.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    ($backup.coverage.datafiles_current | type == "number" and . > 0) and
    $backup.coverage.base_datafiles == $backup.coverage.datafiles_current and
    $backup.coverage.controlfile_records > 0 and $backup.coverage.spfile_records > 0 and
    $backup.coverage.outside_root_pieces == 0 and $backup.coverage.unavailable_pieces == 0 and
    ($backup.checksum_manifest.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    (.verification.checksum_log.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    (.verification.oracle_home_archive_log.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    (.verification.rman_syntax.log.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    (.verification.rman_syntax.exit_code.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    (.verification.rman_log.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    (.verification.rman_log.exit_code.sha256 | type == "string" and test("^[a-f0-9]{64}$")) and
    .verification.rman_syntax.exit_code.value == 0 and .verification.rman_log.exit_code.value == 0 and
    $backup.storage.type == "filesystem" and $backup.storage.path == $backup.root and
    ($backup.storage.device_id | type == "string" and test("^[0-9]+$")) and
    ($backup.storage.available_bytes | type == "number" and . >= $reserve and floor == .) and
    ($backup.storage.total_bytes | type == "number" and . >= $backup.storage.available_bytes and floor == .) and
    ($backup.storage.observed_at | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"))
  ' "$recovery_file" >/dev/null || { opu_error 'filesystem recovery needs complete validated backup coverage, preparation archives and sufficient measured free space'; return 65; }
  observed=$(jq -r '.backup.storage.observed_at' "$recovery_file")
  observed_epoch=$(epoch "$observed") || { opu_error 'filesystem recovery observation time is invalid'; return 65; }
  maximum_age=$(jq -r '.maximum_snapshot_age_seconds' "$policy_file")
  [[ "$maximum_age" =~ ^[0-9]+$ ]] && [ "$maximum_age" -gt 0 ] || return 65
  [ "$observed_epoch" -le "$reference_epoch" ] && [ $((reference_epoch - observed_epoch)) -le "$maximum_age" ] || {
    opu_error 'filesystem recovery capacity evidence is stale or future-dated'; return 65;
  }
}

verify_database_recovery_current() {
  local recovery_file=$1 snapshot_binding_file=$2 policy_file=$3 reference_epoch=${4:-$(date -u +%s)}
  local expected_file_sha=${5:-} expected_record_sha=${6:-} actual_file_sha record_sha canonical
  local observed_at completed_at observed_epoch completed_epoch recorded_age calculated_age max_age max_age_seconds
  local source_snapshot source_snapshot_sha
  require_file "$recovery_file" recovery-evidence || return
  actual_file_sha=$(opu_hash_file "$recovery_file")
  [ -z "$expected_file_sha" ] || [ "$actual_file_sha" = "$expected_file_sha" ] || {
    opu_error 'recovery evidence changed after plan creation'; return 74;
  }
  jq -e '
    .schema_version == "1.0" and .collector.name == "oracle.recovery.evidence" and .collector.version == "1" and
    .status == "passed" and
    (.source_snapshot.path | type == "string" and startswith("/")) and
    (.source_snapshot.sha256 | test("^[a-f0-9]{64}$")) and
    (.backup.selected_recovery_set.observed_at | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
    (.backup.selected_recovery_set.oldest_datafile_backup_completed_at | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and
    (.backup.selected_recovery_set.age_seconds_at_collection | type == "number" and . >= 0 and floor == .) and
    (.backup.selected_recovery_set.restore_piece_handles | type == "array" and length > 0 and length == (unique | length)) and
    (.backup.selected_recovery_set.datafile_backup_sets | type == "array" and length > 0)
  ' "$recovery_file" >/dev/null || { opu_error 'recovery evidence lacks an exact selected-backup freshness contract'; return 65; }
  record_sha=$(jq -r '.record_sha256 // empty' "$recovery_file")
  [[ "$record_sha" =~ ^[a-f0-9]{64}$ ]] || { opu_error 'recovery evidence has no valid record digest'; return 74; }
  canonical=$(jq -cS 'del(.record_sha256)' "$recovery_file") || return 74
  [ "$record_sha" = "$(opu_hash_string "$canonical")" ] || { opu_error 'recovery evidence record integrity verification failed'; return 74; }
  [ -z "$expected_record_sha" ] || [ "$record_sha" = "$expected_record_sha" ] || {
    opu_error 'recovery evidence record differs from the plan'; return 74;
  }
  jq -e --slurpfile binding "$snapshot_binding_file" '
    .source_snapshot as $source |
    any(($binding[0].snapshot_evidence // [])[];
      .path == $source.path and .sha256 == $source.sha256)
  ' "$recovery_file" >/dev/null || { opu_error 'recovery evidence is not bound to a reconciled readiness snapshot'; return 74; }
  source_snapshot=$(jq -r '.source_snapshot.path' "$recovery_file")
  source_snapshot_sha=$(jq -r '.source_snapshot.sha256' "$recovery_file")
  require_file "$source_snapshot" recovery-source-snapshot || return
  [ "$(opu_hash_file "$source_snapshot")" = "$source_snapshot_sha" ] || {
    opu_error 'recovery source snapshot changed'; return 74;
  }
  observed_at=$(jq -r '.backup.selected_recovery_set.observed_at' "$recovery_file")
  completed_at=$(jq -r '.backup.selected_recovery_set.oldest_datafile_backup_completed_at' "$recovery_file")
  recorded_age=$(jq -r '.backup.selected_recovery_set.age_seconds_at_collection' "$recovery_file")
  observed_epoch=$(epoch "$observed_at") || { opu_error 'recovery backup observation time is invalid'; return 65; }
  completed_epoch=$(epoch "$completed_at") || { opu_error 'selected recovery backup completion time is invalid'; return 65; }
  [ "$completed_epoch" -le "$observed_epoch" ] && [ "$observed_epoch" -le "$reference_epoch" ] || {
    opu_error 'selected recovery backup timing is future-dated'; return 65;
  }
  calculated_age=$((observed_epoch - completed_epoch))
  [ "$recorded_age" -eq "$calculated_age" ] || {
    opu_error 'selected recovery backup age does not match its sealed timestamps'; return 74;
  }
  max_age=$(jq -r '.recovery.max_backup_age_minutes // empty' "$policy_file")
  [[ "$max_age" =~ ^[0-9]+$ ]] || { opu_error 'policy has no valid selected-backup age limit'; return 65; }
  max_age_seconds=$((max_age * 60))
  RECOVERY_VALID_UNTIL_EPOCH=$((completed_epoch + max_age_seconds))
  [ "$reference_epoch" -le "$RECOVERY_VALID_UNTIL_EPOCH" ] || {
    opu_error 'the exact selected recovery backup is older than policy permits'; return 65;
  }
  verify_filesystem_recovery_current "$recovery_file" "$policy_file" "$reference_epoch" || return
  # shellcheck disable=SC2034 # Read by bin/opu-patch-plan after this function returns.
  RECOVERY_COMPLETED_AT=$completed_at
  # shellcheck disable=SC2034 # Read by bin/opu-patch-plan after this function returns.
  RECOVERY_VALID_UNTIL=$(iso_from_epoch "$RECOVERY_VALID_UNTIL_EPOCH") || return 65
}
