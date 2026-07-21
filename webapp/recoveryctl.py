"""Wraps opu-database-recovery-prepare: create, analyze, approve, authorize,
execute, reconcile, status.

Unlike opu-patch-plan, this tool's state (and the fake Oracle home it
operates against) must live wherever the fixture was built for that
request_id — recovery-prepare does real backup work itself, it isn't a pure
state machine dispatching to a separate executor. Every call resolves its
environment from the fixture recorded under RECOVERY_DIR/<request_id>/.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import recovery_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "bin" / "opu-database-recovery-prepare"
# Must live under /tmp, /private/tmp, /var/folders, or /private/var/folders —
# opu-database-recovery-prepare's constrained_test_mode() (a real safety
# belt, not a convenience check) refuses to activate TEST_MODE for any
# OPU_RECOVERY_PREP_STATE_DIR outside those prefixes, precisely so a fixture
# path can never look like a real deployment location. Confirmed by hitting
# this directly: bin/opu-database-recovery-prepare:33-39.
RECOVERY_DIR = Path("/tmp/opu-webapp-recovery-fixtures")
DEFAULT_TIMEOUT_SECONDS = 120
EXECUTE_TIMEOUT_SECONDS = 300  # execute does real work even against the fixture


class RecoveryError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.error = "recovery_tool_failed"
        self.message = message
        self.stderr = stderr

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message, "stderr": self.stderr}


def _env_for(request_id: str) -> dict:
    fixture_dir = RECOVERY_DIR / request_id
    env = os.environ.copy()
    env.update(recovery_fixtures.env_for(fixture_dir))
    return env


def _run(request_id: str, args: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict | None:
    argv = [str(TOOL), *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_env_for(request_id))
    except subprocess.TimeoutExpired:
        raise RecoveryError(f"opu-database-recovery-prepare timed out after {timeout}s") from None

    # Several subcommands (analyze, and rejections like self-approval) exit
    # nonzero with a real, structured JSON result rather than a crash — only
    # treat this as a failure when there's genuinely no stdout to parse.
    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RecoveryError(f"opu-database-recovery-prepare produced unparsable output: {exc}", stderr=result.stdout[-2000:]) from exc

    if result.returncode == 0:
        return None

    raise RecoveryError(f"opu-database-recovery-prepare exited {result.returncode} with no output", stderr=result.stderr.strip())


def create_testmode_demo(request_id: str, requester: str) -> dict:
    fixture_dir = RECOVERY_DIR / request_id
    fx = recovery_fixtures.build(fixture_dir, request_id)
    args = [
        "create", "--request-id", request_id, "--requester", requester,
        "--snapshot", str(fx["snapshot"]), "--policy", str(fx["policy"]),
        "--database", "ORCL", "--backup-parent", str(fx["backup_parent"]),
        "--window-start", fx["window_start"], "--window-end", fx["window_end"],
    ]
    return _run(request_id, args)


def analyze(request_id: str) -> dict:
    return _run(request_id, ["analyze", "--request-id", request_id])


def approve(request_id: str, actor: str, approval_ticket: str) -> dict:
    return _run(request_id, ["approve", "--request-id", request_id, "--actor", actor, "--approval-ticket", approval_ticket])


def authorize(request_id: str, actor: str) -> dict:
    return _run(request_id, ["authorize", "--request-id", request_id, "--actor", actor])


def execute(request_id: str, actor: str) -> dict:
    return _run(request_id, ["execute", "--request-id", request_id, "--actor", actor], timeout=EXECUTE_TIMEOUT_SECONDS)


def reconcile(request_id: str, actor: str) -> dict:
    return _run(request_id, ["reconcile", "--request-id", request_id, "--actor", actor])


def status(request_id: str) -> dict:
    return _run(request_id, ["status", "--request-id", request_id])


def recovery_evidence_path(request_id: str) -> str | None:
    """Returns the completed request's recovery-evidence.json path, if any."""
    current = status(request_id)
    return (current.get("result") or {}).get("recovery_evidence", {}).get("path")


def list_requests() -> list[dict]:
    if not RECOVERY_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(RECOVERY_DIR.iterdir()):
        if not entry.is_dir():
            continue
        try:
            summaries.append(status(entry.name))
        except RecoveryError as exc:
            summaries.append({"request_id": entry.name, "state": "unreadable", "error": exc.to_json()})
    return summaries
