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

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ". " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "; . " + shlex.quote(str(ROOT / "lib/opu/execution.sh")) + "; "


def seal(value, field):
    value = dict(value)
    value[field] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


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
    task.update(status="running", claimed_by="worker")
    write(plan_dir / "tasks" / "task1.json", task)
    env = dict(os.environ, OPU_PLAN_STATE_DIR=str(state), TEST_MODE="1", PLAN_STATE_DIR=str(state), OPU_EXECUTION_LOCK_DIR=str(base / "host"))
    (base / "host").mkdir()
    owner = pwd.getpwuid(os.getuid()).pw_name
    targets = {
        "host": (base / "host" / "host-mutation.lock", ["bash", "-c", SOURCE + "opu_execution_host_lock"]),
        "registry": (state / "target-reservations.lock", [str(ROOT / "bin/opu-patch-plan"), "complete", "--plan-id", "lock-test", "--task-id", "task1", "--actor", "worker", "--status", "succeeded", "--evidence-sha256", "a" * 64]),
        "artifact": (base / ".opu-media-patch.lock", [str(ROOT / "bin/opu-artifact-stage"), "--artifact", str(base / "patch"), "--owner", owner, "--from-tar-stdin"]),
    }
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

print("execution lock files: passed (hardlinks, symlinks, FIFOs, contents, opened-inode replacement)")
