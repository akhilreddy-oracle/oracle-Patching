"""Inbound ITSM change-ticket gate (fail-closed).

When OPU_ITSM_REQUIRED=1, plan approval must present a change ticket that
exists in the local registry with state "approved". A missing, unreadable, or
malformed registry rejects the approval — a broken ITSM integration can never
silently approve work (S12 acceptance).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
import runtime_paths

TICKETS_ENV = "OPU_ITSM_TICKETS_FILE"
REQUIRED_ENV = "OPU_ITSM_REQUIRED"
DEFAULT_TICKETS_FILE = runtime_paths.state_dir() / "change-tickets.json"


class ItsmError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.error = "itsm_rejected"
        self.message = message
        self.status = status

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message}


def itsm_enabled() -> bool:
    return (os.environ.get(REQUIRED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def tickets_path() -> Path:
    override = (os.environ.get(TICKETS_ENV) or "").strip()
    return Path(override) if override else DEFAULT_TICKETS_FILE


def _load_registry() -> list[dict]:
    path = tickets_path()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                raise ValueError("unsafe registry")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversized registry")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate registry key")
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError, RecursionError) as exc:
        raise ItsmError(
            f"change-ticket registry unreadable or unsafe at {path}; refusing approval (fail-closed)"
        ) from exc
    tickets = data.get("tickets") if isinstance(data, dict) else None
    if not isinstance(tickets, list):
        raise ItsmError(
            f"change-ticket registry malformed at {path} (expected {{\"tickets\": [...]}}); refusing approval (fail-closed)"
        )
    seen = set()
    for entry in tickets:
        if (not isinstance(entry, dict) or not isinstance(entry.get("ticket"), str)
                or not entry["ticket"].strip() or not isinstance(entry.get("state"), str)
                or not entry["state"].strip() or entry["ticket"].strip() in seen):
            raise ItsmError("change-ticket registry has malformed or ambiguous entries; refusing approval (fail-closed)")
        seen.add(entry["ticket"].strip())
    return tickets


def list_tickets() -> list[dict]:
    """Non-raising view for the read API: empty when the registry is unusable."""
    try:
        registry = _load_registry()
    except ItsmError:
        return []
    return [
        {
            "ticket": str(entry.get("ticket") or ""),
            "state": str(entry.get("state") or ""),
            "window_start": entry.get("window_start"),
            "window_end": entry.get("window_end"),
        }
        for entry in registry
        if str(entry.get("ticket") or "").strip()
    ]


def validate_ticket(ticket: str) -> None:
    """Raise ItsmError unless ticket exists in the registry with state approved."""
    wanted = str(ticket or "").strip()
    if not wanted:
        raise ItsmError("approval_ticket is required when ITSM enforcement is enabled")
    for entry in _load_registry():
        if str(entry.get("ticket") or "").strip() != wanted:
            continue
        state = str(entry.get("state") or "").strip().lower()
        if state == "approved":
            return
        raise ItsmError(f"change ticket {wanted!r} is in state {state!r}, not approved")
    raise ItsmError(f"change ticket {wanted!r} not found in the change-ticket registry")
