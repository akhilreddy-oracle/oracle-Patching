#!/usr/bin/env bash
# ITSM gate + outbound notification unit checks (no HTTP server, no SSH).
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-integrations.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

export OPU_NOTIFICATIONS_FILE="$TMP/notifications.json"
export OPU_EVENTS_FILE="$TMP/events.jsonl"
export OPU_NOTIFICATIONS_DEADLETTER_FILE="$TMP/deadletter.jsonl"
export OPU_ITSM_TICKETS_FILE="$TMP/change-tickets.json"
export PYTHONPATH="$ROOT/webapp${PYTHONPATH:+:$PYTHONPATH}"

python3 - <<PY
import json, os, time
from pathlib import Path
from unittest.mock import patch

import itsm
import notifications
import pipeline_runner

events_file = Path(os.environ["OPU_EVENTS_FILE"])
dead_file = Path(os.environ["OPU_NOTIFICATIONS_DEADLETTER_FILE"])

# emit without webhook config: audit log written, nothing raised
notifications.emit("plan.created", {"plan_id": "p1"})
events = [json.loads(l) for l in events_file.read_text().splitlines()]
assert len(events) == 1
assert events[0]["event"] == "plan.created"
assert events[0]["payload"] == {"plan_id": "p1"}
assert events[0]["emitted_at"]

# webhook to an unreachable port: dead-letter written, no exception
Path(os.environ["OPU_NOTIFICATIONS_FILE"]).write_text(json.dumps({
    "webhooks": [{"url": "http://127.0.0.1:9/hook", "events": ["plan.*", "run.failed"]}]
}))
notifications.emit("plan.approved", {"plan_id": "p1"})
dead = [json.loads(l) for l in dead_file.read_text().splitlines()]
assert len(dead) == 1
assert dead[0]["event"] == "plan.approved"
assert dead[0]["error"]

# non-matching event skips the webhook but still hits the audit log
notifications.emit("recovery.created", {"request_id": "r1"})
assert len(dead_file.read_text().splitlines()) == 1
assert len(events_file.read_text().splitlines()) == 3

# a failed run emits run.failed without breaking the run record
pipeline_runner.RUNS_DIR = events_file.parent / "runs"
def boom(_record):
    raise RuntimeError("boom")
record = pipeline_runner.start_run("plan", "plan:p1:test", boom)
deadline = time.time() + 10
while time.time() < deadline:
    if any('"run.failed"' in l for l in events_file.read_text().splitlines()):
        break
    time.sleep(0.05)
assert record.status == "failed", record.status
tail = notifications.tail_events(1)
assert tail[0]["event"] == "run.failed"
assert tail[0]["payload"]["key"] == "plan:p1:test"

# ITSM: disabled by default, enabled via env
assert not itsm.itsm_enabled()
os.environ["OPU_ITSM_REQUIRED"] = "1"
assert itsm.itsm_enabled()

Path(os.environ["OPU_ITSM_TICKETS_FILE"]).write_text(json.dumps({
    "tickets": [
        {"ticket": "CHG00123", "state": "approved"},
        {"ticket": "CHG00999", "state": "draft"},
    ]
}))
itsm.validate_ticket("CHG00123")
for bad in ("CHG00999", "CHG-missing", ""):
    try:
        itsm.validate_ticket(bad)
        raise SystemExit(f"ticket {bad!r} must not validate")
    except itsm.ItsmError as exc:
        assert exc.status == 403
        assert exc.to_json()["error"] == "itsm_rejected"

# An approved first entry must not hide a conflicting duplicate or a malformed
# registry. Exercise the actual gate, not a normalized replacement fixture.
registry = Path(os.environ["OPU_ITSM_TICKETS_FILE"])
approved = {"ticket": "CHG00123", "state": "approved"}
for entries in ([approved, {"ticket": "CHG00123", "state": "revoked"}],
                [approved, {"ticket": " CHG00123 ", "state": "draft"}],
                [approved, None], [approved, {"ticket": "other", "state": []}]):
    registry.write_text(json.dumps({"tickets": entries}))
    try:
        itsm.validate_ticket("CHG00123")
        raise SystemExit("ambiguous or malformed registry must fail closed")
    except itsm.ItsmError:
        pass
    assert itsm.list_tickets() == []
registry.write_text('{"tickets":[{"ticket":"CHG00123","state":"revoked","state":"approved"}]}')
try:
    itsm.validate_ticket("CHG00123")
    raise SystemExit("duplicate JSON authority fields must fail closed")
except itsm.ItsmError:
    pass
registry.unlink()
os.mkfifo(registry)
try:
    itsm.validate_ticket("CHG00123")
    raise SystemExit("nonregular registry must fail closed without blocking")
except itsm.ItsmError:
    pass
finally:
    registry.unlink()

# corrupt registry with enforcement on: fail-closed
Path(os.environ["OPU_ITSM_TICKETS_FILE"]).write_text("{not json")
try:
    itsm.validate_ticket("CHG00123")
    raise SystemExit("corrupt registry must fail closed")
except itsm.ItsmError:
    pass

# missing registry with enforcement on: fail-closed
os.unlink(os.environ["OPU_ITSM_TICKETS_FILE"])
try:
    itsm.validate_ticket("CHG00123")
    raise SystemExit("missing registry must fail closed")
except itsm.ItsmError:
    pass
assert itsm.list_tickets() == []

# metrics counting sanity from a synthetic events file
events_file.write_text("".join(
    json.dumps({"event": name, "payload": {}, "emitted_at": "t"}) + "\n"
    for name in ("plan.created", "plan.created", "run.failed")
))
counts = notifications.event_counts()
assert counts["total"] == 3
assert counts["by_event"] == {"plan.created": 2, "run.failed": 1}
# plan.approved and the run.failed emission both matched the unreachable webhook
assert notifications.deadletter_count() == 2
assert [e["event"] for e in notifications.tail_events(2)] == ["plan.created", "run.failed"]
run_counts = pipeline_runner.status_counts()
assert run_counts.get("failed", 0) >= 1

# Persisted events and outbound bodies cannot carry credential-shaped values;
# an opaque webhook path must not leak into failed-delivery diagnostics.
hook_secret = "private-hook-path"
Path(os.environ["OPU_NOTIFICATIONS_FILE"]).write_text(json.dumps({"webhooks": [
    {"url": "http://127.0.0.1/" + hook_secret, "events": ["private.*"]}]}))
with patch.object(notifications, "_post_webhook", side_effect=RuntimeError(hook_secret)) as post:
    notifications.emit("private.failed", {"password": "private-password", "message": "Authorization: Bearer private-token"})
    delivered = json.dumps(post.call_args.args[1])
    assert "private-password" not in delivered and "private-token" not in delivered
persisted = events_file.read_text() + dead_file.read_text()
for secret in (hook_secret, "private-password", "private-token"):
    assert secret not in persisted
assert events_file.stat().st_mode & 0o777 == 0o600
assert dead_file.stat().st_mode & 0o777 == 0o600

# Unsafe log targets fail without altering a victim or waiting for a FIFO peer.
victim = events_file.parent / "log-victim"
victim.write_text("keep")
for kind in ("symlink", "hardlink", "fifo"):
    events_file.unlink()
    if kind == "symlink": events_file.symlink_to(victim)
    elif kind == "hardlink": os.link(victim, events_file)
    else: os.mkfifo(events_file)
    notifications.emit("private.failed", {})
    assert victim.read_text() == "keep"
events_file.unlink()
for malformed in ([], {"webhooks": {}}, {"webhooks": [None, {"url": "x", "events": "plan.*"}]}):
    Path(os.environ["OPU_NOTIFICATIONS_FILE"]).write_text(json.dumps(malformed))
    assert notifications.load_config() == {"webhooks": []}

print("integrations test passed")
PY
