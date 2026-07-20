#!/usr/bin/env bash

# Shared discovery document contract. Collector payloads intentionally exclude
# timestamps; the enclosing task result records time while normalized discovery
# evidence remains byte-stable for replay and reconciliation tests.

opu_discovery_append_coverage() {
    local destination source status reason_code evidence_path evidence_sha256
    destination=$1
    source=$2
    status=$3
    reason_code=${4-}
    evidence_path=${5-}
    evidence_sha256=${6-}

    case "$status" in
        complete | partial | unavailable | failed | unsupported | not_applicable) ;;
        *)
            opu_error "invalid discovery coverage status: $status"
            return 64
            ;;
    esac

    {
        printf '{"source":%s,' "$(opu_json_string "$source")"
        printf '"status":%s,' "$(opu_json_string "$status")"
        printf '"reason_code":%s,' "$(opu_json_nullable_string "$reason_code")"
        printf '"evidence_path":%s,' "$(opu_json_nullable_string "$evidence_path")"
        printf '"evidence_sha256":%s}\n' "$(opu_json_nullable_string "$evidence_sha256")"
    } >>"$destination"
}

opu_discovery_append_error() {
    local destination source phase code severity message retryable
    destination=$1
    source=$2
    phase=$3
    code=$4
    severity=$5
    message=$6
    retryable=${7:-false}

    case "$severity" in
        warning | blocker) ;;
        *)
            opu_error "invalid discovery error severity: $severity"
            return 64
            ;;
    esac
    case "$retryable" in
        true | false) ;;
        *)
            opu_error "invalid discovery retryable value: $retryable"
            return 64
            ;;
    esac

    {
        printf '{"source":%s,' "$(opu_json_string "$source")"
        printf '"phase":%s,' "$(opu_json_string "$phase")"
        printf '"code":%s,' "$(opu_json_string "$code")"
        printf '"severity":%s,' "$(opu_json_string "$severity")"
        printf '"message":%s,' "$(opu_json_string "$message")"
        printf '"retryable":%s}\n' "$retryable"
    } >>"$destination"
}

opu_jsonl_count() {
    local file count
    file=$1
    if [ ! -s "$file" ]; then
        printf '0'
        return 0
    fi
    count=$(wc -l <"$file" | tr -d ' ')
    printf '%s' "$count"
}

opu_jsonl_print_array() {
    local file indent first line
    file=$1
    indent=${2:-4}
    first=1
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$first" -eq 0 ]; then
            printf ',\n'
        fi
        printf '%*s%s' "$indent" '' "$line"
        first=0
    done <"$file"
    if [ "$first" -eq 0 ]; then
        printf '\n'
    fi
}

opu_text_print_json_array() {
    local file first value
    file=$1
    first=1
    while IFS= read -r value || [ -n "$value" ]; do
        [ -n "$value" ] || continue
        if [ "$first" -eq 0 ]; then
            printf ','
        fi
        opu_json_string "$value"
        first=0
    done <"$file"
}

opu_discovery_write_payload() {
    local destination collector version status coverage errors observations resources conflicts
    local coverage_count error_count observation_count resource_count conflict_count
    destination=$1
    collector=$2
    version=$3
    status=$4
    coverage=$5
    errors=$6
    observations=$7
    resources=$8
    conflicts=$9

    case "$status" in
        complete | partial | failed | unsupported) ;;
        *)
            opu_error "invalid discovery result status: $status"
            return 64
            ;;
    esac

    coverage_count=$(opu_jsonl_count "$coverage") || return
    error_count=$(opu_jsonl_count "$errors") || return
    observation_count=$(opu_jsonl_count "$observations") || return
    resource_count=$(opu_jsonl_count "$resources") || return
    conflict_count=$(opu_jsonl_count "$conflicts") || return

    {
        printf '{\n'
        printf '  "schema_version": "1.0",\n'
        printf '  "collector": {'
        printf '"name":%s,' "$(opu_json_string "$collector")"
        printf '"version":%s},\n' "$(opu_json_string "$version")"
        printf '  "status": %s,\n' "$(opu_json_string "$status")"
        printf '  "coverage": [\n'
        opu_jsonl_print_array "$coverage" 4
        printf '  ],\n'
        printf '  "errors": [\n'
        opu_jsonl_print_array "$errors" 4
        printf '  ],\n'
        printf '  "observations": [\n'
        opu_jsonl_print_array "$observations" 4
        printf '  ],\n'
        printf '  "resources": [\n'
        opu_jsonl_print_array "$resources" 4
        printf '  ],\n'
        printf '  "conflicts": [\n'
        opu_jsonl_print_array "$conflicts" 4
        printf '  ],\n'
        printf '  "summary": {'
        printf '"coverage_sources":%s,' "$coverage_count"
        printf '"errors":%s,' "$error_count"
        printf '"observations":%s,' "$observation_count"
        printf '"resources":%s,' "$resource_count"
        printf '"conflicts":%s' "$conflict_count"
        printf '}\n'
        printf '}\n'
    } >"$destination"
}

