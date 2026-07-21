#!/usr/bin/env bash

opu_registry_resolve() {
    local operation version
    operation=$1
    version=$2

    OPU_OPERATION_FUNCTION=""
    OPU_OPERATION_MUTABILITY=""

    case "${operation}@${version}" in
        host.discover@1)
            # shellcheck disable=SC2034 # read by bin/opu-agent, which sources this file
            OPU_OPERATION_FUNCTION="opu_operation_host_discover_v1"
            # shellcheck disable=SC2034 # read by bin/opu-agent, which sources this file
            OPU_OPERATION_MUTABILITY="read_only"
            ;;
        oracle_homes.discover@1)
            # shellcheck disable=SC2034 # read by bin/opu-agent, which sources this file
            OPU_OPERATION_FUNCTION="opu_operation_oracle_homes_discover_v1"
            # shellcheck disable=SC2034 # read by bin/opu-agent, which sources this file
            OPU_OPERATION_MUTABILITY="read_only"
            ;;
        *)
            opu_error "unsupported operation or version: ${operation}@${version}"
            return 64
            ;;
    esac
}

opu_registry_print() {
    printf '%s\n' \
        '{' \
        '  "schema_version": "1.0",' \
        '  "operations": [' \
        '    {"name":"host.discover","version":"1","mutability":"read_only"},' \
        '    {"name":"oracle_homes.discover","version":"1","mutability":"read_only"}' \
        '  ]' \
        '}'
}

