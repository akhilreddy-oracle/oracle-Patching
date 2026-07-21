#!/usr/bin/env bash

# Shared primitives for the Oracle Patching Utility agent.
# This file is sourced only from code shipped with the agent.

# shellcheck disable=SC2034 # read by bin/opu-agent, which sources this file
OPU_AGENT_VERSION="0.1.0"
# shellcheck disable=SC2034 # read by bin/opu-agent, which sources this file
OPU_RESULT_SCHEMA_VERSION="1.0"
OPU_MINIMUM_BASH_MAJOR=4
OPU_MINIMUM_BASH_MINOR=4

opu_runtime_init() {
    umask 077
    export LC_ALL=C
    export LANG=C
    export PATH="/usr/sbin:/usr/bin:/sbin:/bin"

    OPU_TEST_MODE=${OPU_TEST_MODE:-0}
    OPU_FS_ROOT=${OPU_FS_ROOT:-/}
    OPU_STATE_DIR=${OPU_STATE_DIR:-/var/lib/oracle-patching-agent}
    OPU_LOCK_WAIT_SECONDS=${OPU_LOCK_WAIT_SECONDS:-30}

    case "$OPU_TEST_MODE" in
        0 | 1) ;;
        *)
            opu_error "OPU_TEST_MODE must be either 0 or 1"
            return 64
            ;;
    esac
    case "$OPU_LOCK_WAIT_SECONDS" in
        "" | *[!0-9]*)
            opu_error "OPU_LOCK_WAIT_SECONDS must be an integer"
            return 64
            ;;
    esac
    if [ "${#OPU_LOCK_WAIT_SECONDS}" -gt 4 ] ||
        [ "$OPU_LOCK_WAIT_SECONDS" -lt 1 ] ||
        [ "$OPU_LOCK_WAIT_SECONDS" -gt 3600 ]; then
        opu_error "OPU_LOCK_WAIT_SECONDS must be between 1 and 3600"
        return 64
    fi

    export OPU_TEST_MODE OPU_FS_ROOT OPU_STATE_DIR OPU_LOCK_WAIT_SECONDS
}

opu_error() {
    printf 'opu-agent: %s\n' "$*" >&2
}

opu_now_utc() {
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

opu_json_escape() {
    local value
    value=${1-}
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\b'/\\b}
    value=${value//$'\f'/\\f}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '%s' "$value"
}

opu_json_string() {
    printf '"%s"' "$(opu_json_escape "${1-}")"
}

opu_json_nullable_string() {
    if [ -n "${1-}" ]; then
        opu_json_string "$1"
    else
        printf 'null'
    fi
}

opu_validate_identifier() {
    local value label
    value=${1-}
    label=${2:-identifier}

    if [ -z "$value" ] || [ "${#value}" -gt 128 ]; then
        opu_error "$label must contain between 1 and 128 characters"
        return 64
    fi

    case "$value" in
        *[!A-Za-z0-9._:-]* | [!A-Za-z0-9]*)
            opu_error "$label contains unsupported characters"
            return 64
            ;;
    esac
}

opu_validate_operation_name() {
    local value
    value=${1-}
    if [ -z "$value" ] || [ "${#value}" -gt 96 ]; then
        opu_error "operation name must contain between 1 and 96 characters"
        return 64
    fi
    case "$value" in
        *[!a-z0-9._-]* | [!a-z]*)
            opu_error "operation name contains unsupported characters"
            return 64
            ;;
    esac
}

opu_validate_absolute_path() {
    local value label
    value=${1-}
    label=${2:-path}
    case "$value" in
        /*) ;;
        *)
            opu_error "$label must be an absolute path"
            return 64
            ;;
    esac
    case "$value" in
        *$'\n'* | *$'\r'* | *$'\t'*)
            opu_error "$label contains a control character"
            return 64
            ;;
    esac
    case "$value" in
        */../* | */.. | */./* | */.)
            opu_error "$label must be normalized and cannot contain dot path segments"
            return 64
            ;;
    esac
}

opu_safe_field() {
    if printf '%s' "${1-}" | grep '[[:cntrl:]]' >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

opu_is_normalized_logical_path() {
    local value
    value=${1-}
    case "$value" in
        /*) ;;
        *) return 1 ;;
    esac
    case "$value" in
        */../* | */.. | */./* | */.) return 1 ;;
    esac
    opu_safe_field "$value"
}

opu_fs_path() {
    local logical_path
    logical_path=$1
    case "$logical_path" in
        /*) ;;
        *) return 64 ;;
    esac

    if [ "$OPU_FS_ROOT" = "/" ]; then
        printf '%s' "$logical_path"
    else
        printf '%s%s' "${OPU_FS_ROOT%/}" "$logical_path"
    fi
}

opu_hash_file() {
    local file
    file=$1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file" | awk '{print $1}'
    else
        opu_error "no SHA-256 implementation is available"
        return 69
    fi
}

opu_hash_string() {
    local value
    value=${1-}
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$value" | sha256sum | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$value" | shasum -a 256 | awk '{print $1}'
    else
        opu_error "no SHA-256 implementation is available"
        return 69
    fi
}

opu_check_runtime() {
    local major minor
    major=${BASH_VERSINFO[0]}
    minor=${BASH_VERSINFO[1]}

    if [ "$OPU_TEST_MODE" = "1" ]; then
        return 0
    fi

    if [ "$major" -lt "$OPU_MINIMUM_BASH_MAJOR" ] || {
        [ "$major" -eq "$OPU_MINIMUM_BASH_MAJOR" ] &&
            [ "$minor" -lt "$OPU_MINIMUM_BASH_MINOR" ];
    }; then
        opu_error "Bash ${OPU_MINIMUM_BASH_MAJOR}.${OPU_MINIMUM_BASH_MINOR} or newer is required"
        return 69
    fi
}

opu_operation_error() {
    # shellcheck disable=SC2034 # read by bin/opu-agent after calling this function
    OPU_ERROR_CODE=$1
    # shellcheck disable=SC2034 # read by bin/opu-agent after calling this function
    OPU_ERROR_MESSAGE=$2
    # shellcheck disable=SC2034 # read by bin/opu-agent after calling this function
    OPU_ERROR_RETRYABLE=${3:-false}
}
