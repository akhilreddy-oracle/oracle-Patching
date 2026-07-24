"""Inbound ITSM change-ticket gate (fail-closed).

When OPU_ITSM_REQUIRED=1, plan approval must present a change ticket that
exists in the local registry with state "approved". A missing, unreadable, or
malformed registry rejects the approval — a broken ITSM integration can never
silently approve work (S12 acceptance).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

TICKETS_ENV = "OPU_ITSM_TICKETS_FILE"
REQUIRED_ENV = "OPU_ITSM_REQUIRED"
DEFAULT_TICKETS_FILE = Path(__file__).resolve().parent / "var" / "change-tickets.json"


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
    if not path.is_file() or path.is_symlink():
        raise ItsmError(
            f"change-ticket registry missing at {path}; refusing approval (fail-closed)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ItsmError(
            f"change-ticket registry unreadable at {path}: {exc}; refusing approval (fail-closed)"
        ) from exc
    tickets = data.get("tickets") if isinstance(data, dict) else None
    if not isinstance(tickets, list):
        raise ItsmError(
            f"change-ticket registry malformed at {path} (expected {{\"tickets\": [...]}}); refusing approval (fail-closed)"
        )
    return [entry for entry in tickets if isinstance(entry, dict)]


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
