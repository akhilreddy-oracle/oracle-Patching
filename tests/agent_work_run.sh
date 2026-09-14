#!/usr/bin/env bash
# Lab pull-agent run bridge: enroll -> publish -> claim -> execute (stub
# executor via test-only OPU_AGENT_EXECUTOR_<ADAPTER> override) -> complete,
# plus fail-closed and enrollment-enforcement checks.
set -euo pipefail
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-agent-work-run.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

export OPU_AGENT_TEST_MODE=1
export OPU_AGENT_QUEUE_DIR="$TMP/queue"
export OPU_AGENT_REGISTRY_FILE="$TMP/registry/agents.json"
export OPU_PLAN_STATE_DIR="$TMP/plans"
export PYTHONPATH="$ROOT/webapp${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$OPU_PLAN_STATE_DIR"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

cat >"$TMP/stub-ok" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "execute" ] || exit 64
echo "stub executor ok: $*"
EOF
cat >"$TMP/stub-fail" <<'EOF'
#!/usr/bin/env bash
echo "stub executor failing" >&2
exit 3
EOF
chmod +x "$TMP/stub-ok" "$TMP/stub-fail"

publish() {
  OPU_PUB_TASK="$1" python3 - <<'PY'
import os
import agent_queue
job = agent_queue.publish_task(
    plan_id="p-" + os.environ["OPU_PUB_TASK"],
    task_id=os.environ["OPU_PUB_TASK"],
    node="node1",
    adapter="database_rolling_opatch",
)
assert job["status"] == "queued", job
PY
}

TOKEN=$("$ROOT/bin/opu-agent-enroll" enroll --node node1 --agent-id agent-a | jq -r '.agent_token')
[ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || fail 'enroll did not return an agent_token'
"$ROOT/bin/opu-agent-enroll" list \
  | jq -e '.agents | length == 1 and .[0].agent_id == "agent-a" and (.[0] | has("token_sha256") | not)' >/dev/null \
  || fail 'list must show agent-a without the token hash'

if env -u OPU_PLAN_STATE_DIR "$ROOT/bin/opu-agent-work-run" --node node1 --agent-id agent-a >/dev/null 2>&1; then
  fail 'work-run must fail closed without OPU_PLAN_STATE_DIR'
fi

publish t1
OPU_AGENT_EXECUTOR_DATABASE_ROLLING_OPATCH="$TMP/stub-ok" \
  "$ROOT/bin/opu-agent-work-run" --node node1 --agent-id agent-a --lease-seconds 60 >"$TMP/ok.json" \
  || fail 'work-run with succeeding stub must exit 0'
jq -e '.status == "completed" and .result.status == "success" and .result.exit_code == 0
       and (.result.stdout_sha256 | length == 64)' <"$TMP/ok.json" >/dev/null \
  || fail 'success run must complete the job with result.status success'

publish t2
if OPU_AGENT_EXECUTOR_DATABASE_ROLLING_OPATCH="$TMP/stub-fail" \
  "$ROOT/bin/opu-agent-work-run" --node node1 --agent-id agent-a --lease-seconds 60 >"$TMP/failed.json"; then
  fail 'work-run with failing stub must exit nonzero'
fi
jq -e '.status == "completed" and .result.status == "failed" and .result.exit_code == 3' <"$TMP/failed.json" >/dev/null \
  || fail 'failed run must complete the job with result.status failed'

export OPU_AGENT_ENROLLMENT_REQUIRED=1
publish t3
python3 - <<'PY'
import agent_queue
for token in ("wrong-token", None):
    try:
        agent_queue.claim("node1", "agent-a", lease_seconds=60, agent_token=token)
        raise SystemExit(f"claim with token {token!r} must be rejected")
    except agent_queue.QueueError as exc:
        assert exc.status == 403, exc.status
PY
if OPU_AGENT_EXECUTOR_DATABASE_ROLLING_OPATCH="$TMP/stub-ok" \
  "$ROOT/bin/opu-agent-work-run" --node node1 --agent-id agent-a --agent-token bad-token >/dev/null 2>&1; then
  fail 'work-run with a bad token must be rejected when enrollment is required'
fi
OPU_AGENT_EXECUTOR_DATABASE_ROLLING_OPATCH="$TMP/stub-ok" \
  "$ROOT/bin/opu-agent-work-run" --node node1 --agent-id agent-a --agent-token "$TOKEN" >"$TMP/enrolled.json" \
  || fail 'work-run with the enrolled token must succeed'
jq -e '.status == "completed" and .result.status == "success"' <"$TMP/enrolled.json" >/dev/null \
  || fail 'enrolled run must complete successfully'

publish t4
"$ROOT/bin/opu-agent-work-pull" --node node1 --agent-id agent-a --agent-token "$TOKEN" \
  | jq -e '.status == "claimed" and .claimed_by == "agent-a"' >/dev/null \
  || fail 'work-pull with the enrolled token must claim'

"$ROOT/bin/opu-agent-enroll" revoke --agent-id agent-a | jq -e '.revoked == true' >/dev/null \
  || fail 'revoke must mark the agent revoked'
OPU_TEST_TOKEN="$TOKEN" python3 - <<'PY'
import os
import agent_enroll
assert agent_enroll.verify("agent-a", os.environ["OPU_TEST_TOKEN"]) is False, "revoked agent must not verify"
PY

printf '%s\n' 'agent work-run test passed'
