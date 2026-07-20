#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
JOBCTL="$ROOT/bin/opu-jobctl"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-jobs.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
NODES="$TMP/nodes"
printf '%s\n' node-a node-b >"$NODES"
run() { OPU_TEST_MODE=1 OPU_JOB_STATE_DIR="$TMP/state" "$JOBCTL" "$@"; }
digest=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

plan=$(run create --job-id grid-job-001 --requester patch-admin --topology grid-rolling --patch-id 39034528 --patch-dir /stage/39034528 --nodes "$NODES")
jq -e '.job_type == "grid.rolling.patch.v1" and .nodes == ["node-a","node-b"]' "$plan" >/dev/null
run submit --job-id grid-job-001 --actor patch-admin
run ready --job-id grid-job-001 --actor precheck-agent --evidence-sha256 "$digest"
if run approve --job-id grid-job-001 --actor patch-admin >/dev/null 2>&1; then
    echo 'requester self-approval was accepted' >&2; exit 1
fi
run approve --job-id grid-job-001 --actor dba-approver
run start --job-id grid-job-001 --actor patch-operator
next=$(run next --job-id grid-job-001)
task=$(jq -r '.task_id' <<<"$next")
[ "$task" = 001-apply-node-a ]
for task in 001-apply-node-a 002-validate-node-a 003-apply-node-b 004-validate-node-b; do
    claimed=$(run claim --job-id grid-job-001 --actor patch-operator --lease-seconds 300)
    [ "$(jq -r '.task_id' <<<"$claimed")" = "$task" ]
    if [ "$task" = 001-apply-node-a ] && run complete --job-id grid-job-001 --task-id "$task" --actor wrong-operator --status succeeded --evidence-sha256 "$digest" >/dev/null 2>&1; then
        echo 'unclaimed task completion was accepted' >&2; exit 1
    fi
    run complete --job-id grid-job-001 --task-id "$task" --actor patch-operator --status succeeded --evidence-sha256 "$digest"
done
run status --job-id grid-job-001 | jq -e '.state == "succeeded"' >/dev/null
plan_file="$TMP/state/jobs/grid-job-001/plan.json"
jq '.patch_id = "tampered"' "$plan_file" >"$plan_file.tmp" && mv "$plan_file.tmp" "$plan_file"
if run status --job-id grid-job-001 >/dev/null 2>&1; then
    echo 'tampered plan was accepted' >&2; exit 1
fi

run create --job-id grid-job-lease-001 --requester patch-admin --topology grid-rolling --patch-id 39034528 --patch-dir /stage/39034528 --nodes "$NODES" >/dev/null
run submit --job-id grid-job-lease-001 --actor patch-admin
run ready --job-id grid-job-lease-001 --actor precheck-agent --evidence-sha256 "$digest"
run approve --job-id grid-job-lease-001 --actor dba-approver
run start --job-id grid-job-lease-001 --actor patch-operator
run claim --job-id grid-job-lease-001 --actor grid-agent-01 --lease-seconds 30 >/dev/null
lease_task="$TMP/state/jobs/grid-job-lease-001/tasks/001-apply-node-a.json"
jq '.lease_expires_epoch = 0' "$lease_task" >"$lease_task.tmp" && mv "$lease_task.tmp" "$lease_task"
run reconcile --job-id grid-job-lease-001 --actor patch-operator
run status --job-id grid-job-lease-001 | jq -e '.state == "paused"' >/dev/null

run create --job-id grid-job-concurrent-001 --requester patch-admin --topology grid-rolling --patch-id 39034528 --patch-dir /stage/39034528 --nodes "$NODES" >/dev/null
run submit --job-id grid-job-concurrent-001 --actor patch-admin
run ready --job-id grid-job-concurrent-001 --actor precheck-agent --evidence-sha256 "$digest"
run approve --job-id grid-job-concurrent-001 --actor dba-approver
run start --job-id grid-job-concurrent-001 --actor patch-operator
run claim --job-id grid-job-concurrent-001 --actor grid-agent-01 --lease-seconds 300 >"$TMP/claim-one.json" &
pid_one=$!
run claim --job-id grid-job-concurrent-001 --actor grid-agent-02 --lease-seconds 300 >"$TMP/claim-two.json" &
pid_two=$!
set +e
wait "$pid_one"; rc_one=$?
wait "$pid_two"; rc_two=$?
set -e
[ "$rc_one" -eq 0 ] || [ "$rc_two" -eq 0 ]
[ "$rc_one" -ne 0 ] || [ "$rc_two" -ne 0 ]
task=$(jq -r '.task_id' "$TMP/claim-one.json" "$TMP/claim-two.json" 2>/dev/null | head -n1)
[ "$task" = 001-apply-node-a ]

db_plan=$(run create --job-id database-job-001 --requester patch-admin --topology database-rolling --database PSADB_PSADB1 --grid-home /u01/app/19.0.0.0/grid --patch-id 39034528 --patch-dir /stage/39034528 --nodes "$NODES")
jq -e '.job_type == "database.rolling.patch.v1" and .database == "PSADB_PSADB1" and .grid_home == "/u01/app/19.0.0.0/grid"' "$db_plan" >/dev/null
run submit --job-id database-job-001 --actor patch-admin
run ready --job-id database-job-001 --actor precheck-agent --evidence-sha256 "$digest"
run approve --job-id database-job-001 --actor dba-approver
run start --job-id database-job-001 --actor patch-operator
run next --job-id database-job-001 | jq -e '.task_id == "001-apply-node-a"' >/dev/null
task_count=$(find "$TMP/state/jobs/database-job-001/tasks" -name '*.json' | wc -l | tr -d ' ')
[ "$task_count" -eq 6 ]
printf '%s\n' 'job lifecycle test passed'
