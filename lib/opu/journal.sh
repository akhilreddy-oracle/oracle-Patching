#!/usr/bin/env bash

# Reuse the same non-truncating inode checks as Oracle execution locks.
# shellcheck source=lib/opu/execution.sh
. "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/execution.sh"

OPU_LOCK_MODE=""
OPU_LOCK_DIR=""

opu_prepare_state_directory() {
    opu_validate_absolute_path "$OPU_STATE_DIR" "state directory" || return
    if [ "${OPU_STATE_DIR%/}" = "" ]; then
        opu_error "the filesystem root cannot be used as the state directory"
        return 64
    fi
    if [ -L "$OPU_STATE_DIR" ]; then
        opu_error "the state directory cannot be a symbolic link"
        return 64
    fi
    mkdir -p \
        "$OPU_STATE_DIR/locks" \
        "$OPU_STATE_DIR/results" \
        "$OPU_STATE_DIR/runs" \
        "$OPU_STATE_DIR/task-bindings" || {
        opu_error "cannot initialize state directory: $OPU_STATE_DIR"
        return 73
    }
}

opu_acquire_lock() {
    local key lock_file attempts max_attempts
    key=$1
    lock_file="$OPU_STATE_DIR/locks/${key}.lock"

    if command -v flock >/dev/null 2>&1; then
        opu_execution_open_lock_file "$lock_file" 9 || return $?
        if ! flock -w "$OPU_LOCK_WAIT_SECONDS" 9; then
            opu_error "timed out waiting for idempotency lock: $key"
            exec 9>&-
            return 75
        fi
        if ! opu_execution_validate_lock "$lock_file" 9; then
            exec 9>&-
            return 65
        fi
        OPU_LOCK_MODE="flock"
        return 0
    fi

    # Development fallback for systems without util-linux flock. Production
    # Oracle Linux packages declare flock as a required capability.
    OPU_LOCK_DIR="${lock_file}.d"
    attempts=0
    max_attempts=$((OPU_LOCK_WAIT_SECONDS * 10))
    while ! mkdir "$OPU_LOCK_DIR" 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge "$max_attempts" ]; then
            opu_error "timed out waiting for idempotency lock: $key"
            return 75
        fi
        sleep 0.1
    done
    OPU_LOCK_MODE="mkdir"
}

opu_release_lock() {
    case "$OPU_LOCK_MODE" in
        flock)
            flock -u 9 >/dev/null 2>&1 || true
            exec 9>&-
            ;;
        mkdir)
            if [ -n "$OPU_LOCK_DIR" ]; then
                rmdir "$OPU_LOCK_DIR" >/dev/null 2>&1 || true
            fi
            ;;
    esac
    OPU_LOCK_MODE=""
    OPU_LOCK_DIR=""
}

opu_bind_task_id() {
    local task_id idempotency_key binding temporary existing
    task_id=$1
    idempotency_key=$2
    binding="$OPU_STATE_DIR/task-bindings/${task_id}"
    temporary="${binding}.tmp.$$"

    printf '%s\n' "$idempotency_key" >"$temporary" || return 74
    if ln "$temporary" "$binding" 2>/dev/null; then
        rm -f "$temporary"
        return 0
    fi
    rm -f "$temporary"

    if ! IFS= read -r existing <"$binding"; then
        opu_error "cannot read existing task binding: $task_id"
        return 74
    fi
    if [ "$existing" != "$idempotency_key" ]; then
        opu_error "task ID is already bound to another idempotency key"
        return 65
    fi
}

opu_verify_completed_result() {
    local completion_dir expected actual request_hash exit_code expected_completion actual_completion
    completion_dir=$1
    if ! IFS= read -r expected <"$completion_dir/result.sha256"; then
        opu_error "completed result is missing its digest"
        return 74
    fi
    actual=$(opu_hash_file "$completion_dir/result.json") || return
    if [ "$actual" != "$expected" ]; then
        opu_error "completed result digest verification failed"
        return 74
    fi
    if ! IFS= read -r request_hash <"$completion_dir/request.sha256" ||
        ! IFS= read -r exit_code <"$completion_dir/exit_code" ||
        ! IFS= read -r expected_completion <"$completion_dir/completion.sha256"; then
        opu_error "completed result metadata is incomplete"
        return 74
    fi
    actual_completion=$(opu_hash_string \
        "result_sha256=${expected}|request_sha256=${request_hash}|exit_code=${exit_code}") || return
    if [ "$actual_completion" != "$expected_completion" ]; then
        opu_error "completed result metadata verification failed"
        return 74
    fi
}

opu_append_event() {
    local event_file event status timestamp
    event_file=$1
    event=$2
    status=$3
    timestamp=$(opu_now_utc) || return
    printf '{"schema_version":"1.0","time":"%s","event":"%s","status":"%s"}\n' \
        "$(opu_json_escape "$timestamp")" \
        "$(opu_json_escape "$event")" \
        "$(opu_json_escape "$status")" >>"$event_file"
}

opu_publish_completion() {
    local result_file exit_code request_hash completion_dir temporary_dir result_digest completion_digest
    result_file=$1
    exit_code=$2
    request_hash=$3
    completion_dir=$4
    temporary_dir="${completion_dir}.tmp.$$"

    rm -rf "$temporary_dir"
    mkdir "$temporary_dir" || return 74
    cp "$result_file" "$temporary_dir/result.json" || {
        rm -rf "$temporary_dir"
        return 74
    }
    printf '%s\n' "$exit_code" >"$temporary_dir/exit_code" || {
        rm -rf "$temporary_dir"
        return 74
    }
    printf '%s\n' "$request_hash" >"$temporary_dir/request.sha256" || {
        rm -rf "$temporary_dir"
        return 74
    }
    result_digest=$(opu_hash_file "$temporary_dir/result.json") || {
        rm -rf "$temporary_dir"
        return 74
    }
    printf '%s\n' "$result_digest" >"$temporary_dir/result.sha256" || {
        rm -rf "$temporary_dir"
        return 74
    }
    completion_digest=$(opu_hash_string \
        "result_sha256=${result_digest}|request_sha256=${request_hash}|exit_code=${exit_code}") || {
        rm -rf "$temporary_dir"
        return 74
    }
    printf '%s\n' "$completion_digest" >"$temporary_dir/completion.sha256" || {
        rm -rf "$temporary_dir"
        return 74
    }
    : >"$temporary_dir/complete" || {
        rm -rf "$temporary_dir"
        return 74
    }
    if ! mv "$temporary_dir" "$completion_dir"; then
        rm -rf "$temporary_dir"
        return 74
    fi
}
