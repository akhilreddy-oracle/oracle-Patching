#!/usr/bin/env bash
# Shared execution lifecycle primitives. Oracle procedure commands remain in
# their adapters; this file owns only locking and immutable attempt identity.

# shellcheck source=lib/opu/python.sh
. "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/python.sh"

opu_execution_flock() {
  local descriptor=$1 timeout=${2:-0}
  if command -v flock >/dev/null 2>&1; then
    flock -w "$timeout" "$descriptor"
  elif [ "$(uname -s)" = Darwin ]; then
    # Development hosts use the same kernel lock and inherited open file
    # description as production util-linux flock.
    opu_python -c 'import fcntl,sys,time
deadline=time.monotonic()+float(sys.argv[2])
while True:
    try:
        fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline: raise SystemExit(75)
        time.sleep(0.05)' "$descriptor" "$timeout"
  else
    opu_error 'flock is required for execution exclusion'
    return 69
  fi
}

opu_execution_validate_lock() {
  local path=$1 descriptor=${2:--1}
  opu_python - "$path" "$descriptor" <<'PY'
import os
import stat
import sys

path, descriptor = sys.argv[1], int(sys.argv[2])

def regular_single_link(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1

def identity(info):
    return info.st_dev, info.st_ino

try:
    try:
        named = os.lstat(path)
    except FileNotFoundError:
        if descriptor < 0:
            raise SystemExit(0)
        raise ValueError("opened lock no longer has a pathname")
    if not regular_single_link(named):
        raise ValueError("lock must be a regular file with exactly one link")
    if descriptor >= 0:
        held = os.fstat(descriptor)
        if not regular_single_link(held) or identity(held) != identity(named):
            raise ValueError("opened lock inode differs from its safe pathname")
        # Validate the held descriptor against a no-follow open, rather than
        # trusting a pathname check performed before the shell opened it.
        check_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            checked, current = os.fstat(check_fd), os.lstat(path)
            if (not regular_single_link(checked) or not regular_single_link(current)
                    or identity(held) != identity(checked) or identity(held) != identity(current)):
                raise ValueError("lock pathname changed during validation")
        finally:
            os.close(check_fd)
except (OSError, ValueError) as exc:
    print(f"opu-agent: unsafe lock file {path}: {exc}", file=sys.stderr)
    raise SystemExit(65)
PY
}

opu_execution_open_lock_file() {
  local path=$1 descriptor=$2 result
  case "$descriptor" in 6|7|8|9) ;; *) opu_error 'unsupported execution lock descriptor'; return 64;; esac
  opu_execution_validate_lock "$path" || return $?
  # O_RDWR without O_TRUNC preserves any existing contents. Opening a raced
  # FIFO read/write also cannot block waiting for another endpoint. No data is
  # ever written through this descriptor; validate its inode before flock.
  case "$descriptor" in
    6) exec 6<>"$path" || return 73 ;;
    7) exec 7<>"$path" || return 73 ;;
    8) exec 8<>"$path" || return 73 ;;
    9) exec 9<>"$path" || return 73 ;;
  esac
  if opu_execution_validate_lock "$path" "$descriptor"; then
    return 0
  else
    result=$?
    case "$descriptor" in 6) exec 6>&- ;; 7) exec 7>&- ;; 8) exec 8>&- ;; 9) exec 9>&- ;; esac
    return "$result"
  fi
}

opu_execution_lock_file() {
  local path=$1 descriptor=$2 timeout=${3:-0} result
  opu_execution_open_lock_file "$path" "$descriptor" || return $?
  if opu_execution_flock "$descriptor" "$timeout" &&
     opu_execution_validate_lock "$path" "$descriptor"; then
    return 0
  else
    result=$?
    case "$descriptor" in 6) exec 6>&- ;; 7) exec 7>&- ;; 8) exec 8>&- ;; 9) exec 9>&- ;; esac
    return "$result"
  fi
}

opu_execution_host_lock() {
  local directory=${OPU_EXECUTION_LOCK_DIR:-/var/lib/oracle-patching-utility/locks}
  if [ "${TEST_MODE:-0}" = 1 ] && [ -z "${OPU_EXECUTION_LOCK_DIR:-}" ]; then
    directory="$PLAN_STATE_DIR/host-locks"
  fi
  opu_validate_absolute_path "$directory" execution-lock-directory || return
  [ ! -L "$directory" ] || { opu_error 'execution lock directory cannot be a symbolic link'; return 65; }
  mkdir -p "$directory" || return 73
  opu_execution_lock_file "$directory/host-mutation.lock" 7 || { opu_error 'another Oracle executor owns this host or its lock file is unsafe'; return 75; }
  # The mutation child keeps descriptor 7. If the supervising parent dies,
  # a still-running Oracle command must continue excluding another executor.
}

opu_execution_prepare_attempt() {
  local parent=$1 task_file=$2 task_id=$3 leaf
  TASK_RETRY_COUNT=$(jq -er '(.retry_count // 0) | select(type == "number" and . >= 0 and . <= 1000000 and floor == .)' "$task_file") || {
    opu_error 'task has an invalid attempt generation'; return 74;
  }
  leaf=$task_id
  [ "$TASK_RETRY_COUNT" -eq 0 ] || leaf="$task_id-retry$TASK_RETRY_COUNT"
  TASK_DIR="$parent/$leaf"
  [ ! -e "$TASK_DIR" ] && [ ! -L "$TASK_DIR" ] || {
    opu_error "execution evidence already exists for task attempt $leaf"; return 65;
  }
}

opu_execution_verify_attempt() {
  local claimed=$1
  jq -e --argjson generation "$TASK_RETRY_COUNT" '(.retry_count // 0) == $generation' "$claimed" >/dev/null || {
    opu_error 'claimed task generation changed before execution'; return 74;
  }
}

opu_execution_seal_attempt() {
  local file=$1
  jq --argjson generation "$TASK_RETRY_COUNT" '.retry_count=$generation' "$file" >"$file.attempt" &&
    mv "$file.attempt" "$file"
}
