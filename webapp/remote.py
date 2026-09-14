"""SSH execution helpers shared across the webapp backend.

Every dynamic value interpolated into a remote command is shell-quoted via
shlex.quote — artifact_dir and similar fields can originate from a browser
form, and this is the one place that boundary is crossed.
"""
from __future__ import annotations

import json
import shlex
import subprocess

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

    # Several opu-* tools exit nonzero (commonly 2) to signal a valid, fully
    # formed "blocked"/has-findings JSON result, not a crash (see
    # docs/HIGH_ASSURANCE_ACCEPTANCE.md: "blocked" is a correct safety
    # outcome). Only treat this as a real failure when there's no stdout to
    # parse.
    if not result.stdout.strip():
        # SSH itself uses exit 255 for connect/banner failures; surface stderr so
        # callers can tell connection problems from a silent remote binary crash.
        err = result.stderr.strip()
        detail = f" stderr={err}" if err else ""
        raise RemoteError(
            "remote_command_failed",
            f"Command exited {result.returncode} on {ssh_alias} with no output: {remote_command}{detail}",
            stderr=err,
        )
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
    src = subprocess.Popen(
        _ssh_argv(src_alias, src_cmd, timeout), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
    )
    try:
        dst = subprocess.Popen(
            _ssh_argv(dst_alias, dst_cmd, timeout), stdin=src.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except Exception:
        src.kill()
        raise
    # The destination owns the read end now; closing ours lets EOF propagate.
    assert src.stdout is not None
    src.stdout.close()
    try:
        dst_out, dst_err = dst.communicate(timeout=timeout)
        _, src_err = src.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        for proc in (src, dst):
            try:
                proc.kill()
            except OSError:
                pass
        raise RemoteError("ssh_timeout", f"Stream from {src_alias} to {dst_alias} exceeded {timeout}s") from None
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
    ips = [tok for tok in result.stdout.split() if tok.count(".") == 3 and tok.replace(".", "").isdigit()]
    return ips


def reachable_from(ssh_alias: str, candidates: list[str], port: int = 22, timeout: int = 45) -> str | None:
    """First candidate IP the host can open a TCP connection to on ``port``."""
    if not candidates:
        return None
    for ip in candidates:
        if not (ip.count(".") == 3 and ip.replace(".", "").isdigit()):
            raise RemoteError("invalid_input", f"not an IPv4 address: {ip!r}")
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
    import tempfile
    import uuid

    if src_sudo:
        src_argv = ["sudo", "-n", *src_argv]
    if dst_sudo:
        dst_argv = ["sudo", "-n", *dst_argv]
    if not src_ips:
        raise RemoteError("invalid_input", "direct transfer needs the source host's IPs for the from= restriction")
    marker = f"opu-transfer-{uuid.uuid4().hex[:12]}"
    dst_user = run_remote_shell(dst_alias, "id -un", timeout=30).stdout.strip() or "root"
    forced = " ".join(shlex.quote(a) for a in dst_argv)
    with tempfile.TemporaryDirectory(prefix="opu-transfer-") as tmp:
        key_path = os.path.join(tmp, "key")
        gen = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", marker, "-f", key_path],
            capture_output=True, text=True, timeout=60,
        )
        if gen.returncode != 0:
            raise RemoteError("keygen_failed", "ssh-keygen failed on the control plane", stderr=gen.stderr.strip())
        with open(key_path + ".pub", encoding="utf-8") as handle:
            pubkey = handle.read().strip()
        with open(key_path, "rb") as handle:
            private_key = handle.read()

    options = (
        f'from="{",".join(src_ips)}",command="{forced.replace(chr(34), chr(92) + chr(34))}",'
        "no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding,no-user-rc"
    )
    auth_line = f"{options} {pubkey}"
    install = (
        "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; "
        f"printf '%s\\n' {shlex.quote(auth_line)} >> ~/.ssh/authorized_keys"
    )
    remove = f"sed -i.opu-bak {shlex.quote(f'/{marker}$/d')} ~/.ssh/authorized_keys && rm -f ~/.ssh/authorized_keys.opu-bak"
    src_key = f"/tmp/{marker}.key"
    src_known = f"/tmp/{marker}.known_hosts"
    installed = False
    try:
        res = run_remote_shell(dst_alias, install, timeout=45)
        if res.returncode != 0:
            raise RemoteError("transfer_setup_failed", f"could not authorise transfer key on {dst_alias}", stderr=res.stderr.strip())
        installed = True
        push_file(src_alias, src_key, private_key, timeout=45)
        run_remote_shell(src_alias, f"chmod 600 {shlex.quote(src_key)}", timeout=30)
        src_cmd = " ".join(shlex.quote(a) for a in src_argv)
        ssh_to_dst = (
            f"ssh -i {shlex.quote(src_key)} -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20 "
            f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={shlex.quote(src_known)} "
            f"-o ServerAliveInterval=15 -o ServerAliveCountMax=8 {shlex.quote(dst_user)}@{dst_ip} opu-forced-command"
        )
        # `set -o pipefail` so a failing tar is not masked by a clean ssh exit.
        script = f"set -o pipefail; {src_cmd} | {ssh_to_dst}"
        return run_remote_shell(src_alias, script, timeout=timeout)
    finally:
        try:
            run_remote_shell(src_alias, f"rm -f {shlex.quote(src_key)} {shlex.quote(src_known)}", timeout=30)
        except RemoteError:
            pass
        if installed:
            try:
                run_remote_shell(dst_alias, remove, timeout=30)
            except RemoteError:
                pass


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
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RemoteError("invalid_json", f"Command produced unparsable output: {exc}", stderr=stdout[-2000:]) from exc


def push_file(
    ssh_alias: str,
    remote_path: str,
    content: bytes,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sudo: bool = False,
) -> None:
    """Write content to remote_path on the target host, creating parent dirs."""
    remote_dir = remote_path.rsplit("/", 1)[0] or "/"
    inner = f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)}"
    # sudo is required when sealing control-plane absolute paths onto a host
    # where the SSH user cannot create those directories (e.g. /Users/... on OL).
    command = f"sudo -n bash -c {shlex.quote(inner)}" if sudo else inner
    try:
        result = subprocess.run(_ssh_argv(ssh_alias, command, timeout), input=content, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s pushing {remote_path}") from None

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
) -> bytes:
    """Read remote_path from the target host (optionally via sudo -n cat)."""
    command = f"sudo -n cat {shlex.quote(remote_path)}" if sudo else f"cat {shlex.quote(remote_path)}"
    try:
        result = subprocess.run(_ssh_argv(ssh_alias, command, timeout), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s pulling {remote_path}") from None
    if result.returncode != 0 or not result.stdout:
        raise RemoteError(
            "pull_file_failed",
            f"Failed to read {remote_path} on {ssh_alias}",
            stderr=result.stderr.decode("utf-8", errors="replace").strip(),
        )
    return result.stdout
