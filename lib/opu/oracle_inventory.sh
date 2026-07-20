#!/usr/bin/env bash

# Extract the Oracle-home ARU platform from an OPatch lsinventory XML file.
#
# Current OPatch releases serialize the local machine platform as the direct
# InventoryInstance/osPlatform child. Older inventories can expose the same
# values through ARU_PLATFORM_INFO or PROPERTY elements. Patch-local
# osPlatforms/osPlatform entries are deliberately ignored because they may be
# generic platform 0 records and are not evidence of the Oracle-home platform.
opu_extract_opatch_platform() {
    local file platform_id platform_name
    file=${1:-}

    [ -n "$file" ] && [ -f "$file" ] && [ ! -L "$file" ] || return 66
    command -v xmllint >/dev/null 2>&1 || return 69
    xmllint --nonet --noout "$file" >/dev/null 2>&1 || return 65

    platform_id=$(xmllint --nonet --xpath \
        'normalize-space(string((/*[local-name()="InventoryInstance"]/*[local-name()="osPlatform"])[1]/@id))' \
        "$file" 2>/dev/null || true)
    platform_name=$(xmllint --nonet --xpath \
        'normalize-space(string((/*[local-name()="InventoryInstance"]/*[local-name()="osPlatform"])[1]/*[local-name()="version"][1]))' \
        "$file" 2>/dev/null || true)

    if ! [[ "$platform_id" =~ ^[1-9][0-9]*$ ]] || [ -z "$platform_name" ]; then
        platform_id=$(xmllint --nonet --xpath \
            'normalize-space(string((//*[local-name()="ARU_PLATFORM_INFO"]/*[local-name()="ARU_ID"])[1]))' \
            "$file" 2>/dev/null || true)
        platform_name=$(xmllint --nonet --xpath \
            'normalize-space(string((//*[local-name()="ARU_PLATFORM_INFO"]/*[local-name()="ARU_ID_DESCRIPTION"])[1]))' \
            "$file" 2>/dev/null || true)
    fi

    if ! [[ "$platform_id" =~ ^[1-9][0-9]*$ ]] || [ -z "$platform_name" ]; then
        platform_id=$(xmllint --nonet --xpath \
            'normalize-space(string((//*[local-name()="PROPERTY" or local-name()="property"][@name="ARU_ID"]/@value | //*[local-name()="PROPERTY" or local-name()="property"][@name="ARU_ID"]/@val)[1]))' \
            "$file" 2>/dev/null || true)
        platform_name=$(xmllint --nonet --xpath \
            'normalize-space(string((//*[local-name()="PROPERTY" or local-name()="property"][@name="ARU_ID_DESCRIPTION"]/@value | //*[local-name()="PROPERTY" or local-name()="property"][@name="ARU_ID_DESCRIPTION"]/@val)[1]))' \
            "$file" 2>/dev/null || true)
    fi

    [[ "$platform_id" =~ ^[1-9][0-9]*$ ]] || return 65
    [ -n "$platform_name" ] || return 65
    case "$platform_name" in
        *$'\n'* | *$'\r'* | *$'\t'*) return 65 ;;
    esac

    printf '%s\t%s\n' "$platform_id" "$platform_name"
}
