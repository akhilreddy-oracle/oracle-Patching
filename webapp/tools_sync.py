"""Keep the opu-* tool tree on every managed host identical to this checkout.

Every remote step runs ``<remote_root>/bin/opu-*`` on the host. When that
copy drifts from the control plane (older collector, missing fail-closed
check, missing ``-silent`` on rollback) the operator sees an opaque block
and has to log in to find out why. Before any remote tool runs we compare a
content fingerprint of the complete agent runtime with a stamp on the host and push
the tree when they differ. Idempotent, cheap (one short SSH when in sync),
and preserves host configuration and state under remote_root.
"""
from __future__ import annotations

import hashlib
import io
import shlex
import tarfile
import threading
import time
import uuid
from pathlib import Path

import remote

ROOT = Path(__file__).resolve().parent.parent
SYNC_DIRS = ("bin", "lib", "operations")
RUNTIME_MODULES = ("adapters.py", "durable.py", "agent_queue.py", "agent_enroll.py", "agent_worker.py")
STAMP_NAME = ".opu-tools-fingerprint"
# Re-verify a host at most this often per webapp process; the stamp check is
# one SSH round-trip, so this only trims chatter within a burst of steps.
RECHECK_SECONDS = 30

_lock = threading.Lock()
_verified_at: dict[str, tuple[float, str]] = {}
_host_locks: dict[str, threading.Lock] = {}


def _tree_files() -> list[Path]:
    files: list[Path] = []
    for name in SYNC_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                files.append(path)
    for name in RUNTIME_MODULES:
        path = ROOT / "webapp" / name
        if not path.is_file() or path.is_symlink():
            raise remote.RemoteError("incomplete_runtime", f"required runtime module is missing: {name}")
        files.append(path)
    return files


def _snapshot_files() -> list[tuple[str, int, bytes]]:
    """Capture each payload once; hashing and packing must use identical bytes."""
    return [(path.relative_to(ROOT).as_posix(), path.stat().st_mode & 0o777, path.read_bytes())
            for path in _tree_files()]


def local_fingerprint(snapshot=None) -> str:
    """sha256 over (relative path, executable bit, content) of every tool file."""
    snapshot = _snapshot_files() if snapshot is None else snapshot
    digest = hashlib.sha256()
    for rel, mode, content in snapshot:
        exe = "x" if mode & 0o111 else "-"
        digest.update(f"{rel}\0{exe}\0".encode())
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _build_tarball(snapshot=None) -> bytes:
    snapshot = _snapshot_files() if snapshot is None else snapshot
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for rel, mode, content in snapshot:
            info = tarfile.TarInfo(rel)
            info.mode, info.size = mode, len(content)
            archive.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _ensure_tools(ssh_alias: str, remote_root: str, sudo: bool, force: bool = False) -> dict:
    """Make the managed agent runtime match this checkout. Returns a summary."""
    if not remote_root.startswith("/") or remote_root.rstrip("/") == "" or ".." in Path(remote_root).parts:
        raise remote.RemoteError("invalid_host_config", f"remote_root must be absolute: {remote_root!r}")
    key = f"{ssh_alias}:{remote_root}"
    snapshot = _snapshot_files()
    want = local_fingerprint(snapshot)
    now = time.monotonic()
    with _lock:
        last = _verified_at.get(key)
    if not force and last is not None and last[1] == want and now - last[0] < RECHECK_SECONDS:
        return {"ssh_alias": ssh_alias, "synced": False, "fingerprint": want, "cached": True}

    stamp = f"{remote_root.rstrip('/')}/{STAMP_NAME}"
    have = ""
    if not force:
        try:
            have = remote.pull_file(ssh_alias, stamp, timeout=30, sudo=sudo).decode("utf-8", "replace").strip()
        except remote.RemoteError:
            have = ""
    if have == want:
        with _lock:
            _verified_at[key] = (now, want)
        return {"ssh_alias": ssh_alias, "synced": False, "fingerprint": want, "cached": False}

    python_helper = next((content.decode("utf-8") for path, _mode, content in snapshot
                          if path == "lib/opu/python.sh"), None)
    if python_helper is None:
        raise remote.RemoteError("incomplete_runtime", "required runtime helper is missing: lib/opu/python.sh")
    payload = _build_tarball(snapshot)
    remote_tar = f"/tmp/opu-tools-{want[:16]}-{uuid.uuid4().hex}.tgz"
    remote.push_file(ssh_alias, remote_tar, payload, timeout=180)
    q_root = shlex.quote(remote_root.rstrip("/"))
    q_tar = shlex.quote(remote_tar)
    q_stamp = shlex.quote(stamp)
    # Hold a host-side install lock and stage the complete runtime before replacing
    # directories. Write the fingerprint last; failed installs must be retried.
    # Python modules are replaced individually so webapp host config/state survive.
    directories = " ".join(SYNC_DIRS)
    modules = " ".join(shlex.quote(name) for name in RUNTIME_MODULES)
    script = (
        "set -eu; work=''; stage='initializing'; "
        f"archive={q_tar}; "
        "cleanup() { result=$?; "
        "if [ \"$result\" -ne 0 ]; then printf 'opu-tools: %s failed (exit %s)\\n' \"$stage\" \"$result\" >&2; fi; "
        "if [ -n \"$work\" ]; then rm -rf -- \"$work\"; fi; "
        "rm -f -- \"$archive\"; exit \"$result\"; }; trap cleanup EXIT;\n"
        + python_helper + "\n"
        "stage='checking Python 3.9 or newer'; runtime_python=$(opu_find_python); "
        f"stage='preparing runtime directory'; mkdir -p {q_root}; cd {q_root}; "
        "stage='checking install lock'; test ! -L .opu-tools-install.lock; "
        "[ ! -e .opu-tools-install.lock ] || test -f .opu-tools-install.lock; "
        "exec 8>>.opu-tools-install.lock; "
        "\"$runtime_python\" -c 'import os,stat; s=os.fstat(8); assert stat.S_ISREG(s.st_mode) and s.st_nlink == 1'; "
        "stage='waiting for install lock'; flock -w 120 8; "
        "stage='staging runtime'; work=$(mktemp -d .opu-tools-new.XXXXXX); "
        "tar -xzf \"$archive\" -C \"$work\"; "
        f"for d in {directories}; do test -d \"$work/$d\"; done; "
        f"for m in {modules}; do test -f \"$work/webapp/$m\"; done; "
        "stage='validating Python runtime imports'; "
        "\"$runtime_python\" -B -c 'import sys; sys.path.insert(0, sys.argv[1]); "
        "import agent_worker, agent_queue, agent_enroll' \"$work/webapp\"; "
        "stage='installing runtime'; test ! -L webapp; "
        f"rm -f {q_stamp}; "
        f"for d in {directories}; do "
        "if [ -e \"$d\" ] || [ -L \"$d\" ]; then mv \"$d\" \"$work/$d.old\"; fi; "
        "mv \"$work/$d\" \"$d\" || { [ ! -e \"$work/$d.old\" ] || mv \"$work/$d.old\" \"$d\"; exit 1; }; "
        "chmod -R a+rX \"$d\"; done; "
        "test ! -L webapp; mkdir -p webapp; "
        f"for m in {modules}; do mv \"$work/webapp/$m\" \"webapp/$m\"; chmod a+r \"webapp/$m\"; done; "
        f"stage='writing runtime fingerprint'; printf '%s\\n' {shlex.quote(want)} > \"$work/stamp\"; "
        f"chmod a+r \"$work/stamp\"; mv \"$work/stamp\" {q_stamp}"
    )
    result = remote.run_remote_shell(ssh_alias, script, timeout=180, sudo=sudo)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        if not detail:
            detail = f"Installer exited with code {result.returncode} without diagnostic output"
        raise remote.RemoteError(
            "tools_sync_failed",
            f"Failed to install opu tools on {ssh_alias} under {remote_root} "
            f"(exit {result.returncode}): {detail[-700:]}",
            stderr=detail,
        )
    with _lock:
        _verified_at[key] = (time.monotonic(), want)
    return {"ssh_alias": ssh_alias, "synced": True, "fingerprint": want, "bytes": len(payload), "cached": False}


def ensure_tools(ssh_alias: str, remote_root: str, sudo: bool, force: bool = False) -> dict:
    key = f"{ssh_alias}:{remote_root}"
    with _lock:
        host_lock = _host_locks.setdefault(key, threading.Lock())
    with host_lock:
        return _ensure_tools(ssh_alias, remote_root, sudo, force)


def ensure_host_tools(host: dict, force: bool = False) -> list[dict]:
    """Sync every SSH alias a host entry can execute tools on (RAC: all nodes)."""
    aliases: list[str] = []
    for node in host.get("nodes") or []:
        alias = node.get("ssh_alias") if isinstance(node, dict) else None
        if alias and alias not in aliases:
            aliases.append(str(alias))
    top = host.get("ssh_alias")
    if top and top not in aliases:
        aliases.insert(0, str(top))
    if not aliases:
        raise remote.RemoteError("invalid_host_config", f"Host {host.get('id')!r} has no ssh_alias")
    remote_root = str(host.get("remote_root") or "")
    sudo = bool(host.get("sudo"))
    return [ensure_tools(alias, remote_root, sudo, force=force) for alias in aliases]
