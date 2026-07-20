#!/usr/bin/env bash

opu_os_release_value() {
    local key file line value
    key=$1
    file=$2
    OPU_READ_VALUE=""

    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "${key}="*)
                value=${line#*=}
                case "$value" in
                    \"*\")
                        value=${value#\"}
                        value=${value%\"}
                        value=${value//\\\"/\"}
                        value=${value//\\\\/\\}
                        ;;
                    \'*\')
                        value=${value#\'}
                        value=${value%\'}
                        ;;
                esac
                OPU_READ_VALUE=$value
                return 0
                ;;
        esac
    done <"$file"
    return 1
}

opu_read_first_line() {
    local file
    file=$1
    OPU_READ_VALUE=""
    if [ -r "$file" ]; then
        IFS= read -r OPU_READ_VALUE <"$file" || true
    fi
}

opu_host_publish_discovery() {
    local output status coverage errors observations resources conflicts rc
    output=$1
    status=$2
    coverage=$3
    errors=$4
    observations=$5
    resources=$6
    conflicts=$7
    opu_discovery_write_payload \
        "$output" "host.discover" "1" "$status" \
        "$coverage" "$errors" "$observations" "$resources" "$conflicts"
    rc=$?
    rm -f "$coverage" "$errors" "$observations" "$resources" "$conflicts"
    return "$rc"
}

opu_operation_host_discover_v1() {
    local output coverage errors observations resources conflicts
    local os_release os_release_hash os_id os_name version_id pretty_name id_like
    local hostname_file hostname_value hostname_hash machine_id_file machine_id machine_id_hash
    local kernel_name kernel_release architecture kernel_hash overall_status confidence composite_hash

    output=$1
    coverage="${output}.coverage.$$"
    errors="${output}.errors.$$"
    observations="${output}.observations.$$"
    resources="${output}.resources.$$"
    conflicts="${output}.conflicts.$$"
    : >"$coverage" && : >"$errors" && : >"$observations" &&
        : >"$resources" && : >"$conflicts" || return 74

    os_release=$(opu_fs_path /etc/os-release) || return
    if [ ! -r "$os_release" ]; then
        opu_discovery_append_coverage \
            "$coverage" "os_release" "failed" "DISCOVERY_OS_RELEASE_MISSING" \
            "/etc/os-release" "" || return
        opu_discovery_append_error \
            "$errors" "os_release" "collect" "DISCOVERY_OS_RELEASE_MISSING" \
            "blocker" "The target does not expose a readable /etc/os-release file." false || return
        opu_host_publish_discovery \
            "$output" "failed" "$coverage" "$errors" "$observations" "$resources" "$conflicts" || return
        opu_operation_error \
            "DISCOVERY_OS_RELEASE_MISSING" \
            "The target does not expose a readable /etc/os-release file." false
        return 20
    fi
    os_release_hash=$(opu_hash_file "$os_release") || return

    opu_os_release_value ID "$os_release" || true
    os_id=$OPU_READ_VALUE
    opu_os_release_value NAME "$os_release" || true
    os_name=$OPU_READ_VALUE
    opu_os_release_value VERSION_ID "$os_release" || true
    version_id=$OPU_READ_VALUE
    opu_os_release_value PRETTY_NAME "$os_release" || true
    pretty_name=$OPU_READ_VALUE
    opu_os_release_value ID_LIKE "$os_release" || true
    id_like=$OPU_READ_VALUE

    if ! opu_safe_field "$os_id" || ! opu_safe_field "$os_name" ||
        ! opu_safe_field "$version_id" || ! opu_safe_field "$pretty_name" ||
        ! opu_safe_field "$id_like"; then
        opu_discovery_append_coverage \
            "$coverage" "os_release" "failed" "DISCOVERY_INVALID_OS_RELEASE" \
            "/etc/os-release" "$os_release_hash" || return
        opu_discovery_append_error \
            "$errors" "os_release" "validate" "DISCOVERY_INVALID_OS_RELEASE" \
            "blocker" "The OS release file contains unsupported control characters." false || return
        opu_host_publish_discovery \
            "$output" "failed" "$coverage" "$errors" "$observations" "$resources" "$conflicts" || return
        opu_operation_error \
            "DISCOVERY_INVALID_OS_RELEASE" \
            "The OS release file contains unsupported control characters." false
        return 20
    fi

    opu_discovery_append_coverage \
        "$coverage" "os_release" "complete" "" "/etc/os-release" "$os_release_hash" || return
    if [ "$os_id" != "ol" ]; then
        opu_discovery_append_error \
            "$errors" "platform" "detect" "DISCOVERY_UNSUPPORTED_OS" \
            "blocker" "This release supports Oracle Linux only." false || return
        opu_host_publish_discovery \
            "$output" "unsupported" "$coverage" "$errors" "$observations" "$resources" "$conflicts" || return
        opu_operation_error \
            "DISCOVERY_UNSUPPORTED_OS" \
            "This release supports Oracle Linux only; observed OS ID: ${os_id:-unknown}." false
        return 20
    fi

    overall_status="complete"
    confidence="high"

    hostname_file=$(opu_fs_path /etc/hostname) || return
    opu_read_first_line "$hostname_file"
    hostname_value=$OPU_READ_VALUE
    if ! opu_safe_field "$hostname_value"; then
        opu_discovery_append_coverage \
            "$coverage" "hostname" "failed" "DISCOVERY_INVALID_HOSTNAME" \
            "/etc/hostname" "" || return
        opu_discovery_append_error \
            "$errors" "hostname" "validate" "DISCOVERY_INVALID_HOSTNAME" \
            "warning" "The hostname source contains unsupported control characters." false || return
        hostname_value=""
        overall_status="partial"
        confidence="medium"
    elif [ -n "$hostname_value" ]; then
        hostname_hash=$(opu_hash_file "$hostname_file") || return
        opu_discovery_append_coverage \
            "$coverage" "hostname" "complete" "" "/etc/hostname" "$hostname_hash" || return
    else
        opu_discovery_append_coverage \
            "$coverage" "hostname" "unavailable" "DISCOVERY_HOSTNAME_UNAVAILABLE" \
            "/etc/hostname" "" || return
        opu_discovery_append_error \
            "$errors" "hostname" "collect" "DISCOVERY_HOSTNAME_UNAVAILABLE" \
            "warning" "No hostname was available from /etc/hostname." true || return
        overall_status="partial"
        confidence="medium"
    fi

    machine_id_file=$(opu_fs_path /etc/machine-id) || return
    opu_read_first_line "$machine_id_file"
    machine_id=$OPU_READ_VALUE
    machine_id_hash=""
    if ! opu_safe_field "$machine_id"; then
        opu_discovery_append_coverage \
            "$coverage" "machine_id" "failed" "DISCOVERY_INVALID_MACHINE_ID" \
            "/etc/machine-id" "" || return
        opu_discovery_append_error \
            "$errors" "machine_id" "validate" "DISCOVERY_INVALID_MACHINE_ID" \
            "blocker" "The machine ID source contains unsupported control characters." false || return
        overall_status="partial"
        confidence="low"
    elif [ -n "$machine_id" ]; then
        machine_id_hash=$(opu_hash_string "$machine_id") || return
        opu_discovery_append_coverage \
            "$coverage" "machine_id" "complete" "" "/etc/machine-id" \
            "$(opu_hash_file "$machine_id_file")" || return
    else
        opu_discovery_append_coverage \
            "$coverage" "machine_id" "unavailable" "DISCOVERY_MACHINE_ID_UNAVAILABLE" \
            "/etc/machine-id" "" || return
        opu_discovery_append_error \
            "$errors" "machine_id" "collect" "DISCOVERY_MACHINE_ID_UNAVAILABLE" \
            "blocker" "No machine ID is available for stable host matching." true || return
        overall_status="partial"
        confidence="low"
    fi

    if [ "$OPU_TEST_MODE" = "1" ] && [ -n "${OPU_TEST_KERNEL_NAME:-}" ]; then
        kernel_name=$OPU_TEST_KERNEL_NAME
        kernel_release=${OPU_TEST_KERNEL_RELEASE:-unknown}
        architecture=${OPU_TEST_ARCHITECTURE:-unknown}
    else
        if ! kernel_name=$(uname -s) || ! kernel_release=$(uname -r) ||
            ! architecture=$(uname -m); then
            opu_discovery_append_coverage \
                "$coverage" "kernel" "failed" "DISCOVERY_UNAME_FAILED" "command:uname" "" || return
            opu_discovery_append_error \
                "$errors" "kernel" "collect" "DISCOVERY_UNAME_FAILED" \
                "blocker" "Kernel or architecture discovery failed." true || return
            opu_host_publish_discovery \
                "$output" "failed" "$coverage" "$errors" "$observations" "$resources" "$conflicts" || return
            opu_operation_error "DISCOVERY_UNAME_FAILED" "Kernel or architecture discovery failed." true
            return 20
        fi
    fi
    if ! opu_safe_field "$kernel_name" || ! opu_safe_field "$kernel_release" ||
        ! opu_safe_field "$architecture"; then
        opu_discovery_append_coverage \
            "$coverage" "kernel" "failed" "DISCOVERY_INVALID_KERNEL_DATA" "command:uname" "" || return
        opu_discovery_append_error \
            "$errors" "kernel" "validate" "DISCOVERY_INVALID_KERNEL_DATA" \
            "blocker" "A kernel discovery source contains unsupported control characters." false || return
        opu_host_publish_discovery \
            "$output" "failed" "$coverage" "$errors" "$observations" "$resources" "$conflicts" || return
        opu_operation_error \
            "DISCOVERY_INVALID_KERNEL_DATA" \
            "A kernel discovery source contains unsupported control characters." false
        return 20
    fi
    kernel_hash=$(opu_hash_string \
        "name=${kernel_name}|release=${kernel_release}|architecture=${architecture}") || return
    opu_discovery_append_coverage \
        "$coverage" "kernel" "complete" "" "command:uname" "$kernel_hash" || return

    composite_hash=$(opu_hash_string \
        "os_release=${os_release_hash}|machine_id=${machine_id_hash}|kernel=${kernel_hash}|hostname=${hostname_value}") || return
    {
        printf '{"kind":"host",'
        printf '"source":"composite",'
        printf '"confidence":%s,' "$(opu_json_string "$confidence")"
        printf '"evidence_sha256":%s,' "$(opu_json_string "$composite_hash")"
        printf '"natural_keys":{"machine_id_sha256":%s},' \
            "$(opu_json_nullable_string "$machine_id_hash")"
        printf '"attributes":{'
        printf '"hostname":%s,' "$(opu_json_nullable_string "$hostname_value")"
        printf '"os":{'
        printf '"id":%s,' "$(opu_json_string "$os_id")"
        printf '"name":%s,' "$(opu_json_nullable_string "$os_name")"
        printf '"version_id":%s,' "$(opu_json_nullable_string "$version_id")"
        printf '"pretty_name":%s,' "$(opu_json_nullable_string "$pretty_name")"
        printf '"id_like":%s},' "$(opu_json_nullable_string "$id_like")"
        printf '"kernel":{"name":%s,"release":%s},' \
            "$(opu_json_string "$kernel_name")" "$(opu_json_string "$kernel_release")"
        printf '"architecture":%s' "$(opu_json_string "$architecture")"
        printf '}}\n'
    } >>"$observations"

    opu_host_publish_discovery \
        "$output" "$overall_status" "$coverage" "$errors" "$observations" "$resources" "$conflicts"
}

