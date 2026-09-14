#!/usr/bin/env bash
# Lab pull-agent queue + optional RBAC unit checks (no SSH).
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-agent-queue.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

export OPU_AGENT_TEST_MODE=1
export OPU_AGENT_QUEUE_DIR="$TMP/queue"
export OPU_WEBAPP_PRINCIPALS_FILE="$TMP/principals.json"
export PYTHONPATH="$ROOT/webapp${PYTHONPATH:+:$PYTHONPATH}"

python3 - <<PY
import json, os
import agent_queue
import auth

job = agent_queue.publish_task(plan_id="p1", task_id="001-a", node="node1", adapter="database_rolling_opatch")
assert job["status"] == "queued"
claimed = agent_queue.claim("node1", "agent-a", lease_seconds=60)
assert claimed and claimed["claimed_by"] == "agent-a"
done = agent_queue.complete(claimed["job_id"], "agent-a", result={"ok": True}, claim_token=claimed["claim_token"])
assert done["status"] == "completed"
assert agent_queue.claim("node1", "agent-a", lease_seconds=60) is None

os.environ["OPU_WEBAPP_RBAC"] = "0"
auth.require_role("anyone", "approve")

Path = __import__("pathlib").Path
Path(os.environ["OPU_WEBAPP_PRINCIPALS_FILE"]).write_text(json.dumps({
  "principals": [
    {"actor": "alice", "roles": ["requester"]},
    {"actor": "bob", "roles": ["approver"]},
    {"actor": "carol", "roles": ["operator"]},
  ]
}))
os.environ["OPU_WEBAPP_RBAC"] = "1"
auth.require_role("bob", "approve")
try:
    auth.require_role("alice", "approve")
    raise SystemExit("requester must not approve")
except auth.AuthError:
    pass
print("agent queue and RBAC test passed")
PY
