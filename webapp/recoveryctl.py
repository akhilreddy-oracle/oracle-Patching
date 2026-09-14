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
import re
import subprocess
from pathlib import Path

import recovery_fixtures
import evidence

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
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RecoveryError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.error = "recovery_tool_failed"
        self.message = message
        self.stderr = stderr

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message, "stderr": self.stderr}


def _fixture_path(request_id: str) -> Path:
    if not isinstance(request_id, str) or not _ID_RE.fullmatch(request_id):
        raise RecoveryError("request_id contains unsupported characters")
    root = RECOVERY_DIR.resolve()
    candidate = RECOVERY_DIR / request_id
    if candidate.is_symlink() or candidate.resolve().parent != root:
        raise RecoveryError("request_id escapes the recovery fixture root")
    return candidate


def _env_for(request_id: str) -> dict:
    fixture_dir = _fixture_path(request_id)
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


def create_testmode_demo(request_id: str, requester: str, *, host_id: str | None = None) -> dict:
    fixture_dir = _fixture_path(request_id)
    if not isinstance(requester, str) or not _ID_RE.fullmatch(requester):
        raise RecoveryError("requester contains unsupported characters")
    if host_id is not None:
        evidence.validate_host_id(host_id)
    fx = recovery_fixtures.build(fixture_dir, request_id)
    (fixture_dir / "webapp-metadata.json").write_text(json.dumps({"host_id": host_id}) + "\n")
    args = [
        "create", "--request-id", request_id, "--requester", requester,
        "--snapshot", str(fx["snapshot"]), "--policy", str(fx["policy"]),
        "--database", "ORCL", "--backup-parent", str(fx["backup_parent"]),
        "--window-start", fx["window_start"], "--window-end", fx["window_end"],
    ]
    result = _run(request_id, args)
    return {**(result or {}), "host_id": host_id}


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
    fixture_dir = _fixture_path(request_id)
    result = _run(request_id, ["status", "--request-id", request_id]) or {}
    host_id = None
    metadata = fixture_dir / "webapp-metadata.json"
    if metadata.is_file() and not metadata.is_symlink():
        try:
            host_id = json.loads(metadata.read_text()).get("host_id")
            if host_id is not None:
                evidence.validate_host_id(host_id)
        except (ValueError, TypeError, AttributeError, OSError):
            host_id = None
    return {**result, "host_id": host_id}


def recovery_evidence_path(request_id: str) -> str | None:
    """Returns the completed request's recovery-evidence.json path, if any."""
    current = status(request_id)
    return (current.get("result") or {}).get("recovery_evidence", {}).get("path")


def list_requests(host_id: str | None = None) -> list[dict]:
    if not RECOVERY_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(RECOVERY_DIR.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            request = status(entry.name)
            if host_id is None or request.get("host_id") == host_id:
                summaries.append(request)
        except RecoveryError as exc:
            summaries.append({"request_id": entry.name, "state": "unreadable", "error": exc.to_json()})
    return summaries
