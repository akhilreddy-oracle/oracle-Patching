"""Install or verify a retained native runtime generation; stdlib only.

This module is sent as captured source to the target's selected interpreter.
It never replaces legacy code or an already published generation, and never
changes plan, launch, recovery or queue state.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

GENERATION_DIR = ".opu-runtimes"
MANIFEST = ".opu-generation.json"
RUNTIME_MODULES = ("adapters.py", "durable.py", "runtime_paths.py", "diagnostics.py", "agent_queue.py", "agent_enroll.py", "agent_worker.py", "runtime_install.py")
# Complete native package contract. New helper dependencies belong here too.
NATIVE_FILES = """
bin/opu-agent
bin/opu-agent-enroll
bin/opu-agent-work-pull
bin/opu-agent-work-run
bin/opu-artifact-inspect
bin/opu-artifact-stage
bin/opu-compatibility-reconcile
bin/opu-database-lock-recover
bin/opu-database-ojvm-patch
bin/opu-database-ojvm-rollback
bin/opu-database-out-of-place-patch
bin/opu-database-out-of-place-switchback
bin/opu-database-rac-node-patch
bin/opu-database-rac-node-rollback
bin/opu-database-recovery-prepare
bin/opu-database-rolling-patch
bin/opu-database-single-instance-patch
bin/opu-database-single-instance-rollback
bin/opu-dataguard-evaluate
bin/opu-dataguard-observe
bin/opu-dataguard-orchestrate
bin/opu-dataguard-plan-order
bin/opu-dataguard-reinstate
bin/opu-dataguard-reinstate-gate
bin/opu-dataguard-switchover
bin/opu-dataguard-switchover-gate
bin/opu-extjob-provenance-inspect
bin/opu-grid-node-patch
bin/opu-grid-node-rollback
bin/opu-grid-opatchauto-patch
bin/opu-grid-opatchauto-rollback
bin/opu-grid-recovery-evidence-collect
bin/opu-grid-rolling-patch
bin/opu-jobctl
bin/opu-opatch-compatibility-collect
bin/opu-opatch-upgrade
bin/opu-patch-plan
bin/opu-procedure-validate
bin/opu-readiness-evaluate
bin/opu-recovery-evidence-collect
bin/opu-snapshot-reconcile
bin/opu-topology-discover
lib/opu/artifact_media.py
lib/opu/common.sh
lib/opu/dataguard.sh
lib/opu/dataguard_parse.jq
lib/opu/discovery.sh
lib/opu/execution.sh
lib/opu/extjob_provenance.py
lib/opu/jobs.sh
lib/opu/journal.sh
lib/opu/lock_recovery.py
lib/opu/oracle_inventory.sh
lib/opu/python.sh
lib/opu/recovery_capacity.py
lib/opu/recovery_evidence.sh
lib/opu/registry.sh
operations/discovery/host.sh
operations/discovery/oracle_homes.sh
""".split()
REQUIRED_FILES = {*NATIVE_FILES, *("webapp/" + name for name in RUNTIME_MODULES)}


class InstallError(Exception):
    def __init__(self, message, code=65):
        super().__init__(message)
        self.code = code


def require(condition, message):
    if not condition:
        raise InstallError(message)


def regular_bytes(path, *, readonly=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe runtime file")
        require(not readonly or not info.st_mode & 0o222, "published runtime file is writable")
        require(info.st_size <= 32 * 1024 * 1024, "oversized runtime file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(32 * 1024 * 1024 + 1)
        require(len(data) <= 32 * 1024 * 1024, "oversized runtime file")
        return data, info.st_mode
    finally:
        os.close(fd)


def relative_file(name):
    require(isinstance(name, str) and bool(name), "missing runtime path")
    path = Path(name)
    require(not path.is_absolute() and path.as_posix() == name
            and all(p not in {"", ".", ".."} for p in name.split("/"))
            and len(path.parts) >= 2 and path.parts[0] in {"bin", "lib", "operations", "webapp"},
            "invalid runtime member path")
    return path


def fingerprint(files):
    result = hashlib.sha256()
    for name, executable, content in files:
        result.update((name + "\0" + ("x" if executable else "-") + "\0").encode())
        result.update(content)
        result.update(b"\0")
    return result.hexdigest()


def complete(names):
    require(REQUIRED_FILES <= names and any(name.startswith("operations/") for name in names),
            "incomplete native runtime payload")


def directory(path, *, readonly=False):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "unsafe runtime directory")
    require(not info.st_mode & (0o222 if readonly else 0o022), "runtime directory is writable by an unsafe principal")


def verify(generation, expected):
    directory(generation, readonly=True)
    raw, _ = regular_bytes(generation / MANIFEST, readonly=True)
    manifest = json.loads(raw)
    require(isinstance(manifest, dict) and manifest.get("fingerprint") == expected
            and isinstance(manifest.get("files"), list) and manifest["files"], "invalid generation manifest")
    files, seen = [], set()
    for item in manifest["files"]:
        require(isinstance(item, dict) and type(item.get("executable")) is bool, "invalid runtime manifest member")
        name = item.get("path")
        relative = relative_file(name)
        require(name not in seen, "duplicate runtime member")
        seen.add(name)
        for parent in relative.parents:
            if parent != Path("."):
                directory(generation / parent, readonly=True)
        content, mode = regular_bytes(generation / relative, readonly=True)
        require(bool(mode & 0o111) == item["executable"], "runtime executable mode changed")
        files.append((name, item["executable"], content))
    complete(seen)
    require(fingerprint(files) == expected, "published runtime content changed; refusing to replace it")
    # A new module beside the verified files could alter Python import behavior.
    actual = set()
    for current, dirs, names in os.walk(generation, followlinks=False):
        for child in dirs:
            directory(Path(current) / child, readonly=True)
        actual.update((Path(current) / name).relative_to(generation).as_posix() for name in names)
    require(actual == seen | {MANIFEST}, "published runtime has unexpected files")


def stage_archive(archive_path, parent, expected):
    work = Path(tempfile.mkdtemp(prefix=".opu-tools-new.", dir=parent))
    try:
        payload, _ = regular_bytes(archive_path)
        import io
        entries, seen = [], set()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            require(0 < len(members) <= 4096, "invalid runtime member count")
            total = 0
            for member in members:
                relative = relative_file(member.name)
                require(member.isfile() and member.name not in seen and 0 <= member.size <= 32 * 1024 * 1024,
                        "unsafe or duplicate archive member")
                total += member.size
                require(total <= 128 * 1024 * 1024, "oversized runtime archive")
                seen.add(member.name)
                with archive.extractfile(member) as stream:
                    content = stream.read(member.size + 1)
                require(len(content) == member.size, "incomplete runtime member")
                entries.append((member.name, bool(member.mode & 0o111), content))
        complete(seen)
        require(fingerprint(entries) == expected, "uploaded runtime differs from captured source")
        for name, executable, content in entries:
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o555 if executable else 0o444)
        manifest = {"fingerprint": expected, "files": [
            {"path": name, "executable": executable} for name, executable, _ in entries]}
        with (work / MANIFEST).open("x") as stream:
            json.dump(manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        (work / MANIFEST).chmod(0o444)
        for current, _, _ in os.walk(work, topdown=False):
            Path(current).chmod(0o555)
        verify(work, expected)
        checked = subprocess.run([sys.executable, "-I", "-B", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import agent_worker, agent_queue, agent_enroll",
            str(work / "webapp")], capture_output=True, text=True, timeout=30)
        require(checked.returncode == 0, "native runtime Python imports failed: " + checked.stderr[-1000:])
        return work
    except BaseException:
        remove_stage(work)
        raise


def remove_stage(work):
    for current, dirs, _ in os.walk(work, followlinks=False):
        Path(current).chmod(0o700)
    shutil.rmtree(work)


def install(root, expected, archive=None):
    require(re.fullmatch(r"[a-f0-9]{64}", expected) is not None, "invalid runtime fingerprint")
    require(root.is_absolute() and root == root.resolve(), "deployment root must be canonical and absolute")
    if not root.exists() and archive is None:
        raise InstallError("runtime generation is not installed", 66)
    root.mkdir(parents=True, exist_ok=True)
    directory(root)
    lock_path = root / ".opu-tools-install.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    work = None
    try:
        opened = os.fstat(fd)
        require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and not opened.st_mode & 0o022,
                "unsafe runtime install lock")
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InstallError("runtime installation is busy", 75)
                time.sleep(0.1)
        current = lock_path.lstat()
        require((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino), "runtime lock pathname changed")
        parent = root / GENERATION_DIR
        if not parent.exists() and archive is None:
            raise InstallError("runtime generation is not installed", 66)
        parent.mkdir(mode=0o755, exist_ok=True)
        directory(parent)
        generation = parent / expected
        if generation.exists() or generation.is_symlink():
            verify(generation, expected)
            return {"fingerprint": expected, "runtime_root": str(generation), "synced": False}
        if archive is None:
            raise InstallError("runtime generation is not installed", 66)
        # Staging is outside the reserved generation layout; runtime imports
        # correctly reject a .opu-runtimes child that is not a full digest.
        work = stage_archive(archive, root, expected)
        # macOS requires write permission when moving a directory between
        # parents. Move under the destination parent using a private name, then
        # seal it before the final same-parent atomic publication.
        work.chmod(0o755)
        pending = parent / work.name
        require(not pending.exists() and not pending.is_symlink(), "staging runtime name collision")
        work.rename(pending)
        work = pending
        work.chmod(0o555)
        verify(work, expected)
        # No moving pointer: every caller receives this exact retained path.
        work.rename(generation)
        work = None
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return {"fingerprint": expected, "runtime_root": str(generation), "synced": True}
    finally:
        if work is not None:
            remove_stage(work)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    try:
        result = install(args.root, args.fingerprint, args.archive)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (InstallError, OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print("opu-tools: " + str(error), file=sys.stderr)
        return error.code if isinstance(error, InstallError) else 65


if __name__ == "__main__":
    raise SystemExit(main())
