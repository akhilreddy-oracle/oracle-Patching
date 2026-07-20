#!/usr/bin/env bash

OPU_HOME_RECORD_SEPARATOR=$'\037'
OPU_MAX_INVENTORY_XML_BYTES=10485760

opu_trim() {
    local value
    value=${1-}
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    printf '%s' "$value"
}

opu_resolve_home_path() {
    local logical_path host_path physical root_physical
    logical_path=$1
    OPU_HOME_PATH_EXISTS=false
    OPU_HOME_REAL_PATH=""
    OPU_HOME_PATH_ESCAPED=false

    host_path=$(opu_fs_path "$logical_path") || return
    if [ ! -d "$host_path" ]; then
        return 0
    fi
    OPU_HOME_PATH_EXISTS=true
    physical=$(CDPATH= cd -- "$host_path" 2>/dev/null && pwd -P) || {
        OPU_HOME_PATH_EXISTS=false
        return 0
    }

    if [ "$OPU_FS_ROOT" = "/" ]; then
        OPU_HOME_REAL_PATH=$physical
        return 0
    fi

    root_physical=$(CDPATH= cd -- "$OPU_FS_ROOT" 2>/dev/null && pwd -P) || return 74
    case "$physical" in
        "$root_physical") OPU_HOME_REAL_PATH="/" ;;
        "$root_physical"/*) OPU_HOME_REAL_PATH=/${physical#"$root_physical"/} ;;
        *)
            OPU_HOME_PATH_ESCAPED=true
            OPU_HOME_PATH_EXISTS=false
            OPU_HOME_REAL_PATH=""
            ;;
    esac
}

opu_append_home_candidate() {
    local observations records source source_location evidence_sha256 confidence
    local home sid auto_start inventory_name removed runtime_pid effective_uid
    observations=$1
    records=$2
    source=$3
    source_location=$4
    evidence_sha256=$5
    confidence=$6
    home=$7
    sid=${8-}
    auto_start=${9-}
    inventory_name=${10-}
    removed=${11:-false}
    runtime_pid=${12-}
    effective_uid=${13-}

    opu_is_normalized_logical_path "$home" || return 65
    if ! opu_safe_field "$source_location" || ! opu_safe_field "$sid" ||
        ! opu_safe_field "$auto_start" || ! opu_safe_field "$inventory_name" ||
        ! opu_safe_field "$runtime_pid" || ! opu_safe_field "$effective_uid"; then
        return 65
    fi

    opu_resolve_home_path "$home" || return
    if [ "$OPU_HOME_PATH_ESCAPED" = "true" ]; then
        return 65
    fi

    {
        printf '{"kind":"oracle_home_observation",'
        printf '"source":%s,' "$(opu_json_string "$source")"
        printf '"source_location":%s,' "$(opu_json_string "$source_location")"
        printf '"confidence":%s,' "$(opu_json_string "$confidence")"
        printf '"evidence_sha256":%s,' "$(opu_json_string "$evidence_sha256")"
        printf '"natural_keys":{'
        printf '"configured_path":%s,' "$(opu_json_string "$home")"
        printf '"real_path":%s},' "$(opu_json_nullable_string "$OPU_HOME_REAL_PATH")"
        printf '"attributes":{'
        printf '"oracle_sid":%s,' "$(opu_json_nullable_string "$sid")"
        printf '"auto_start":%s,' "$(opu_json_nullable_string "$auto_start")"
        printf '"inventory_name":%s,' "$(opu_json_nullable_string "$inventory_name")"
        printf '"path_exists":%s,' "$OPU_HOME_PATH_EXISTS"
        printf '"inventory_removed":%s,' "$removed"
        printf '"runtime_pid":%s,' "$(opu_json_nullable_string "$runtime_pid")"
        printf '"effective_uid":%s' "$(opu_json_nullable_string "$effective_uid")"
        printf '}}\n'
    } >>"$observations"

    printf '%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s\n' \
        "$home" "$OPU_HOME_RECORD_SEPARATOR" \
        "$OPU_HOME_REAL_PATH" "$OPU_HOME_RECORD_SEPARATOR" \
        "$source" "$OPU_HOME_RECORD_SEPARATOR" \
        "$sid" "$OPU_HOME_RECORD_SEPARATOR" \
        "$auto_start" "$OPU_HOME_RECORD_SEPARATOR" \
        "$inventory_name" "$OPU_HOME_RECORD_SEPARATOR" \
        "$OPU_HOME_PATH_EXISTS" "$OPU_HOME_RECORD_SEPARATOR" \
        "$removed" "$OPU_HOME_RECORD_SEPARATOR" \
        "$runtime_pid" "$OPU_HOME_RECORD_SEPARATOR" \
        "$effective_uid" "$OPU_HOME_RECORD_SEPARATOR" \
        "$evidence_sha256" >>"$records"
}

opu_collect_oratab_homes() {
    local observations records coverage errors oratab line line_number
    local sid remainder home auto_start digest invalid_count
    observations=$1
    records=$2
    coverage=$3
    errors=$4
    OPU_SOURCE_COMPLETE=0
    OPU_SOURCE_DEGRADED=0

    oratab=$(opu_fs_path /etc/oratab) || return
    if [ ! -r "$oratab" ]; then
        opu_discovery_append_coverage \
            "$coverage" "oratab" "unavailable" "DISCOVERY_ORATAB_UNAVAILABLE" \
            "/etc/oratab" "" || return
        opu_discovery_append_error \
            "$errors" "oratab" "collect" "DISCOVERY_ORATAB_UNAVAILABLE" \
            "warning" "The Oracle oratab file is not readable." true || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi

    digest=$(opu_hash_file "$oratab") || return
    line_number=0
    invalid_count=0
    while IFS= read -r line || [ -n "$line" ]; do
        line_number=$((line_number + 1))
        line=$(opu_trim "$line")
        case "$line" in
            "" | \#*) continue ;;
        esac
        sid=${line%%:*}
        if [ "$sid" = "$line" ]; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "oratab" "validate" "DISCOVERY_ORATAB_RECORD_INVALID" \
                "warning" "An oratab record at line $line_number is malformed." false || return
            continue
        fi
        remainder=${line#*:}
        home=${remainder%%:*}
        if [ "$home" = "$remainder" ]; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "oratab" "validate" "DISCOVERY_ORATAB_RECORD_INVALID" \
                "warning" "An oratab record at line $line_number is malformed." false || return
            continue
        fi
        auto_start=${remainder#*:}
        sid=$(opu_trim "$sid")
        home=$(opu_trim "$home")
        auto_start=$(opu_trim "$auto_start")
        if ! opu_is_normalized_logical_path "$home"; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "oratab" "validate" "DISCOVERY_ORATAB_HOME_PATH_INVALID" \
                "warning" "An oratab record at line $line_number has an unsafe Oracle-home path." false || return
            continue
        fi
        if ! opu_append_home_candidate \
            "$observations" "$records" "oratab" "/etc/oratab:$line_number" \
            "$digest" "medium" "$home" "$sid" "$auto_start" "" false "" ""; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "oratab" "validate" "DISCOVERY_ORATAB_RECORD_REJECTED" \
                "warning" "An oratab record at line $line_number could not be represented safely." false || return
        fi
    done <"$oratab"

    if [ "$invalid_count" -gt 0 ]; then
        opu_discovery_append_coverage \
            "$coverage" "oratab" "partial" "DISCOVERY_ORATAB_RECORDS_SKIPPED" \
            "/etc/oratab" "$digest" || return
        OPU_SOURCE_DEGRADED=1
    else
        opu_discovery_append_coverage \
            "$coverage" "oratab" "complete" "" "/etc/oratab" "$digest" || return
        OPU_SOURCE_COMPLETE=1
    fi
}

opu_collect_inventory_homes() {
    local observations records coverage errors ora_inst pointer_digest line inventory_loc
    local inventory_xml xml_digest xml_size count index xml_loc xml_name xml_removed invalid_count
    observations=$1
    records=$2
    coverage=$3
    errors=$4
    OPU_SOURCE_COMPLETE=0
    OPU_SOURCE_DEGRADED=0

    ora_inst=$(opu_fs_path /etc/oraInst.loc) || return
    if [ ! -r "$ora_inst" ]; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory_pointer" "unavailable" \
            "DISCOVERY_ORAINST_UNAVAILABLE" "/etc/oraInst.loc" "" || return
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "unavailable" \
            "DISCOVERY_ORAINST_UNAVAILABLE" "" "" || return
        opu_discovery_append_error \
            "$errors" "central_inventory_pointer" "collect" "DISCOVERY_ORAINST_UNAVAILABLE" \
            "warning" "The Oracle inventory pointer is not readable." true || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi
    pointer_digest=$(opu_hash_file "$ora_inst") || return
    inventory_loc=""
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            inventory_loc=*)
                inventory_loc=$(opu_trim "${line#*=}")
                break
                ;;
        esac
    done <"$ora_inst"
    if ! opu_is_normalized_logical_path "$inventory_loc"; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory_pointer" "failed" \
            "DISCOVERY_ORAINST_PATH_INVALID" "/etc/oraInst.loc" "$pointer_digest" || return
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "unavailable" \
            "DISCOVERY_ORAINST_PATH_INVALID" "" "" || return
        opu_discovery_append_error \
            "$errors" "central_inventory_pointer" "validate" "DISCOVERY_ORAINST_PATH_INVALID" \
            "blocker" "The Oracle inventory pointer contains an unsafe path." false || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi
    opu_discovery_append_coverage \
        "$coverage" "central_inventory_pointer" "complete" "" "/etc/oraInst.loc" \
        "$pointer_digest" || return

    inventory_xml=$(opu_fs_path "${inventory_loc%/}/ContentsXML/inventory.xml") || return
    if [ ! -r "$inventory_xml" ]; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "unavailable" \
            "DISCOVERY_INVENTORY_XML_UNAVAILABLE" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "" || return
        opu_discovery_append_error \
            "$errors" "central_inventory" "collect" "DISCOVERY_INVENTORY_XML_UNAVAILABLE" \
            "warning" "The central Oracle inventory XML is not readable." true || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi
    xml_digest=$(opu_hash_file "$inventory_xml") || return
    xml_size=$(wc -c <"$inventory_xml" | tr -d ' ')
    case "$xml_size" in
        "" | *[!0-9]*) xml_size=0 ;;
    esac
    if [ "$xml_size" -gt "$OPU_MAX_INVENTORY_XML_BYTES" ]; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "failed" "DISCOVERY_INVENTORY_XML_TOO_LARGE" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        opu_discovery_append_error \
            "$errors" "central_inventory" "validate" "DISCOVERY_INVENTORY_XML_TOO_LARGE" \
            "blocker" "The central inventory XML exceeds the collector size limit." false || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi
    if grep -i '<!DOCTYPE' "$inventory_xml" >/dev/null 2>&1; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "failed" "DISCOVERY_INVENTORY_XML_DOCTYPE_REJECTED" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        opu_discovery_append_error \
            "$errors" "central_inventory" "validate" "DISCOVERY_INVENTORY_XML_DOCTYPE_REJECTED" \
            "blocker" "DOCTYPE declarations are not accepted in inventory evidence." false || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi
    if ! command -v xmllint >/dev/null 2>&1; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "failed" "DISCOVERY_XMLLINT_UNAVAILABLE" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        opu_discovery_append_error \
            "$errors" "central_inventory" "detect" "DISCOVERY_XMLLINT_UNAVAILABLE" \
            "blocker" "The required xmllint parser is unavailable." false || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi
    if ! xmllint --nonet --noout "$inventory_xml" >/dev/null 2>&1; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "failed" "DISCOVERY_INVENTORY_XML_MALFORMED" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        opu_discovery_append_error \
            "$errors" "central_inventory" "validate" "DISCOVERY_INVENTORY_XML_MALFORMED" \
            "blocker" "The central Oracle inventory is not well-formed XML." false || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi

    count=$(xmllint --nonet --xpath 'count(/INVENTORY/HOME_LIST/HOME)' "$inventory_xml" 2>/dev/null) || {
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "failed" "DISCOVERY_INVENTORY_XML_QUERY_FAILED" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        opu_discovery_append_error \
            "$errors" "central_inventory" "collect" "DISCOVERY_INVENTORY_XML_QUERY_FAILED" \
            "blocker" "The central inventory HOME_LIST could not be queried." false || return
        OPU_SOURCE_DEGRADED=1
        return 0
    }
    case "$count" in
        "" | *[!0-9]*)
            opu_discovery_append_coverage \
                "$coverage" "central_inventory" "failed" "DISCOVERY_INVENTORY_XML_QUERY_FAILED" \
                "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
            opu_discovery_append_error \
                "$errors" "central_inventory" "collect" "DISCOVERY_INVENTORY_XML_QUERY_FAILED" \
                "blocker" "The central inventory HOME_LIST could not be queried." false || return
            OPU_SOURCE_DEGRADED=1
            return 0
            ;;
    esac

    invalid_count=0
    index=1
    while [ "$index" -le "$count" ]; do
        xml_loc=$(xmllint --nonet --xpath \
            "string((/INVENTORY/HOME_LIST/HOME)[$index]/@LOC)" "$inventory_xml" 2>/dev/null) || xml_loc=""
        xml_name=$(xmllint --nonet --xpath \
            "string((/INVENTORY/HOME_LIST/HOME)[$index]/@NAME)" "$inventory_xml" 2>/dev/null) || xml_name=""
        xml_removed=$(xmllint --nonet --xpath \
            "string((/INVENTORY/HOME_LIST/HOME)[$index]/@REMOVED)" "$inventory_xml" 2>/dev/null) || xml_removed=""
        case "$xml_removed" in
            T | TRUE | Y | true) xml_removed=true ;;
            *) xml_removed=false ;;
        esac
        if ! opu_is_normalized_logical_path "$xml_loc" || ! opu_safe_field "$xml_name"; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "central_inventory" "validate" "DISCOVERY_INVENTORY_HOME_INVALID" \
                "warning" "A central inventory HOME entry at index $index was rejected." false || return
            index=$((index + 1))
            continue
        fi
        if ! opu_append_home_candidate \
            "$observations" "$records" "central_inventory" \
            "${inventory_loc%/}/ContentsXML/inventory.xml#HOME[$index]" \
            "$xml_digest" "high" "$xml_loc" "" "" "$xml_name" "$xml_removed" "" ""; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "central_inventory" "validate" "DISCOVERY_INVENTORY_HOME_REJECTED" \
                "warning" "A central inventory HOME entry at index $index could not be represented safely." false || return
        fi
        index=$((index + 1))
    done

    if [ "$invalid_count" -gt 0 ]; then
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "partial" "DISCOVERY_INVENTORY_HOMES_SKIPPED" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        OPU_SOURCE_DEGRADED=1
    else
        opu_discovery_append_coverage \
            "$coverage" "central_inventory" "complete" "" \
            "${inventory_loc%/}/ContentsXML/inventory.xml" "$xml_digest" || return
        OPU_SOURCE_COMPLETE=1
    fi
}

opu_collect_runtime_homes() {
    local observations records coverage errors proc_root comm_file process_dir pid comm
    local executable executable_deleted home sid status_file effective_uid evidence invalid_count relevant_count
    local runtime_evidence_file
    observations=$1
    records=$2
    coverage=$3
    errors=$4
    OPU_SOURCE_COMPLETE=0
    OPU_SOURCE_DEGRADED=0

    proc_root=$(opu_fs_path /proc) || return
    if [ ! -d "$proc_root" ] || [ ! -r "$proc_root" ]; then
        opu_discovery_append_coverage \
            "$coverage" "runtime_processes" "unavailable" "DISCOVERY_PROC_UNAVAILABLE" \
            "/proc" "" || return
        opu_discovery_append_error \
            "$errors" "runtime_processes" "collect" "DISCOVERY_PROC_UNAVAILABLE" \
            "warning" "The process filesystem is unavailable." true || return
        OPU_SOURCE_DEGRADED=1
        return 0
    fi

    runtime_evidence_file="${records}.runtime-evidence"
    : >"$runtime_evidence_file" || return 74
    invalid_count=0
    relevant_count=0
    for comm_file in "$proc_root"/[0-9]*/comm; do
        [ -e "$comm_file" ] || continue
        process_dir=${comm_file%/comm}
        pid=${process_dir##*/}
        if ! IFS= read -r comm <"$comm_file"; then
            continue
        fi
        case "$comm" in
            ora_pmon_*) ;;
            *) continue ;;
        esac
        relevant_count=$((relevant_count + 1))
        if ! opu_safe_field "$comm"; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "runtime_processes" "validate" "DISCOVERY_PROCESS_NAME_INVALID" \
                "warning" "A PMON process name could not be represented safely." false || return
            continue
        fi
        sid=${comm#ora_pmon_}
        if [ -z "$sid" ]; then
            invalid_count=$((invalid_count + 1))
            continue
        fi
        executable=$(readlink "$process_dir/exe" 2>/dev/null) || executable=""
        executable_deleted=false
        case "$executable" in
            *' (deleted)')
                executable=${executable%' (deleted)'}
                executable_deleted=true
                ;;
        esac
        case "$executable" in
            /*/bin/oracle) home=${executable%/bin/oracle} ;;
            *)
                invalid_count=$((invalid_count + 1))
                opu_discovery_append_error \
                    "$errors" "runtime_processes" "collect" "DISCOVERY_PMON_EXECUTABLE_UNAVAILABLE" \
                    "warning" "A PMON executable path was unavailable or unexpected for PID $pid." true || return
                continue
                ;;
        esac
        if ! opu_is_normalized_logical_path "$home"; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "runtime_processes" "validate" "DISCOVERY_PMON_HOME_PATH_INVALID" \
                "warning" "A PMON executable resolved to an unsafe Oracle-home path." false || return
            continue
        fi
        status_file="$process_dir/status"
        effective_uid=""
        if [ -r "$status_file" ]; then
            effective_uid=$(awk '/^Uid:/{print $3; exit}' "$status_file")
            case "$effective_uid" in
                "" | *[!0-9]*) effective_uid="" ;;
            esac
        fi
        evidence=$(opu_hash_string \
            "pid=${pid}|comm=${comm}|executable=${executable}|effective_uid=${effective_uid}|deleted=${executable_deleted}") || return
        printf '%s\n' "$evidence" >>"$runtime_evidence_file"
        if ! opu_append_home_candidate \
            "$observations" "$records" "runtime_process" "/proc/$pid" \
            "$evidence" "high" "$home" "$sid" "" "" false "$pid" "$effective_uid"; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "runtime_processes" "validate" "DISCOVERY_RUNTIME_HOME_REJECTED" \
                "warning" "A runtime Oracle-home observation could not be represented safely." false || return
        fi
        if [ "$executable_deleted" = "true" ]; then
            invalid_count=$((invalid_count + 1))
            opu_discovery_append_error \
                "$errors" "runtime_processes" "validate" "DISCOVERY_RUNTIME_EXECUTABLE_DELETED" \
                "warning" "A PMON executable is marked deleted for PID $pid." false || return
        fi
    done

    sort "$runtime_evidence_file" -o "$runtime_evidence_file"
    evidence=$(opu_hash_file "$runtime_evidence_file") || return
    rm -f "$runtime_evidence_file"
    if [ "$invalid_count" -gt 0 ]; then
        opu_discovery_append_coverage \
            "$coverage" "runtime_processes" "partial" "DISCOVERY_RUNTIME_PROCESSES_SKIPPED" \
            "/proc" "$evidence" || return
        OPU_SOURCE_DEGRADED=1
    else
        opu_discovery_append_coverage \
            "$coverage" "runtime_processes" "complete" "" "/proc" "$evidence" || return
        OPU_SOURCE_COMPLETE=1
    fi
}

opu_reconcile_home_records() {
    local records resources conflicts work_prefix paths home record_home real_path source sid auto_start
    local inventory_name path_exists removed runtime_pid effective_uid evidence first_real real_conflict
    local configured runtime_active inventory_attached inventory_removed any_exists source_count confidence fingerprint
    local sources_file sids_file names_file reasons_file combined_names
    records=$1
    resources=$2
    conflicts=$3
    work_prefix=$4
    paths="${work_prefix}.paths"
    : >"$paths" || return 74

    while IFS="$OPU_HOME_RECORD_SEPARATOR" read -r record_home _rest; do
        [ -n "$record_home" ] && printf '%s\n' "$record_home" >>"$paths"
    done <"$records"
    sort -u "$paths" -o "$paths"

    while IFS= read -r home || [ -n "$home" ]; do
        [ -n "$home" ] || continue
        sources_file="${work_prefix}.sources"
        sids_file="${work_prefix}.sids"
        names_file="${work_prefix}.names"
        reasons_file="${work_prefix}.reasons"
        : >"$sources_file" && : >"$sids_file" && : >"$names_file" && : >"$reasons_file" || return 74
        first_real=""
        real_conflict=false
        configured=false
        runtime_active=false
        inventory_attached=false
        inventory_removed=false
        any_exists=false

        while IFS="$OPU_HOME_RECORD_SEPARATOR" read -r \
            record_home real_path source sid auto_start inventory_name path_exists removed \
            runtime_pid effective_uid evidence; do
            [ "$record_home" = "$home" ] || continue
            printf '%s\n' "$source" >>"$sources_file"
            [ -n "$sid" ] && printf '%s\n' "$sid" >>"$sids_file"
            [ -n "$inventory_name" ] && printf '%s\n' "$inventory_name" >>"$names_file"
            if [ -n "$real_path" ]; then
                if [ -z "$first_real" ]; then
                    first_real=$real_path
                elif [ "$first_real" != "$real_path" ]; then
                    real_conflict=true
                fi
            fi
            [ "$path_exists" = "true" ] && any_exists=true
            case "$source" in
                oratab) configured=true ;;
                runtime_process) runtime_active=true ;;
                central_inventory)
                    if [ "$removed" = "true" ]; then
                        inventory_removed=true
                    else
                        inventory_attached=true
                    fi
                    ;;
            esac
        done <"$records"

        sort -u "$sources_file" -o "$sources_file"
        sort -u "$sids_file" -o "$sids_file"
        sort -u "$names_file" -o "$names_file"
        source_count=$(wc -l <"$sources_file" | tr -d ' ')
        if [ -n "$first_real" ] && [ "$source_count" -ge 2 ]; then
            confidence="high"
        else
            confidence="medium"
        fi
        combined_names=$(tr '\n' ',' <"$names_file")
        fingerprint=$(opu_hash_string \
            "canonical_path=${home}|real_path=${first_real}|inventory_names=${combined_names}") || return

        {
            printf '{"kind":"oracle_home",'
            printf '"confidence":%s,' "$(opu_json_string "$confidence")"
            printf '"identity":{'
            printf '"canonical_path":%s,' "$(opu_json_string "$home")"
            printf '"real_path":%s,' "$(opu_json_nullable_string "$first_real")"
            printf '"identity_fingerprint":%s},' "$(opu_json_string "$fingerprint")"
            printf '"attributes":{'
            printf '"path_exists":%s,' "$any_exists"
            printf '"configured":%s,' "$configured"
            printf '"runtime_active":%s,' "$runtime_active"
            printf '"inventory_attached":%s,' "$inventory_attached"
            printf '"inventory_removed":%s,' "$inventory_removed"
            printf '"oracle_sids":['
            opu_text_print_json_array "$sids_file"
            printf '],"inventory_names":['
            opu_text_print_json_array "$names_file"
            printf '],"sources":['
            opu_text_print_json_array "$sources_file"
            printf ']}}\n'
        } >>"$resources"

        [ "$real_conflict" = "true" ] && printf '%s\n' "REAL_PATH_CONFLICT" >>"$reasons_file"
        if [ "$inventory_removed" = "true" ] && [ "$runtime_active" = "true" ]; then
            printf '%s\n' "INVENTORY_REMOVED_RUNTIME_ACTIVE" >>"$reasons_file"
        fi
        if [ "$inventory_removed" = "true" ] && [ "$configured" = "true" ]; then
            printf '%s\n' "INVENTORY_REMOVED_BUT_CONFIGURED" >>"$reasons_file"
        fi
        if [ "$runtime_active" = "true" ] && [ "$any_exists" != "true" ]; then
            printf '%s\n' "RUNTIME_HOME_PATH_MISSING" >>"$reasons_file"
        fi
        sort -u "$reasons_file" -o "$reasons_file"
        if [ -s "$reasons_file" ]; then
            {
                printf '{"kind":"discovery_conflict",'
                printf '"resource_kind":"oracle_home",'
                printf '"natural_key":%s,' "$(opu_json_string "$home")"
                printf '"reason_codes":['
                opu_text_print_json_array "$reasons_file"
                printf '],"sources":['
                opu_text_print_json_array "$sources_file"
                printf ']}\n'
            } >>"$conflicts"
        fi
    done <"$paths"

    rm -f "$paths" "${work_prefix}.sources" "${work_prefix}.sids" \
        "${work_prefix}.names" "${work_prefix}.reasons"
}

opu_operation_oracle_homes_discover_v1() {
    local output coverage errors observations resources conflicts records work_prefix
    local complete_sources degraded_sources overall_status
    output=$1
    work_prefix="${output}.work.$$"
    coverage="${work_prefix}.coverage"
    errors="${work_prefix}.errors"
    observations="${work_prefix}.observations"
    resources="${work_prefix}.resources"
    conflicts="${work_prefix}.conflicts"
    records="${work_prefix}.records"
    : >"$coverage" && : >"$errors" && : >"$observations" &&
        : >"$resources" && : >"$conflicts" && : >"$records" || return 74

    complete_sources=0
    degraded_sources=0
    opu_collect_oratab_homes "$observations" "$records" "$coverage" "$errors" || return
    complete_sources=$((complete_sources + OPU_SOURCE_COMPLETE))
    degraded_sources=$((degraded_sources + OPU_SOURCE_DEGRADED))
    opu_collect_inventory_homes "$observations" "$records" "$coverage" "$errors" || return
    complete_sources=$((complete_sources + OPU_SOURCE_COMPLETE))
    degraded_sources=$((degraded_sources + OPU_SOURCE_DEGRADED))
    opu_collect_runtime_homes "$observations" "$records" "$coverage" "$errors" || return
    complete_sources=$((complete_sources + OPU_SOURCE_COMPLETE))
    degraded_sources=$((degraded_sources + OPU_SOURCE_DEGRADED))

    sort "$observations" -o "$observations"
    sort "$records" -o "$records"
    opu_reconcile_home_records "$records" "$resources" "$conflicts" "$work_prefix" || return
    sort "$resources" -o "$resources"
    sort "$conflicts" -o "$conflicts"

    if [ "$complete_sources" -eq 0 ]; then
        overall_status="failed"
    elif [ "$degraded_sources" -gt 0 ]; then
        overall_status="partial"
    else
        overall_status="complete"
    fi
    opu_discovery_write_payload \
        "$output" "oracle_homes.discover" "1" "$overall_status" \
        "$coverage" "$errors" "$observations" "$resources" "$conflicts" || return

    rm -f "$coverage" "$errors" "$observations" "$resources" "$conflicts" "$records"
}
