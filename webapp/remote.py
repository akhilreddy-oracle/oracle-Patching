"""SSH execution helpers shared across the webapp backend.

Every dynamic value interpolated into a remote command is shell-quoted via
shlex.quote — artifact_dir and similar fields can originate from a browser
form, and this is the one place that boundary is crossed.
"""
from __future__ import annotations

import json
import base64
import ipaddress
import re
import shlex
import subprocess
import tempfile
import time

DEFAULT_TIMEOUT_SECONDS = 45


class RemoteError(Exception):
    def __init__(self, error: str, message: str, stderr: str = ""):
        super().__init__(message)
        self.error = error
        self.message = message
        self.stderr = stderr

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message, "stderr": self.stderr}


def _ssh_argv(ssh_alias: str, remote_command: str, timeout: int) -> list[str]:
    if not isinstance(ssh_alias, str) or not ssh_alias or ssh_alias.startswith("-") or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in ssh_alias):
        raise RemoteError("invalid_input", "SSH target must be a nonempty hostname or configured alias, not an option")
    connect_timeout = min(timeout, 20)
    # Keepalives: long silent executes (OPatch apply, datapatch) otherwise get
    # torn down by idle NAT/firewall timeouts and surface as ssh exit 255.
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=8",
        "-o", "TCPKeepAlive=yes",
        ssh_alias,
        remote_command,
    ]


def run_remote_raw(
    ssh_alias: str, argv: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS, sudo: bool = False
) -> subprocess.CompletedProcess:
    """Run a fixed remote command and return the CompletedProcess (caller checks rc)."""
    if sudo:
        argv = ["sudo", "-n", *argv]
    remote_command = " ".join(shlex.quote(a) for a in argv)
    try:
        return subprocess.run(
            _ssh_argv(ssh_alias, remote_command, timeout),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s") from None
    except OSError as exc:
        raise RemoteError("ssh_unavailable", f"Could not start SSH for {ssh_alias}: {exc}") from exc


def run_remote(ssh_alias: str, argv: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS, sudo: bool = False) -> str:
    """Run a fixed remote command (as argv tokens) over SSH and return its stdout.

    sudo=True prepends `sudo -n` — the documented convention elsewhere in this
    repo (docs/STANDALONE_DATABASE_PATCH.md, docs/RECOVERY_PREPARATION.md) for
    invoking a specific opu-* binary as root over SSH from a non-root
    automation account. Never used to run an arbitrary shell string — argv[0]
    is always a fixed absolute binary path from trusted config, same as the
    rest of this module.

    Requires non-empty stdout (JSON-producing opu-* tools). For side-effect
    commands that legitimately print nothing on success (mkdir, tar, rm), use
    run_remote_checked instead.
    """
    result = run_remote_raw(ssh_alias, argv, timeout=timeout, sudo=sudo)
    remote_command = " ".join(shlex.quote(a) for a in (["sudo", "-n", *argv] if sudo else argv))

    # Collector exit 2 represents a completed blocked evaluation. Transport
    # failures and other exits cannot become evidence merely by printing JSON.
    if result.returncode not in {0, 2} or not result.stdout.strip():
        # SSH itself uses exit 255 for connect/banner failures; surface stderr so
        # callers can tell connection problems from a silent remote binary crash.
        err = result.stderr.strip()
        detail = f" stderr={err}" if err else ""
        raise RemoteError(
            "remote_command_failed",
            f"Command exited {result.returncode} on {ssh_alias} without a verified collector result: {remote_command}{detail}",
            stderr=err,
        )
    if result.returncode == 2:
        try:
            payload = json.loads(result.stdout)
            blocked = isinstance(payload, dict) and (payload.get("status") == "blocked"
                or isinstance(payload.get("artifact"), dict) and payload["artifact"].get("status") == "blocked")
        except ValueError:
            blocked = False
        if not blocked:
            raise RemoteError("remote_command_failed", "Collector exit 2 did not provide a blocked result", stderr=result.stderr.strip())
    return result.stdout


def run_remote_shell(
    ssh_alias: str, script: str, timeout: int = DEFAULT_TIMEOUT_SECONDS, sudo: bool = False
) -> subprocess.CompletedProcess:
    """Run a fixed bash script string remotely via `bash -c`.

    Only for internal, fully-templated scripts where every dynamic value has
    already been shlex-quoted by the caller (detached executor launch/poll).
    """
    inner = f"bash -c {shlex.quote(script)}"
    command = f"sudo -n {inner}" if sudo else inner
    try:
        return subprocess.run(_ssh_argv(ssh_alias, command, timeout), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s") from None
    except OSError as exc:
        raise RemoteError("ssh_unavailable", f"Could not start SSH for {ssh_alias}: {exc}") from exc


def pipe_remote(
    src_alias: str,
    src_argv: list[str],
    dst_alias: str,
    dst_argv: list[str],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    src_sudo: bool = False,
    dst_sudo: bool = False,
) -> subprocess.CompletedProcess:
    """Stream ``src_argv`` stdout on one host into ``dst_argv`` stdin on another.

    Data flows through the control plane (the hosts need no trust between
    them). Returns the destination CompletedProcess; ``stderr`` carries both
    sides' diagnostics, and a nonzero source exit is surfaced as a failure even
    when the destination command exited 0.
    """
    if src_sudo:
        src_argv = ["sudo", "-n", *src_argv]
    if dst_sudo:
        dst_argv = ["sudo", "-n", *dst_argv]
    src_cmd = " ".join(shlex.quote(a) for a in src_argv)
    dst_cmd = " ".join(shlex.quote(a) for a in dst_argv)
    # A pipe for producer stderr can fill while the consumer awaits producer
    # stdout. Spool diagnostics to disk while both processes stream normally.
    src = dst = None
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as source_errors:
        try:
            src = subprocess.Popen(_ssh_argv(src_alias, src_cmd, timeout), stdout=subprocess.PIPE,
                                   stderr=source_errors, stdin=subprocess.DEVNULL)
            dst = subprocess.Popen(_ssh_argv(dst_alias, dst_cmd, timeout), stdin=src.stdout,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert src.stdout is not None
            src.stdout.close()
            dst_out, dst_err = dst.communicate(timeout=max(0.001, deadline - time.monotonic()))
            src.wait(timeout=max(0.001, deadline - time.monotonic()))
            source_errors.seek(0, 2)
            source_errors.seek(max(0, source_errors.tell() - 65536))
            src_err = source_errors.read()
        except subprocess.TimeoutExpired:
            raise RemoteError("ssh_timeout", f"Stream from {src_alias} to {dst_alias} exceeded {timeout}s") from None
        except OSError as exc:
            raise RemoteError("ssh_unavailable", f"Could not start SSH stream: {exc}") from exc
        finally:
            for proc in (dst, src):
                if proc is not None:
                    if proc.poll() is None:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    proc.wait()
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
    stderr = dst_err.decode("utf-8", errors="replace")
    src_stderr = src_err.decode("utf-8", errors="replace").strip()
    if src_stderr:
        stderr = f"{stderr.rstrip()}\n[source {src_alias}] {src_stderr}".strip()
    returncode = dst.returncode
    if returncode == 0 and src.returncode != 0:
        returncode = src.returncode or 1
        stderr = f"{stderr}\n[source {src_alias}] exited {src.returncode}".strip()
    return subprocess.CompletedProcess(dst_argv, returncode, dst_out.decode("utf-8", errors="replace"), stderr)


def host_ips(ssh_alias: str, timeout: int = 30) -> list[str]:
    """IPv4 addresses configured on the host (for peer reachability checks)."""
    result = run_remote_shell(ssh_alias, "hostname -I 2>/dev/null || hostname -i", timeout=timeout)
    if result.returncode != 0:
        raise RemoteError("host_addresses_failed", f"Could not determine addresses for {ssh_alias}", result.stderr)
    ips = []
    for token in result.stdout.split():
        try:
            address = str(ipaddress.IPv4Address(token))
        except ipaddress.AddressValueError:
            continue
        if address not in ips:
            ips.append(address)
    return ips


def _ipv4(value: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError("address must be text")
        return str(ipaddress.IPv4Address(value))
    except ValueError as exc:
        raise RemoteError("invalid_input", f"not an IPv4 address: {value!r}") from exc


def reachable_from(ssh_alias: str, candidates: list[str], port: int = 22, timeout: int = 45) -> str | None:
    """First candidate IP the host can open a TCP connection to on ``port``."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise RemoteError("invalid_input", "port must be an integer between 1 and 65535")
    candidates = [_ipv4(value) for value in candidates]
    if not candidates:
        return None
    joined = " ".join(candidates)
    script = (
        f"for ip in {joined}; do if timeout 4 bash -c \"cat </dev/null >/dev/tcp/$ip/{int(port)}\" 2>/dev/null; "
        f"then echo \"$ip\"; exit 0; fi; done; exit 1"
    )
    result = run_remote_shell(ssh_alias, script, timeout=timeout)
    ip = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    return ip if result.returncode == 0 and ip in candidates else None


def direct_transfer(
    src_alias: str,
    src_argv: list[str],
    dst_alias: str,
    dst_argv: list[str],
    dst_ip: str,
    src_ips: list[str],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    src_sudo: bool = False,
    dst_sudo: bool = False,
) -> subprocess.CompletedProcess:
    """Stream ``src_argv`` stdout directly from the source host into
    ``dst_argv`` on the destination host over the hosts' own network.

    No standing trust is created: a throwaway ed25519 key is authorised on the
    destination only for the duration of the transfer, restricted with
    ``from=<source IPs>`` and a forced ``command=`` that runs exactly
    ``dst_argv`` (whatever the client asks for is ignored), with no pty, agent,
    port or X11 forwarding. The private key lives on the source host in a
    0600 temp file and is removed afterwards together with the authorised
    line. Falls back are the caller's decision (see ``pipe_remote``).
    """
    import os
    import uuid

    dst_ip = _ipv4(dst_ip)
    src_ips = [_ipv4(value) for value in src_ips]
    if src_sudo:
        src_argv = ["sudo", "-n", *src_argv]
    if dst_sudo:
        dst_argv = ["sudo", "-n", *dst_argv]
    if not src_ips:
        raise RemoteError("invalid_input", "direct transfer needs the source host's IPs for the from= restriction")
    marker = f"opu-transfer-{uuid.uuid4().hex}"
    identity = run_remote_shell(dst_alias, "id -un", timeout=30)
    dst_user = identity.stdout.strip()
    if identity.returncode != 0 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?", dst_user):
        raise RemoteError("transfer_setup_failed", "Destination SSH login identity could not be verified")
    # Read host keys through the already authenticated controller connection.
    keys = run_remote_shell(dst_alias,
        'set -eu; for key in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub; '
        'do if [ -r "$key" ]; then cat "$key"; fi; done', timeout=30)
    known_hosts = []
    if keys.returncode == 0:
        for line in keys.stdout.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
                continue
            try:
                blob = base64.b64decode(fields[1], validate=True)
                size = int.from_bytes(blob[:4], "big")
                if size < 1 or blob[4:4 + size].decode("ascii") != fields[0] or len(blob) <= 4 + size:
                    continue
            except (ValueError, UnicodeError):
                continue
            known_hosts.append(f"{dst_ip} {fields[0]} {fields[1]}")
    if not known_hosts:
        raise RemoteError("transfer_setup_failed", "No destination host keys could be authenticated; use the controller relay")
    forced = " ".join(shlex.quote(a) for a in dst_argv)
    with tempfile.TemporaryDirectory(prefix="opu-transfer-") as tmp:
        key_path = os.path.join(tmp, "key")
        try:
            gen = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", marker, "-f", key_path],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteError("keygen_failed", "Could not generate the temporary transfer key") from exc
        if gen.returncode != 0:
            raise RemoteError("keygen_failed", "ssh-keygen failed on the control plane", stderr=gen.stderr.strip())
        with open(key_path + ".pub", encoding="utf-8") as handle:
            pubkey = handle.read().strip()
        with open(key_path, "rb") as handle:
            private_key = handle.read()

    if any(character in forced for character in ("\n", "\r", "\x00")):
        raise RemoteError("invalid_input", "Transfer commands cannot contain control line breaks")
    escaped_forced = forced.replace("\\", "\\\\").replace('"', '\\"')
    options = (
        f'from="{",".join(src_ips)}",command="{escaped_forced}",'
        "no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding,no-user-rc"
    )
    auth_line = f"{options} {pubkey}"
    auth_guard = (
        'set -eu; umask 077; transfer_uid=$(id -u); '
        'if [ ! -e ~/.ssh ] && [ ! -L ~/.ssh ]; then mkdir -m 700 ~/.ssh; fi; '
        '[ -d ~/.ssh ] && [ ! -L ~/.ssh ]; [ "$(stat -c %u ~/.ssh)" = "$transfer_uid" ]; chmod 700 ~/.ssh; '
        'opu_transfer_safe_file() { '
        'if [ ! -e "$1" ] && [ ! -L "$1" ]; then (set -C; : >"$1"); fi; '
        '[ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %u:%h "$1")" = "$transfer_uid:1" ]; }; '
        'opu_transfer_safe_file ~/.ssh/opu-transfer.lock; exec 9>>~/.ssh/opu-transfer.lock; '
        '[ "$(stat -Lc %u:%h:%d:%i /proc/$$/fd/9)" = "$(stat -c %u:%h:%d:%i ~/.ssh/opu-transfer.lock)" ]; '
        'flock -x 9; opu_transfer_safe_file ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; ')
    install = (
        auth_guard +
        f"printf '%s\\n' {shlex.quote(auth_line)} >> ~/.ssh/authorized_keys"
    )
    remove = (auth_guard + 'temporary=$(mktemp ~/.ssh/.opu-transfer.XXXXXXXXXXXX); '
              'trap \'rm -f -- "$temporary"\' EXIT; '
              + f"awk -v marker={shlex.quote(marker)} '$NF != marker' ~/.ssh/authorized_keys >\"$temporary\"; "
              'chmod 600 "$temporary"; mv -- "$temporary" ~/.ssh/authorized_keys')
    source_directory = None
    authorization_attempted = False
    try:
        allocation = run_remote_shell(src_alias, "umask 077; mktemp -d /tmp/opu-transfer.XXXXXXXXXXXX", timeout=30)
        candidate = allocation.stdout.strip()
        if allocation.returncode != 0 or not re.fullmatch(r"/tmp/opu-transfer\.[A-Za-z0-9]{12}", candidate):
            raise RemoteError("transfer_setup_failed", "Could not reserve private source transfer storage")
        source_directory = candidate
        src_key, src_known = candidate + "/key", candidate + "/known_hosts"
        push_file(src_alias, src_key, private_key, timeout=45, private=True)
        push_file(src_alias, src_known, ("\n".join(known_hosts) + "\n").encode(), timeout=45, private=True)
        # A lost SSH reply may follow a completed append: always remove the key.
        authorization_attempted = True
        res = run_remote_shell(dst_alias, install, timeout=45)
        if res.returncode != 0:
            raise RemoteError("transfer_setup_failed", f"could not authorise transfer key on {dst_alias}", stderr=res.stderr.strip())
        src_cmd = " ".join(shlex.quote(a) for a in src_argv)
        ssh_to_dst = (
            f"ssh -F /dev/null -i {shlex.quote(src_key)} -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20 "
            f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={shlex.quote(src_known)} -o GlobalKnownHostsFile=/dev/null "
            f"-o ServerAliveInterval=15 -o ServerAliveCountMax=8 {shlex.quote(dst_user)}@{dst_ip} opu-forced-command"
        )
        # `set -o pipefail` so a failing tar is not masked by a clean ssh exit.
        script = f"set -o pipefail; {src_cmd} | {ssh_to_dst}"
        return run_remote_shell(src_alias, script, timeout=timeout)
    finally:
        cleanup_errors = []
        if authorization_attempted:
            try:
                cleaned = run_remote_shell(dst_alias, remove, timeout=30)
                if cleaned.returncode != 0:
                    cleanup_errors.append("destination authorization removal was not verified")
            except RemoteError as exc:
                cleanup_errors.append(f"destination authorization removal failed: {exc.error}")
        if source_directory:
            try:
                cleaned = run_remote_shell(src_alias,
                    f"set -eu; rm -f -- {shlex.quote(src_key)} {shlex.quote(src_known)}; rmdir -- {shlex.quote(source_directory)}", timeout=30)
                if cleaned.returncode != 0:
                    cleanup_errors.append("source private-key removal was not verified")
            except RemoteError as exc:
                cleanup_errors.append(f"source private-key removal failed: {exc.error}")
        if cleanup_errors:
            raise RemoteError("transfer_cleanup_failed", "Temporary transfer credentials require cleanup; do not retry or relay automatically",
                              stderr="; ".join(cleanup_errors) + f"; source={src_alias} directory={source_directory}; destination={dst_alias} authorization_marker={marker}")


def run_remote_checked(
    ssh_alias: str, argv: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS, sudo: bool = False
) -> str:
    """Run a remote command; raise only on nonzero exit. Empty stdout is allowed."""
    result = run_remote_raw(ssh_alias, argv, timeout=timeout, sudo=sudo)
    if result.returncode != 0:
        remote_command = " ".join(shlex.quote(a) for a in (["sudo", "-n", *argv] if sudo else argv))
        err = result.stderr.strip()
        detail = f" stderr={err}" if err else ""
        raise RemoteError(
            "remote_command_failed",
            f"Command exited {result.returncode} on {ssh_alias}: {remote_command}{detail}",
            stderr=err,
        )
    return result.stdout


def run_remote_json(ssh_alias: str, argv: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS, sudo: bool = False) -> dict:
    stdout = run_remote(ssh_alias, argv, timeout, sudo=sudo)
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError("collector response must be a JSON object")
        return payload
    except ValueError as exc:
        raise RemoteError("invalid_json", f"Command produced unparsable output: {exc}", stderr=stdout[-2000:]) from exc


def push_file(
    ssh_alias: str,
    remote_path: str,
    content: bytes,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sudo: bool = False,
    private: bool = False,
) -> None:
    """Write content to remote_path on the target host, creating parent dirs."""
    remote_dir = remote_path.rsplit("/", 1)[0] or "/"
    inner = f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)}"
    if private:
        inner = "umask 077; set -C; " + inner
    # sudo is required when sealing control-plane absolute paths onto a host
    # where the SSH user cannot create those directories (e.g. /Users/... on OL).
    command = f"sudo -n bash -c {shlex.quote(inner)}" if sudo else inner
    try:
        result = subprocess.run(_ssh_argv(ssh_alias, command, timeout), input=content, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s pushing {remote_path}") from None
    except OSError as exc:
        raise RemoteError("ssh_unavailable", f"Could not start SSH for {ssh_alias}: {exc}") from exc

    if result.returncode != 0:
        raise RemoteError(
            "push_file_failed",
            f"Failed to write {remote_path} on {ssh_alias}",
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )


def pull_file(
    ssh_alias: str,
    remote_path: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sudo: bool = False,
    max_bytes: int | None = None,
) -> bytes:
    """Read a remote file, optionally limiting transfer size before buffering it."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError("max_bytes must be a positive integer")
    reader = ["cat", "--"] if max_bytes is None else ["head", "-c", str(max_bytes + 1), "--"]
    argv = (["sudo", "-n"] if sudo else []) + reader + [remote_path]
    command = " ".join(shlex.quote(value) for value in argv)
    try:
        result = subprocess.run(_ssh_argv(ssh_alias, command, timeout), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s pulling {remote_path}") from None
    except OSError as exc:
        raise RemoteError("ssh_unavailable", f"Could not start SSH for {ssh_alias}: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        raise RemoteError(
            "pull_file_failed",
            f"Failed to read {remote_path} on {ssh_alias}",
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    if max_bytes is not None and len(result.stdout) > max_bytes:
        raise RemoteError("remote_file_too_large", f"File {remote_path} exceeds the {max_bytes}-byte read limit")
    return result.stdout
