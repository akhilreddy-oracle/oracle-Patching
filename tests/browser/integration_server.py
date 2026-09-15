#!/usr/bin/env python3
"""Actual HTTP controller and executors in a disposable, offline fixture tree.

No production handler/controller methods are mocked. Oracle binaries use
TEST_MODE shims. One explicitly simulated managed host uses confined local
transport; every other remote target fails closed. No configuration or saved
state is copied from the working application's webapp.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PORT = 18766  # Deliberately fixed; never reuse the operator's port or URL.
DENIED_PROGRAMS = {"ssh", "scp", "sftp", "rsync", "curl", "wget", "sudo"}


def main():
    # Refuse to change any controller configuration until the isolated port is
    # known to be vacant. Playwright also sets reuseExistingServer=False.
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", PORT))
    probe.close()
    with tempfile.TemporaryDirectory(prefix="opu-browser-backend-", dir="/tmp") as temporary:
        base = Path(temporary).resolve()
        checkout = base / "source"

        def ignore(directory, names):
            return [name for name in names if name in {"var", "__pycache__", "node_modules", ".git"}
                    or name.startswith(".env") or name.endswith((".pyc", ".log"))
                    or Path(directory) == ROOT / "webapp" and name.endswith(".json")]

        for name in ("bin", "lib", "operations", "contracts", "scripts", "webapp", "tests"):
            shutil.copytree(ROOT / name, checkout / name, ignore=ignore, symlinks=True)
        (checkout / "webapp/hosts.json").write_text('{"hosts": []}\n')
        private = checkout / "webapp/var"
        private.mkdir(mode=0o700)
        principals = private / "principals.json"
        # Synthetic fixture credentials only, each bound to a different role.
        principals.write_text(json.dumps({"principals": [
            {"actor": role, "roles": [role], "token_sha256": hashlib.sha256(f"integration-{role}-token".encode()).hexdigest()}
            for role in ("requester", "approver", "operator")]}))
        principals.chmod(0o600)
        deny_bin = base / "deny-bin"
        deny_bin.mkdir()
        for name in DENIED_PROGRAMS:
            target = deny_bin / name
            target.write_text("#!/bin/sh\nprintf '%s\\n' 'Remote and privileged commands are forbidden in browser integration fixtures' >&2\nexit 97\n")
            target.chmod(0o700)
        for name in list(os.environ):
            if name.startswith(("OPU_", "ORACLE_", "TNS_")) or name in {"SSH_AUTH_SOCK", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}:
                os.environ.pop(name)
        os.environ.update({
            "OPU_WEBAPP_PORT": str(PORT), "OPU_WEBAPP_RBAC": "1",
            "OPU_WEBAPP_PRINCIPALS_FILE": str(principals), "OPU_PRODUCTION_MODE": "0",
            "OPU_TEST_MODE": "1", "OPU_TEST_KERNEL_NAME": "Linux",
            "OPU_TEST_KERNEL_RELEASE": "5.15.0-test", "OPU_TEST_ARCHITECTURE": "x86_64",
            "OPU_FS_ROOT": str(checkout / "tests/fixtures/oracle-linux-8"),
            "OPU_STATE_DIR": str(base / "agent"), "OPU_TARGET_LOCK_DIR": str(base / "target-locks"),
            "PATH": str(deny_bin) + os.pathsep + os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(base / "tmp"),
        })
        (base / "tmp").mkdir()
        os.chdir(checkout)
        sys.path.insert(0, str(checkout / "webapp"))
        import server
        import recoveryctl
        import remote

        recoveryctl.RECOVERY_DIR = base / "recovery-fixtures"

        def forbidden(*args, **kwargs):
            raise RuntimeError("Remote transport is forbidden in browser integration fixtures")

        # All ordinary transport entry points and subprocess command generation
        # are blocked before serving any request, including absolute SSH paths.
        for name in ("_ssh_argv", "run_remote_raw", "run_remote_shell", "pipe_remote", "run_remote_checked"):
            if hasattr(remote, name):
                setattr(remote, name, forbidden)

        from connected_fixture import install
        install(base, checkout)

        def audit(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyaddr", "socket.gethostbyname"}:
                forbidden()
            if event == "subprocess.Popen" and Path(str(args[0])).name in DENIED_PROGRAMS:
                forbidden()

        sys.addaudithook(audit)
        # HTTPServer uses reverse DNS merely to populate its display name.
        # Keep this offline and deterministic; the actual bind stays loopback.
        socket.getfqdn = lambda _name="": "127.0.0.1"
        # Raising out of serve_forever runs TemporaryDirectory cleanup. Native
        # fixtures are bounded; successful tests wait for every run to finish.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: sys.exit(0))
        print("Integration boundary: actual HTTP/RBAC/controller; fixture Oracle; simulated managed-host transport; no live hosts", flush=True)
        server.main()


if __name__ == "__main__":
    main()
