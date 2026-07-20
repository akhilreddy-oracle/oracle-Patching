#!/usr/bin/env bash

# Durable, local control-plane primitives for fixed patch job types.  The job
# engine creates work; it never receives or executes arbitrary shell text.

OPU_JOB_STATE_DIR=${OPU_JOB_STATE_DIR:-/var/lib/oracle-patching-jobs}

opu_job_dir() { printf '%s/jobs/%s' "$OPU_JOB_STATE_DIR" "$1"; }
opu_job_state_file() { printf '%s/state' "$(opu_job_dir "$1")"; }
opu_job_event_file() { printf '%s/events.jsonl' "$(opu_job_dir "$1")"; }

opu_job_prepare_state() {
    opu_validate_absolute_path "$OPU_JOB_STATE_DIR" "job state directory" || return
    [ "${OPU_JOB_STATE_DIR%/}" != "" ] || { opu_error "filesystem root cannot be the job state directory"; return 64; }
    [ ! -L "$OPU_JOB_STATE_DIR" ] || { opu_error "job state directory cannot be a symbolic link"; return 64; }
    mkdir -p "$OPU_JOB_STATE_DIR/jobs" || { opu_error "cannot create job state directory"; return 73; }
}

opu_job_valid_patch_id() { [[ ${1:-} =~ ^[0-9]{1,20}$ ]]; }
opu_job_valid_digest() { [[ ${1:-} =~ ^[a-f0-9]{64}$ ]]; }

opu_job_verify_plan() {
    local job_id plan expected actual canonical
    job_id=$1; plan=$(opu_job_dir "$job_id")/plan.json
    [ -f "$plan" ] || { opu_error "job plan is missing: $job_id"; return 74; }
    expected=$(jq -r '.plan_sha256 // empty' "$plan") || return 74
    canonical=$(jq -c 'del(.plan_sha256)' "$plan") || return 74
    actual=$(opu_hash_string "$canonical") || return
    [ -n "$expected" ] && [ "$expected" = "$actual" ] || {
        opu_error "job plan integrity verification failed: $job_id"
        return 74
    }
}

opu_job_lock() {
    local job_id attempts=0 max_attempts=50
    job_id=$1
    OPU_JOB_LOCK_MODE=""
    OPU_JOB_LOCK_DIR=""
    if command -v flock >/dev/null 2>&1; then
        exec 8>"$(opu_job_dir "$job_id")/.lock" || return 73
        flock -w 5 8 || { opu_error "timed out waiting for job lock: $job_id"; return 75; }
        OPU_JOB_LOCK_MODE=flock
        return 0
    fi
    # macOS development environments do not provide flock. The mkdir fallback
    # is atomic on local POSIX filesystems and keeps the same short timeout.
    OPU_JOB_LOCK_DIR="$(opu_job_dir "$job_id")/.lock.d"
    while ! mkdir "$OPU_JOB_LOCK_DIR" 2>/dev/null; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt "$max_attempts" ] || { opu_error "timed out waiting for job lock: $job_id"; return 75; }
        sleep 0.1
    done
    OPU_JOB_LOCK_MODE=mkdir
}

opu_job_unlock() {
    case ${OPU_JOB_LOCK_MODE:-} in
        flock) flock -u 8 >/dev/null 2>&1 || true; exec 8>&-;;
        mkdir) rmdir "${OPU_JOB_LOCK_DIR:-}" >/dev/null 2>&1 || true;;
    esac
    OPU_JOB_LOCK_MODE=""; OPU_JOB_LOCK_DIR=""
}

opu_job_event() {
    local job_id event state actor detail file now
    job_id=$1; event=$2; state=$3; actor=$4; detail=${5:-}; file=$(opu_job_event_file "$job_id")
    now=$(opu_now_utc) || return
    printf '{"schema_version":"1.0","time":%s,"job_id":%s,"event":%s,"state":%s,"actor":%s,"detail":%s}\n' \
        "$(opu_json_string "$now")" "$(opu_json_string "$job_id")" \
        "$(opu_json_string "$event")" "$(opu_json_string "$state")" \
        "$(opu_json_nullable_string "$actor")" "$(opu_json_nullable_string "$detail")" >>"$file"
}

opu_job_read_state() {
    local state_file
    state_file=$(opu_job_state_file "$1")
    [ -f "$state_file" ] || { opu_error "job does not exist: $1"; return 66; }
    opu_job_verify_plan "$1" || return
    IFS= read -r OPU_JOB_STATE <"$state_file" || return 74
}

opu_job_transition() {
    local job_id expected next actor detail current state_file
    job_id=$1; expected=$2; next=$3; actor=$4; detail=${5:-}; state_file=$(opu_job_state_file "$job_id")
    opu_job_read_state "$job_id" || return
    current=$OPU_JOB_STATE
    [ "$current" = "$expected" ] || { opu_error "job $job_id is $current, expected $expected"; return 65; }
    printf '%s\n' "$next" >"${state_file}.tmp.$$" && mv "${state_file}.tmp.$$" "$state_file" || return 74
    opu_job_event "$job_id" "state_changed" "$next" "$actor" "$detail"
}

opu_job_read_nodes() {
    local node_file=$1 node
    OPU_JOB_NODES=()
    [ -f "$node_file" ] && [ ! -L "$node_file" ] || { opu_error "node file is missing or a symbolic link"; return 66; }
    while IFS= read -r node || [ -n "$node" ]; do
        node=${node%%#*}; node=${node//[[:space:]]/}; [ -z "$node" ] && continue
        [[ $node =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || { opu_error "invalid node name: $node"; return 64; }
        [[ " ${OPU_JOB_NODES[*]-} " == *" $node "* ]] || OPU_JOB_NODES+=("$node")
    done <"$node_file"
    ((${#OPU_JOB_NODES[@]})) || { opu_error "node file has no usable nodes"; return 64; }
}

opu_job_create() {
    local job_id requester topology database grid_home patch_id patch_dir node_file job_dir plan_tmp plan_hash node nodes_json first canonical job_type
    job_id=""; requester=""; topology=""; database=""; grid_home=""; patch_id=""; patch_dir=""; node_file=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --job-id) job_id=${2:-}; shift 2;; --requester) requester=${2:-}; shift 2;;
            --topology) topology=${2:-}; shift 2;; --patch-id) patch_id=${2:-}; shift 2;;
            --database) database=${2:-}; shift 2;; --grid-home) grid_home=${2:-}; shift 2;;
            --patch-dir) patch_dir=${2:-}; shift 2;; --nodes) node_file=${2:-}; shift 2;;
            *) opu_error "unknown create option: $1"; return 64;;
        esac
    done
    opu_validate_identifier "$job_id" "job ID" || return
    opu_validate_identifier "$requester" "requester" || return
    [[ "$topology" =~ ^(grid-rolling|database-rolling)$ ]] || { opu_error "unsupported topology: $topology"; return 64; }
    opu_job_valid_patch_id "$patch_id" || { opu_error "patch ID must be numeric"; return 64; }
    opu_validate_absolute_path "$patch_dir" "patch directory" || return
    opu_validate_absolute_path "$node_file" "node file" || return
    if [ "$topology" = database-rolling ]; then
        opu_validate_identifier "$database" "database" || return
        opu_validate_absolute_path "$grid_home" "Grid home" || return
        job_type=database.rolling.patch.v1
    else
        job_type=grid.rolling.patch.v1
    fi
    opu_job_prepare_state || return
    opu_job_read_nodes "$node_file" || return
    job_dir=$(opu_job_dir "$job_id")
    mkdir "$job_dir" 2>/dev/null || { opu_error "job already exists: $job_id"; return 65; }
    mkdir "$job_dir/tasks" || return 74
    nodes_json="["; first=1
    for node in "${OPU_JOB_NODES[@]}"; do
        [ "$first" -eq 1 ] || nodes_json+=","; nodes_json+="$(opu_json_string "$node")"; first=0
    done
    nodes_json+="]"
    plan_tmp="$job_dir/plan.json.tmp"
    if [ "$topology" = database-rolling ]; then
        printf '{"schema_version":"1.0","job_type":%s,"topology":"database-rolling","database":%s,"grid_home":%s,"patch_id":%s,"patch_directory":%s,"nodes":%s,"requester":%s}\n' \
            "$(opu_json_string "$job_type")" "$(opu_json_string "$database")" "$(opu_json_string "$grid_home")" "$(opu_json_string "$patch_id")" "$(opu_json_string "$patch_dir")" "$nodes_json" "$(opu_json_string "$requester")" >"$plan_tmp" || return 74
    else
        printf '{"schema_version":"1.0","job_type":%s,"topology":"grid-rolling","patch_id":%s,"patch_directory":%s,"nodes":%s,"requester":%s}\n' \
            "$(opu_json_string "$job_type")" "$(opu_json_string "$patch_id")" "$(opu_json_string "$patch_dir")" "$nodes_json" "$(opu_json_string "$requester")" >"$plan_tmp" || return 74
    fi
    canonical=$(jq -c . "$plan_tmp") || return 74
    plan_hash=$(opu_hash_string "$canonical") || return
    jq --arg hash "$plan_hash" '. + {plan_sha256:$hash}' "$plan_tmp" >"$job_dir/plan.json" || return 74
    rm -f "$plan_tmp"
    printf 'draft\n' >"$job_dir/state"
    printf '%s\n' "$requester" >"$job_dir/requester"
    opu_job_event "$job_id" "job_created" "draft" "$requester" "plan_sha256=$plan_hash" || return
    printf '%s\n' "$job_dir/plan.json"
}

opu_job_submit() { opu_job_transition "$1" draft prechecking "$2" "prechecks_requested"; }

opu_job_ready() {
    local job_id actor evidence
    job_id=$1; actor=$2; evidence=$3
    opu_job_valid_digest "$evidence" || { opu_error "evidence digest must be a SHA-256 hex value"; return 64; }
    printf '%s\n' "$evidence" >"$(opu_job_dir "$job_id")/precheck_evidence.sha256"
    opu_job_transition "$job_id" prechecking awaiting_approval "$actor" "precheck_evidence_sha256=$evidence"
}

opu_job_approve() {
    local job_id actor requester
    job_id=$1; actor=$2
    opu_validate_identifier "$actor" "approver" || return
    [ -f "$(opu_job_dir "$job_id")/requester" ] || { opu_error "job does not exist: $job_id"; return 66; }
    IFS= read -r requester <"$(opu_job_dir "$job_id")/requester" || return 74
    [ "$actor" != "$requester" ] || { opu_error "requester cannot approve their own job"; return 77; }
    printf '%s\n' "$actor" >"$(opu_job_dir "$job_id")/approved_by"
    opu_job_transition "$job_id" awaiting_approval scheduled "$actor" "plan_approved"
}

opu_job_start() {
    local job_id actor plan node index task_id task_file topology
    job_id=$1; actor=$2; plan=$(opu_job_dir "$job_id")/plan.json
    opu_job_transition "$job_id" scheduled running "$actor" "execution_started" || return
    topology=$(jq -r '.topology' "$plan") || return 74
    index=0
    while IFS= read -r node; do
        for stage in apply validate; do
            index=$((index + 1)); task_id=$(printf '%03d-%s-%s' "$index" "$stage" "$node"); task_file="$(opu_job_dir "$job_id")/tasks/$task_id.json"
            printf '{"schema_version":"1.0","task_id":%s,"job_id":%s,"stage":%s,"node":%s,"status":"pending"}\n' \
                "$(opu_json_string "$task_id")" "$(opu_json_string "$job_id")" "$(opu_json_string "$stage")" "$(opu_json_string "$node")" >"$task_file"
        done
    done < <(jq -r '.nodes[]' "$plan")
    if [ "$topology" = database-rolling ]; then
        index=$((index + 1)); task_id=$(printf '%03d-datapatch-cluster' "$index"); task_file="$(opu_job_dir "$job_id")/tasks/$task_id.json"
        printf '{"schema_version":"1.0","task_id":%s,"job_id":%s,"stage":"datapatch","node":"cluster","status":"pending"}\n' \
            "$(opu_json_string "$task_id")" "$(opu_json_string "$job_id")" >"$task_file"
        index=$((index + 1)); task_id=$(printf '%03d-validate-database' "$index"); task_file="$(opu_job_dir "$job_id")/tasks/$task_id.json"
        printf '{"schema_version":"1.0","task_id":%s,"job_id":%s,"stage":"validate-database","node":"cluster","status":"pending"}\n' \
            "$(opu_json_string "$task_id")" "$(opu_json_string "$job_id")" >"$task_file"
    fi
    opu_job_event "$job_id" "tasks_created" running "$actor" "count=$index"
}

opu_job_next() {
    local job_id task
    job_id=$1; opu_job_read_state "$job_id" || return
    [ "$OPU_JOB_STATE" = running ] || { opu_error "job is not running"; return 65; }
    for task in "$(opu_job_dir "$job_id")"/tasks/*.json; do
        [ -e "$task" ] || continue
        jq -e '.status == "pending"' "$task" >/dev/null 2>&1 && { cat "$task"; return 0; }
    done
    return 66
}

opu_job_claim() (
    local job_id actor lease_seconds now expires task task_id task_file
    job_id=$1; actor=$2; lease_seconds=$3
    opu_validate_identifier "$actor" "actor" || exit $?
    [[ $lease_seconds =~ ^[0-9]{1,4}$ ]] && [ "$lease_seconds" -ge 30 ] && [ "$lease_seconds" -le 3600 ] || {
        opu_error "lease seconds must be between 30 and 3600"; exit 64;
    }
    opu_job_lock "$job_id" || exit $?
    trap 'opu_job_unlock' EXIT
    opu_job_read_state "$job_id" || exit $?
    [ "$OPU_JOB_STATE" = running ] || { opu_error "job is not running"; exit 65; }
    now=$(date -u +%s); expires=$((now + lease_seconds))
    if find "$(opu_job_dir "$job_id")/tasks" -name '*.json' -exec jq -e 'select(.status == "running")' {} \; | grep -q .; then
        opu_error "a rolling task is already running; wait for its evidence or reconcile its lease"
        exit 65
    fi
    for task_file in "$(opu_job_dir "$job_id")"/tasks/*.json; do
        [ -e "$task_file" ] || continue
        jq -e '.status == "pending"' "$task_file" >/dev/null 2>&1 || continue
        task_id=$(jq -r '.task_id' "$task_file")
        jq --arg actor "$actor" --argjson now "$now" --argjson expires "$expires" \
            '.status="running" | .claimed_by=$actor | .claimed_at_epoch=$now | .lease_expires_epoch=$expires' \
            "$task_file" >"${task_file}.tmp" && mv "${task_file}.tmp" "$task_file" || exit 74
        opu_job_event "$job_id" "task_claimed" running "$actor" "task_id=$task_id lease_expires_epoch=$expires"
        cat "$task_file"
        exit 0
    done
    exit 66
)

opu_job_reconcile() (
    local job_id actor now task_file task_id expiry found=0
    job_id=$1; actor=$2; opu_job_lock "$job_id" || exit $?
    trap 'opu_job_unlock' EXIT
    opu_job_read_state "$job_id" || exit $?
    [ "$OPU_JOB_STATE" = running ] || { opu_error "job is not running"; exit 65; }
    now=$(date -u +%s)
    for task_file in "$(opu_job_dir "$job_id")"/tasks/*.json; do
        [ -e "$task_file" ] || continue
        jq -e '.status == "running"' "$task_file" >/dev/null 2>&1 || continue
        expiry=$(jq -r '.lease_expires_epoch // 0' "$task_file")
        [ "$expiry" -le "$now" ] || continue
        task_id=$(jq -r '.task_id' "$task_file")
        jq --argjson now "$now" '.status="unknown" | .reconciled_at_epoch=$now' "$task_file" >"${task_file}.tmp" && mv "${task_file}.tmp" "$task_file" || exit 74
        opu_job_transition "$job_id" running paused "$actor" "lease_expired_task=$task_id"
        opu_job_event "$job_id" "task_outcome_unknown" paused "$actor" "task_id=$task_id"
        found=1; break
    done
    [ "$found" -eq 1 ] && exit 0
    exit 66
)

opu_job_complete() {
    local job_id task_id actor status evidence task_file pending expected claimed_by expiry now
    job_id=$1; task_id=$2; actor=$3; status=$4; evidence=$5
    opu_validate_identifier "$task_id" "task ID" || return
    [[ $status =~ ^(succeeded|failed)$ ]] || { opu_error "task status must be succeeded or failed"; return 64; }
    opu_job_valid_digest "$evidence" || { opu_error "evidence digest must be a SHA-256 hex value"; return 64; }
    opu_job_read_state "$job_id" || return
    [ "$OPU_JOB_STATE" = running ] || { opu_error "job is not running"; return 65; }
    task_file="$(opu_job_dir "$job_id")/tasks/$task_id.json"; [ -f "$task_file" ] || { opu_error "task does not exist"; return 66; }
    claimed_by=$(jq -r '.claimed_by // empty' "$task_file")
    expiry=$(jq -r '.lease_expires_epoch // 0' "$task_file"); now=$(date -u +%s)
    [ "$claimed_by" = "$actor" ] || { opu_error "task is not claimed by actor $actor"; return 77; }
    [ "$expiry" -gt "$now" ] || { opu_error "task lease expired; reconcile before retrying"; return 75; }
    jq -e '.status == "running"' "$task_file" >/dev/null 2>&1 || { opu_error "task is not running"; return 65; }
    jq --arg status "$status" --arg evidence "$evidence" --argjson now "$now" \
        '.status=$status | .evidence_sha256=$evidence | .completed_at_epoch=$now' \
        "$task_file" >"${task_file}.tmp" && mv "${task_file}.tmp" "$task_file" || return 74
    if [ "$status" = failed ]; then opu_job_transition "$job_id" running paused "$actor" "task_failed=$task_id"; return; fi
    pending=$(find "$(opu_job_dir "$job_id")/tasks" -name '*.json' -exec jq -r 'select(.status == "pending") | 1' {} \; | wc -l | tr -d ' ')
    opu_job_event "$job_id" "task_completed" running "$actor" "task_id=$task_id"
    [ "$pending" -gt 0 ] || opu_job_transition "$job_id" running succeeded "$actor" "all_tasks_succeeded"
}

opu_job_status() {
    local job_id; job_id=$1; opu_job_read_state "$job_id" || return
    jq --arg state "$OPU_JOB_STATE" '. + {state:$state}' "$(opu_job_dir "$job_id")/plan.json"
}
