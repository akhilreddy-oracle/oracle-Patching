#!/usr/bin/env python3
"""Unsafe lock files must be refused without truncation or FIFO blocking."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ". " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; . " + shlex.quote(str(ROOT / "lib/opu/execution.sh")) + "; "


def seal(value, field):
    value = dict(value)
    value[field] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def lock_function(tool, name):
    """Load just the real lock function, so no adapter or SSH work can run."""
    script = (ROOT / "bin" / tool).read_text()
    start = script.index(name + "() {")
    end = script.index("\n}", start) + 2
    return script[start:end]


with tempfile.TemporaryDirectory(prefix="opu-lock-files-") as temporary:
    base = Path(temporary).resolve()
    state = base / "controller"
    plan_dir = state / "plans" / "lock-test"
    plan = seal({"schema_version": "1.0", "plan_id": "lock-test", "nodes": ["node1"]}, "plan_sha256")
    write(plan_dir / "plan.json", plan)
    (plan_dir / "state").write_text("running\n")
    task = seal({"schema_version": "1.0", "task_id": "task1", "plan_id": "lock-test", "plan_sha256": plan["plan_sha256"],
                 "stage": "precheck", "node": "node1", "adapter": "fixture", "authorization_sha256": "a" * 64,
                 "authorization_record_sha256": "b" * 64}, "task_definition_sha256")
    # Reach registry-lock validation with an actually valid task owner.
    task.update(status="running", claimed_by="worker", lease_expires_epoch=int(time.time()) + 3600)
    write(plan_dir / "tasks" / "task1.json", task)
    env = dict(os.environ, OPU_PLAN_STATE_DIR=str(state), TEST_MODE="1", PLAN_STATE_DIR=str(state), OPU_EXECUTION_LOCK_DIR=str(base / "host"))
    (base / "host").mkdir()
    (state / "locks").mkdir()
    owner = pwd.getpwuid(os.getuid()).pw_name
    targets = {
        "host": (base / "host" / "host-mutation.lock", ["bash", "-c", SOURCE + "opu_execution_host_lock"]),
        "registry": (state / "target-reservations.lock", [str(ROOT / "bin/opu-patch-plan"), "complete", "--plan-id", "lock-test", "--task-id", "task1", "--actor", "worker", "--status", "succeeded", "--evidence-sha256", "a" * 64]),
        "artifact": (base / ".opu-media-patch.lock", [str(ROOT / "bin/opu-artifact-stage"), "--artifact", str(base / "patch"), "--owner", owner, "--from-tar-stdin"]),
        # Exercise the production flock branch on every development platform;
        # only the actual kernel lock command is replaced, not file handling.
        "journal": (state / "locks" / "journal-test.lock", ["bash", "-c", SOURCE +
                    ". " + shlex.quote(str(ROOT / "lib/opu/journal.sh")) +
                    '; flock() { return 0; }; OPU_STATE_DIR="$1"; OPU_LOCK_WAIT_SECONDS=1; opu_acquire_lock journal-test',
                    "test", str(state)]),
    }
    (state / "jobs" / "job-test").mkdir(parents=True)
    targets["job"] = (state / "jobs" / "job-test" / ".lock", ["bash", "-c", SOURCE +
                      ". " + shlex.quote(str(ROOT / "lib/opu/jobs.sh")) +
                      '; flock() { return 0; }; OPU_JOB_STATE_DIR="$1"; opu_job_lock job-test',
                      "test", str(state)])
    for tool in ("opu-database-single-instance-patch", "opu-database-ojvm-patch", "opu-database-out-of-place-patch",
                 "opu-database-rac-node-patch", "opu-database-rac-node-rollback", "opu-grid-node-patch", "opu-grid-opatchauto-patch",
                 "opu-database-rolling-patch", "opu-grid-rolling-patch"):
        directory = base / tool
        directory.mkdir()
        rolling = "rolling" in tool
        name = "acquire_lock" if rolling else "acquire_executor_lock"
        command = SOURCE + 'flock() { return 0; }; die() { opu_error "$*"; exit 65; }; ' + lock_function(tool, name)
        command += '\nTEST_MODE=1; RUN_DIR="$1"; NODE_RUN_DIR="$1"; LOCAL_NODE=node1; ' + name + ' "$1"'
        targets[tool] = (directory / (".lock" if rolling else "executor.lock"), ["bash", "-c", command, "test", str(directory)])
    for label, (lock_path, command) in targets.items():
        for kind in ("hardlink", "symlink", "fifo"):
            sentinel = base / f"{label}-{kind}-sentinel"
            expected = b"existing data must survive lock acquisition\n"
            sentinel.write_bytes(expected)
            if kind == "hardlink":
                os.link(sentinel, lock_path)
            elif kind == "symlink":
                lock_path.symlink_to(sentinel)
            else:
                os.mkfifo(lock_path)
            result = subprocess.run(command, env=env, input="", capture_output=True, text=True, timeout=10)
            assert result.returncode != 0, (label, kind, result.stdout)
            assert "unsafe" in result.stderr and "lock" in result.stderr, (label, kind, result.stderr)
            assert sentinel.read_bytes() == expected, (label, kind, "sentinel was changed")
            lock_path.unlink()

    # A legitimate pre-existing lock also retains its bytes, even when the
    # lock is successfully obtained (the descriptor exists only for flock).
    lock_path = base / "regular.lock"
    lock_path.write_bytes(b"preserve regular lock contents\n")
    result = subprocess.run(["bash", "-c", SOURCE + 'opu_execution_lock_file "$1" 6', "test", str(lock_path)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert lock_path.read_bytes() == b"preserve regular lock contents\n"

    for label, (call_lock, call_command) in targets.items():
        if label in ("host", "registry", "artifact"):
            continue  # These command-level cases also require their own inputs.
        call_lock.write_bytes(b"preserve existing lock contents\n")
        result = subprocess.run(call_command, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, (label, result.stderr)
        assert call_lock.read_bytes() == b"preserve existing lock contents\n", label

    # Simulate replacement after opening: pathname checks alone would accept
    # the new regular file, but fstat must reject the old held inode.
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        replacement = base / "replacement.lock"
        replacement.write_bytes(b"replacement data\n")
        replacement.replace(lock_path)
        result = subprocess.run(["bash", "-c", SOURCE + 'opu_execution_validate_lock "$1" "$2"', "test", str(lock_path), str(descriptor)], env=env, pass_fds=(descriptor,), capture_output=True, text=True, timeout=10)
        assert result.returncode != 0 and "inode" in result.stderr, result.stderr
        assert lock_path.read_bytes() == b"replacement data\n"
    finally:
        os.close(descriptor)

    # Persistent Oracle services must not inherit fd7, while the supervising
    # shell and ordinary stop/probe commands must retain the host exclusion.
    service_doubles = r'''
check_fd() {
    local state=closed
    if { : >&7; } 2>/dev/null; then state=open; fi
    [ "$state" = "$1" ] || { echo "unexpected host lock inheritance: $state, expected $1" >&2; return 91; }
}
sqlplus_fixed() {
    local input
    input=$(cat)
    if [[ "$input" == *startup* ]]; then check_fd closed; else check_fd open; fi
}
as_owner() {
    if [[ " $* " == *' start '* ]]; then check_fd closed; else check_fd open; fi
}
wait_for_pmon_state() { return 0; }
die() { echo "$*" >&2; exit 65; }
'''
    for tool, startup_names in (
        ("opu-database-single-instance-patch", ("startup_database",)),
        ("opu-database-ojvm-patch", ("startup_upgrade_database", "startup_normal_database")),
        ("opu-database-out-of-place-patch", ("startup_database",)),
    ):
        definitions = "\n".join(lock_function(tool, name) for name in ("listener_action", *startup_names))
        command = 'set -e; ' + definitions + "\n" + service_doubles
        command += '\nORACLE_SID=fixture; ORACLE_OWNER=fixture; ORACLE_HOME_TARGET="$1"; LISTENER_NAME=fixture; exec 7<>"$1/host.lock"; '
        command += "; ".join(name + ' "$1"' for name in startup_names) + "; check_fd open; "
        listener_args = '"$1" ' if tool == "opu-database-out-of-place-patch" else ""
        command += "; ".join("listener_action " + listener_args + action + "; check_fd open" for action in ("start", "stop", "status"))
        result = subprocess.run(["bash", "-c", command, "fixture", str(base)], env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, (tool, result.stderr)

print("execution lock files: passed (hardlinks, symlinks, FIFOs, contents, opened-inode replacement, service inheritance)")
