"""Outbound notifications: local event audit log + webhook fan-out.

Fail-safe by design: emit() never raises to callers. Every event is appended
to a local audit log (var/events.jsonl) whether or not webhooks are
configured; webhook delivery failures are appended to a dead-letter log
instead of propagating. Outbound integration failure can therefore never
block or alter a control-plane action. No retries in the lab slice.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from fnmatch import fnmatch
from pathlib import Path
import runtime_paths

VAR_DIR = runtime_paths.state_dir()
CONFIG_ENV = "OPU_NOTIFICATIONS_FILE"
EVENTS_ENV = "OPU_EVENTS_FILE"
DEADLETTER_ENV = "OPU_NOTIFICATIONS_DEADLETTER_FILE"
DEFAULT_CONFIG_FILE = VAR_DIR / "notifications.json"
DEFAULT_EVENTS_FILE = VAR_DIR / "events.jsonl"
DEFAULT_DEADLETTER_FILE = VAR_DIR / "notifications-deadletter.jsonl"
WEBHOOK_TIMEOUT_SECONDS = 5


def config_path() -> Path:
    override = (os.environ.get(CONFIG_ENV) or "").strip()
    return Path(override) if override else DEFAULT_CONFIG_FILE


def events_path() -> Path:
    override = (os.environ.get(EVENTS_ENV) or "").strip()
    return Path(override) if override else DEFAULT_EVENTS_FILE


def deadletter_path() -> Path:
    override = (os.environ.get(DEADLETTER_ENV) or "").strip()
    return Path(override) if override else DEFAULT_DEADLETTER_FILE


def load_config() -> dict:
    """Return {"webhooks": [...]}; a missing or corrupt config means no webhooks."""
    path = config_path()
    if not path.is_file() or path.is_symlink():
        return {"webhooks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"webhooks": []}
    webhooks = []
    for entry in data.get("webhooks") or []:
        url = str(entry.get("url") or "").strip()
        patterns = [str(p).strip() for p in (entry.get("events") or []) if str(p).strip()]
        if url and patterns:
            webhooks.append({"url": url, "events": patterns})
    return {"webhooks": webhooks}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_jsonl(path: Path, record: dict) -> None:
    # O_APPEND single-write keeps concurrent appenders from interleaving lines.
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _post_webhook(url: str, record: dict) -> None:
    body = json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS):
        pass


def _matches(event: str, patterns: list[str]) -> bool:
    return any(fnmatch(event, pattern) for pattern in patterns)


def emit(event: str, payload: dict) -> None:
    """Record an event and fan out to subscribed webhooks. Never raises."""
    try:
        record = {"event": event, "payload": payload, "emitted_at": _utc_now()}
        _append_jsonl(events_path(), record)
        for hook in load_config()["webhooks"]:
            if not _matches(event, hook["events"]):
                continue
            try:
                _post_webhook(hook["url"], record)
            except Exception as exc:  # noqa: BLE001 - dead-lettered, never propagated
                try:
                    _append_jsonl(deadletter_path(), {
                        "event": event,
                        "url": hook["url"],
                        "error": str(exc),
                        "failed_at": _utc_now(),
                        "record": record,
                    })
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 - outbound integration must never block callers
        pass


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def tail_events(limit: int = 100) -> list[dict]:
    events = _read_jsonl(events_path())
    return events[-max(int(limit), 1):]


def event_counts() -> dict:
    by_event: dict[str, int] = {}
    events = _read_jsonl(events_path())
    for record in events:
        name = str(record.get("event") or "unknown")
        by_event[name] = by_event.get(name, 0) + 1
    return {"total": len(events), "by_event": by_event}


def deadletter_count() -> int:
    return len(_read_jsonl(deadletter_path()))
