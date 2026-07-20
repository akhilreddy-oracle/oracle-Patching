#!/usr/bin/env bash

set -u
set -o pipefail

TEST_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(CDPATH= cd -- "$TEST_ROOT/.." && pwd -P)
AGENT="$PROJECT_ROOT/bin/opu-agent"
FIXTURE_OL8="$TEST_ROOT/fixtures/oracle-linux-8"
FIXTURE_UNSUPPORTED="$TEST_ROOT/fixtures/not-oracle-linux"

TEST_COUNT=0
PASS_COUNT=0
FAIL_COUNT=0
CURRENT_TMP=""

new_tmp() {
    CURRENT_TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-test.XXXXXX") || exit 1
}

cleanup_tmp() {
    if [ -n "$CURRENT_TMP" ] && [ -d "$CURRENT_TMP" ]; then
        rm -rf "$CURRENT_TMP"
    fi
    CURRENT_TMP=""
}

fail() {
    printf '    %s\n' "$1" >&2
    return 1
}

assert_contains() {
    local haystack needle
    haystack=$1
    needle=$2
    printf '%s\n' "$haystack" | grep -F -- "$needle" >/dev/null 2>&1 ||
        fail "expected output to contain: $needle"
}

assert_file_contains() {
    local file needle
    file=$1
    needle=$2
    grep -F -- "$needle" "$file" >/dev/null 2>&1 ||
        fail "expected $file to contain: $needle"
}

assert_valid_json() {
    local file
    file=$1
    jq -e . "$file" >/dev/null 2>&1 || fail "invalid JSON in $file"
}

assert_jq() {
    local file filter description
    file=$1
    filter=$2
    description=$3
    jq -e "$filter" "$file" >/dev/null 2>&1 || fail "$description"
}

assert_discovery_contract() {
    local file
    file=$1
    assert_jq "$file" '
      .payload as $p |
      ($p.schema_version == "1.0") and
      ($p.collector.name | type == "string") and
      ($p.collector.version | test("^[1-9][0-9]*$")) and
      (["complete","partial","failed","unsupported"] | index($p.status) != null) and
      ($p.coverage | type == "array") and
      ($p.errors | type == "array") and
      ($p.observations | type == "array") and
      ($p.resources | type == "array") and
      ($p.conflicts | type == "array") and
      ($p.summary.coverage_sources == ($p.coverage | length)) and
      ($p.summary.errors == ($p.errors | length)) and
      ($p.summary.observations == ($p.observations | length)) and
      ($p.summary.resources == ($p.resources | length)) and
      ($p.summary.conflicts == ($p.conflicts | length)) and
      ([ $p.coverage[].status ] | all(
        . == "complete" or . == "partial" or . == "unavailable" or
        . == "failed" or . == "unsupported" or . == "not_applicable"
      ))
    ' "discovery payload violates the common contract"
}

copy_ol8_fixture() {
    local destination
    destination=$1
    mkdir -p "$destination"
    cp -R "$FIXTURE_OL8/." "$destination/"
}

run_agent() {
    local fixture state_dir
    fixture=$1
    state_dir=$2
    shift 2
    OPU_TEST_MODE=1 \
        OPU_TEST_KERNEL_NAME=Linux \
        OPU_TEST_KERNEL_RELEASE=5.15.0-test \
        OPU_TEST_ARCHITECTURE=x86_64 \
        OPU_FS_ROOT="$fixture" \
        OPU_STATE_DIR="$state_dir" \
        "$AGENT" "$@"
}

test_operation_registry_is_allowlisted() {
    local output
    output=$(OPU_TEST_MODE=1 "$AGENT" operations) || return
    assert_contains "$output" '"name":"host.discover"' || return
    assert_contains "$output" '"name":"oracle_homes.discover"' || return
    if printf '%s\n' "$output" | grep -F 'shell.execute' >/dev/null 2>&1; then
        fail "registry must not expose a generic shell operation"
    fi
}

test_host_discovery_and_result_envelope() {
    local state output result
    new_tmp
    state="$CURRENT_TMP/state"
    output=$(run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-host-001 \
        --idempotency-key idem-host-001) || return
    assert_contains "$output" '"status": "succeeded"' || return
    assert_contains "$output" '"id":"ol"' || return
    assert_contains "$output" '"architecture":"x86_64"' || return
    assert_contains "$output" '"machine_id_sha256"' || return
    result="$state/results/idem-host-001/result.json"
    [ -f "$state/results/idem-host-001/complete" ] || fail "completion marker was not published" || return
    assert_valid_json "$result" || return
    assert_discovery_contract "$result" || return
    assert_jq "$result" '
      .payload.status == "complete" and
      .payload.summary.observations == 1 and
      .payload.summary.errors == 0 and
      .payload.observations[0].kind == "host" and
      .payload.observations[0].confidence == "high" and
      (.payload.observations[0].evidence_sha256 | test("^[a-f0-9]{64}$"))
    ' "host discovery did not meet the complete-source contract"
}

test_oracle_home_discovery_preserves_sources() {
    local state output result
    new_tmp
    state="$CURRENT_TMP/state"
    output=$(run_agent "$FIXTURE_OL8" "$state" run \
        --operation oracle_homes.discover \
        --operation-version 1 \
        --task-id task-homes-001 \
        --idempotency-key idem-homes-001) || return
    assert_contains "$output" '"status": "succeeded"' || return
    assert_contains "$output" '"source":"oratab"' || return
    assert_contains "$output" '"source":"central_inventory"' || return
    assert_contains "$output" '"oracle_sid":"ORCL"' || return
    assert_contains "$output" '"inventory_removed":true' || return
    assert_contains "$output" '"source":"runtime_process"' || return
    if printf '%s\n' "$output" | grep -F '/../' >/dev/null 2>&1; then
        fail "non-normalized Oracle home escaped into discovery output"
        return
    fi
    result="$state/results/idem-homes-001/result.json"
    assert_valid_json "$result" || return
    assert_discovery_contract "$result" || return
    assert_jq "$result" '
      .payload.status == "complete" and
      .payload.summary.observations == 7 and
      .payload.summary.resources == 4 and
      .payload.summary.conflicts == 0 and
      ([.payload.coverage[] | select(.status != "complete")] | length == 0) and
      ([.payload.observations[] |
        select((.source_location | type) != "string" or
               (.evidence_sha256 | test("^[a-f0-9]{64}$") | not))] | length == 0) and
      ([.payload.resources[] |
        select(.identity.canonical_path == "/u01/app/oracle/product/19c/dbhome_1") |
        .attributes.sources] | first == ["central_inventory","oratab","runtime_process"]) and
      ([.payload.resources[] |
        select(.identity.canonical_path == "/u01/app/oracle/product/19c/runtime_home") |
        .attributes.runtime_active] | first == true) and
      ([.payload.observations[] |
        select(.attributes.inventory_name == "OraDB19Home2")] | length == 1)
    ' "Oracle-home discovery did not reconcile all healthy sources"
}

test_repeated_delivery_returns_identical_result() {
    local state first second
    new_tmp
    state="$CURRENT_TMP/state"
    first=$(run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-repeat-001 \
        --idempotency-key idem-repeat-001) || return
    second=$(run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-repeat-001 \
        --idempotency-key idem-repeat-001) || return
    [ "$first" = "$second" ] || fail "redelivery did not return the original result" || return
    [ "$(find "$state/runs" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "1" ] ||
        fail "redelivery unexpectedly created another run"
}

test_completed_result_tampering_is_detected() {
    local state rc
    new_tmp
    state="$CURRENT_TMP/state"
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-tamper-001 \
        --idempotency-key idem-tamper-001 >/dev/null || return
    printf 'tampered\n' >>"$state/results/idem-tamper-001/result.json"
    set +e
    run_agent "$FIXTURE_OL8" "$state" result \
        --idempotency-key idem-tamper-001 >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "tampered completed evidence was accepted"
}

test_completed_metadata_tampering_is_detected() {
    local state rc
    new_tmp
    state="$CURRENT_TMP/state"
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-meta-tamper-001 \
        --idempotency-key idem-meta-tamper-001 >/dev/null || return
    printf '99\n' >"$state/results/idem-meta-tamper-001/exit_code"
    set +e
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-meta-tamper-001 \
        --idempotency-key idem-meta-tamper-001 >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "tampered completion metadata was accepted"
}

test_concurrent_redelivery_executes_once() {
    local state output_dir pid_file index pid first_output
    new_tmp
    state="$CURRENT_TMP/state"
    output_dir="$CURRENT_TMP/outputs"
    pid_file="$CURRENT_TMP/pids"
    mkdir "$output_dir"
    : >"$pid_file"

    index=1
    while [ "$index" -le 10 ]; do
        (
            run_agent "$FIXTURE_OL8" "$state" run \
                --operation host.discover \
                --operation-version 1 \
                --task-id task-concurrent-001 \
                --idempotency-key idem-concurrent-001 \
                >"$output_dir/$index.json"
        ) &
        printf '%s\n' "$!" >>"$pid_file"
        index=$((index + 1))
    done

    while IFS= read -r pid; do
        wait "$pid" || fail "a concurrent redelivery failed" || return
    done <"$pid_file"

    first_output="$output_dir/1.json"
    index=2
    while [ "$index" -le 10 ]; do
        cmp "$first_output" "$output_dir/$index.json" >/dev/null 2>&1 ||
            fail "concurrent redelivery returned inconsistent results" || return
        index=$((index + 1))
    done
    [ "$(find "$state/runs" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "1" ] ||
        fail "concurrent redelivery executed the operation more than once"
}

test_task_id_cannot_change_idempotency_key() {
    local state rc
    new_tmp
    state="$CURRENT_TMP/state"
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-binding-001 \
        --idempotency-key idem-binding-001 >/dev/null || return
    set +e
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-binding-001 \
        --idempotency-key idem-binding-changed >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "task binding conflict was accepted"
}

test_idempotency_key_cannot_change_request() {
    local state rc
    new_tmp
    state="$CURRENT_TMP/state"
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-fingerprint-001 \
        --idempotency-key idem-fingerprint-001 >/dev/null || return
    set +e
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation oracle_homes.discover \
        --operation-version 1 \
        --task-id task-fingerprint-002 \
        --idempotency-key idem-fingerprint-001 >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "idempotency key accepted a changed request"
}

test_unsupported_os_fails_closed_with_evidence() {
    local state output rc result
    new_tmp
    state="$CURRENT_TMP/state"
    set +e
    output=$(run_agent "$FIXTURE_UNSUPPORTED" "$state" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-os-001 \
        --idempotency-key idem-os-001)
    rc=$?
    set -e
    [ "$rc" -eq 20 ] || fail "unsupported OS returned unexpected code: $rc" || return
    assert_contains "$output" '"status": "failed"' || return
    assert_contains "$output" '"code":"DISCOVERY_UNSUPPORTED_OS"' || return
    result="$state/results/idem-os-001/result.json"
    assert_valid_json "$result"
}

test_operation_name_cannot_inject_a_command() {
    local state sentinel rc
    new_tmp
    state="$CURRENT_TMP/state"
    sentinel="$CURRENT_TMP/should-not-exist"
    set +e
    run_agent "$FIXTURE_OL8" "$state" run \
        --operation "host.discover;touch${sentinel}" \
        --operation-version 1 \
        --task-id task-injection-001 \
        --idempotency-key idem-injection-001 >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "injected operation name was accepted" || return
    [ ! -e "$sentinel" ] || fail "operation name injection created a file"
}

test_unsafe_runtime_values_are_rejected() {
    local rc
    set +e
    OPU_TEST_MODE=1 OPU_LOCK_WAIT_SECONDS='1+1' "$AGENT" operations >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "non-integer lock timeout was accepted" || return

    set +e
    OPU_TEST_MODE=1 OPU_STATE_DIR=/ "$AGENT" run \
        --operation host.discover \
        --operation-version 1 \
        --task-id task-root-state-001 \
        --idempotency-key idem-root-state-001 >/dev/null 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "filesystem root was accepted as the state directory"
}

run_test() {
    local name function_name rc
    name=$1
    function_name=$2
    TEST_COUNT=$((TEST_COUNT + 1))
    printf 'TEST %02d  %s\n' "$TEST_COUNT" "$name"
    set +e
    "$function_name"
    rc=$?
    set -e
    cleanup_tmp
    if [ "$rc" -eq 0 ]; then
        PASS_COUNT=$((PASS_COUNT + 1))
        printf '  PASS\n'
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        printf '  FAIL\n'
    fi
}

trap cleanup_tmp EXIT HUP INT TERM
set -e

run_test "operation registry is a fixed allowlist" test_operation_registry_is_allowlisted
run_test "host discovery emits a valid result envelope" test_host_discovery_and_result_envelope
run_test "Oracle-home discovery retains source provenance" test_oracle_home_discovery_preserves_sources
run_test "redelivery returns the original completed result" test_repeated_delivery_returns_identical_result
run_test "completed result tampering is detected" test_completed_result_tampering_is_detected
run_test "completed metadata tampering is detected" test_completed_metadata_tampering_is_detected
run_test "concurrent redelivery executes once" test_concurrent_redelivery_executes_once
run_test "task IDs cannot be rebound to another idempotency key" test_task_id_cannot_change_idempotency_key
run_test "idempotency keys cannot be reused for changed requests" test_idempotency_key_cannot_change_request
run_test "unsupported operating systems fail closed" test_unsupported_os_fails_closed_with_evidence
run_test "operation names cannot inject shell commands" test_operation_name_cannot_inject_a_command
run_test "unsafe runtime and state values are rejected" test_unsafe_runtime_values_are_rejected

printf '\nRESULT: %d passed, %d failed, %d total\n' "$PASS_COUNT" "$FAIL_COUNT" "$TEST_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
