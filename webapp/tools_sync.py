"""Keep the opu-* tool tree on every managed host identical to this checkout.

Managed launches use a retained ``<remote_root>/.opu-runtimes/<digest>``
generation returned by synchronization. A complete generation is verified and
published atomically, never overwritten. Legacy code and all mutable state
remain at their existing paths, so updating cannot change an already-started
collector or executor. Direct legacy CLI installations are not silently upgraded.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import tarfile
import threading
import time
import uuid
from pathlib import Path

import remote
import runtime_install

ROOT = Path(__file__).resolve().parent.parent
SYNC_DIRS = ("bin", "lib", "operations")
RUNTIME_MODULES = runtime_install.RUNTIME_MODULES
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
        if not base.is_dir() or base.is_symlink():
            raise remote.RemoteError("incomplete_runtime", f"required runtime directory is missing: {name}")
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
    snapshot = [(path.relative_to(ROOT).as_posix(), path.stat().st_mode & 0o777, path.read_bytes())
                for path in _tree_files()]
    if not runtime_install.REQUIRED_FILES <= {path for path, _mode, _data in snapshot}:
        raise remote.RemoteError("incomplete_runtime", "required runtime source files are missing")
    return snapshot


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


def _generation_root(remote_root: str, fingerprint: str) -> str:
    if not isinstance(remote_root, str) or not remote_root.startswith("/") or remote_root.rstrip("/") == "" or ".." in Path(remote_root).parts:
        raise remote.RemoteError("invalid_host_config", "remote_root must be an absolute deployment directory")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise remote.RemoteError("invalid_runtime", "runtime fingerprint must be a full SHA-256 digest")
    return f"{remote_root.rstrip('/')}/.opu-runtimes/{fingerprint}"


def runtime_for_host(host: dict, receipts: list[dict], ssh_alias: str | None = None) -> dict:
    """Resolve only the exact generation attested by this operation's sync."""
    alias = ssh_alias or host["ssh_alias"]
    if not isinstance(receipts, list):
        raise remote.RemoteError("invalid_runtime", "missing runtime synchronization receipts")
    matches = [r for r in receipts if isinstance(r, dict) and r.get("ssh_alias") == alias]
    if len(matches) != 1:
        raise remote.RemoteError("invalid_runtime", "missing or ambiguous runtime synchronization receipt")
    result = matches[0]
    expected = _generation_root(host["remote_root"], result.get("fingerprint"))
    if result.get("runtime_root") != expected:
        raise remote.RemoteError("invalid_runtime", "runtime receipt does not belong to the configured deployment")
    return result


def tool_path(host: dict, receipts: list[dict], relative: str, ssh_alias: str | None = None) -> str:
    if not isinstance(relative, str) or not re.fullmatch(r"bin/opu-[a-z0-9-]+", relative):
        raise remote.RemoteError("invalid_runtime", "invalid managed runtime entrypoint")
    return runtime_for_host(host, receipts, ssh_alias)["runtime_root"] + "/" + relative


def _ensure_tools(ssh_alias: str, remote_root: str, sudo: bool, force: bool = False) -> dict:
    """Verify/install one immutable generation and return its exact path."""
    snapshot = _snapshot_files()
    want = local_fingerprint(snapshot)
    generation = _generation_root(remote_root, want)
    key = f"{ssh_alias}:{bool(sudo)}:{remote_root}"
    now = time.monotonic()
    with _lock:
        last = _verified_at.get(key)
    if not force and last is not None and last[1] == want and now - last[0] < RECHECK_SECONDS:
        return {"ssh_alias": ssh_alias, "synced": False, "fingerprint": want, "runtime_root": generation, "cached": True}
    captured = {path: content for path, _mode, content in snapshot}
    try:
        python_helper = captured["lib/opu/python.sh"].decode("utf-8")
        installer = captured["webapp/runtime_install.py"].decode("utf-8")
    except KeyError as error:
        raise remote.RemoteError("incomplete_runtime", "required runtime installer is missing") from error

    def invoke(archive=None):
        cleanup = ""
        if archive is not None:
            cleanup = f"archive={shlex.quote(archive)}; trap 'rm -f -- \"$archive\"' EXIT; "
        command = ['--root', remote_root, '--fingerprint', want]
        if archive is not None:
            command += ['--archive', archive]
        script = ("set -eu; " + cleanup + "\n" + python_helper + "\n"
                  "runtime_python=$(opu_find_python); "
                  + '\"$runtime_python\" -B -c ' + shlex.quote(installer) + ' '
                  + ' '.join(shlex.quote(arg) for arg in command))
        return remote.run_remote_shell(ssh_alias, script, timeout=180, sudo=sudo)

    def verified_result(response):
        if response.returncode != 0:
            detail = (response.stderr or response.stdout).strip()[-2000:]
            if not detail:
                detail = f"Installer exited with code {response.returncode} without diagnostic output"
            raise remote.RemoteError("tools_sync_failed",
                f"Failed to prepare opu runtime on {ssh_alias} under {remote_root} (exit {response.returncode}): {detail[-700:]}",
                stderr=detail)
        try:
            result = json.loads(response.stdout)
            if (not isinstance(result, dict) or result.get("fingerprint") != want
                    or result.get("runtime_root") != generation or type(result.get("synced")) is not bool):
                raise ValueError("runtime receipt mismatch")
        except (ValueError, TypeError) as error:
            raise remote.RemoteError("invalid_runtime", "installer did not return the requested generation receipt") from error
        with _lock:
            _verified_at[key] = (time.monotonic(), want)
        return {**result, "ssh_alias": ssh_alias, "cached": False}

    if not force:
        checked = invoke()
        if checked.returncode != 66:  # Only a missing generation permits installation.
            return verified_result(checked)
    payload = _build_tarball(snapshot)
    remote_tar = f"/tmp/opu-tools-{want[:16]}-{uuid.uuid4().hex}.tgz"
    remote.push_file(ssh_alias, remote_tar, payload, timeout=180)
    return {**verified_result(invoke(remote_tar)), "bytes": len(payload)}


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
