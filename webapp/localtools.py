"""Wraps the pure-local jq-based pipeline tools (no SSH, no Oracle dependency).

opu-snapshot-reconcile, opu-procedure-validate, opu-compatibility-reconcile,
and opu-readiness-evaluate are deterministic JSON transforms with no Oracle
Home or root requirement — they run directly on the backend host, exactly as
the test suite (tests/readiness.sh etc.) already proves is safe on any
platform with bash/jq.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
DEFAULT_TIMEOUT_SECONDS = 60


class LocalToolError(Exception):
    def __init__(self, tool: str, message: str, stderr: str = ""):
        super().__init__(message)
        self.error = "local_tool_failed"
        self.tool = tool
        self.message = message
        self.stderr = stderr

    def to_json(self) -> dict:
        return {"error": self.error, "tool": self.tool, "message": self.message, "stderr": self.stderr}


def run_tool(name: str, args: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    binary = BIN / name
    if not binary.is_file():
        raise LocalToolError(name, f"No such tool: {binary}")
    argv = [str(binary), *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LocalToolError(name, f"{name} timed out after {timeout}s") from None

    # Only exit 2 with a blocked result is a completed negative evaluation.
    if result.returncode not in {0, 2} or not result.stdout.strip():
        raise LocalToolError(name, f"{name} exited {result.returncode} without a verified result", stderr=result.stderr.strip())

    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("tool response must be a JSON object")
        if result.returncode == 2 and payload.get("status") != "blocked":
            raise ValueError("exit 2 requires a blocked result")
        return payload
    except ValueError as exc:
        raise LocalToolError(name, f"{name} produced unparsable output: {exc}", stderr=result.stdout[-2000:]) from exc
