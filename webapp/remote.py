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
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", ssh_alias, remote_command]


def run_remote(ssh_alias: str, argv: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run a fixed remote command (as argv tokens) over SSH and return its stdout."""
    remote_command = " ".join(shlex.quote(a) for a in argv)
    try:
        result = subprocess.run(_ssh_argv(ssh_alias, remote_command, timeout), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("ssh_timeout", f"No response from {ssh_alias} within {timeout}s") from None

    # Several opu-* tools exit nonzero (commonly 2) to signal a valid, fully
    # formed "blocked"/has-findings JSON result, not a crash (see
    # docs/HIGH_ASSURANCE_ACCEPTANCE.md: "blocked" is a correct safety
    # outcome). Only treat this as a real failure when there's no stdout to
    # parse.
    if not result.stdout.strip():
        raise RemoteError(
            "remote_command_failed",
            f"Command exited {result.returncode} on {ssh_alias} with no output: {remote_command}",
            stderr=result.stderr.strip(),
        )
    return result.stdout


def run_remote_json(ssh_alias: str, argv: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    stdout = run_remote(ssh_alias, argv, timeout)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RemoteError("invalid_json", f"Command produced unparsable output: {exc}", stderr=stdout[-2000:]) from exc


def push_file(ssh_alias: str, remote_path: str, content: bytes, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Write content to remote_path on the target host, creating parent dirs."""
    remote_dir = remote_path.rsplit("/", 1)[0] or "/"
    command = f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)}"
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
