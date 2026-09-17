"""Outbound notifications: local event audit log + webhook fan-out.

Fail-safe by design: emit() never raises to callers. Every event is appended
to a local audit log (var/events.jsonl) whether or not webhooks are
configured; webhook delivery failures are appended to a dead-letter log
instead of propagating. Outbound integration failure can therefore never
block or alter a control-plane action. No retries in the lab slice.
"""
from __future__ import annotations

import json
import fcntl
import os
import stat
import time
import urllib.request
from urllib.parse import urlsplit
from fnmatch import fnmatch
from pathlib import Path
import runtime_paths
from diagnostics import redacted

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
    if not isinstance(data, dict) or not isinstance(data.get("webhooks", []), list):
        return {"webhooks": []}
    webhooks = []
    for entry in data.get("webhooks", []):
        if (not isinstance(entry, dict) or not isinstance(entry.get("url"), str)
                or not isinstance(entry.get("events"), list)
                or not all(isinstance(pattern, str) for pattern in entry["events"])):
            continue
        url = str(entry.get("url") or "").strip()
        patterns = [str(p).strip() for p in (entry.get("events") or []) if str(p).strip()]
        if url and patterns:
            webhooks.append({"url": url, "events": patterns})
    return {"webhooks": webhooks}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_jsonl(path: Path, record: dict) -> None:
    # Reject unsafe destinations before writing, including FIFOs that otherwise
    # block callers and hardlinks/symlinks that modify an unrelated file.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
            raise ValueError("Unsafe notification log")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("Notification log is busy") from None
                time.sleep(0.01)
        pending = memoryview(line.encode("utf-8"))
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("Notification log write was incomplete")
            pending = pending[written:]
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
        record = redacted({"event": event, "payload": payload, "emitted_at": _utc_now()})
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
                        # Webhook paths/query strings can be bearer credentials.
                        # Keep only the destination hostname and error class.
                        "destination_host": urlsplit(hook["url"]).hostname,
                        "error": type(exc).__name__,
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
