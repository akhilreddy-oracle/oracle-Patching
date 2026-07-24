#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-dataguard.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

HOME_PATH=/u01/app/oracle/product/19.0.0/dbhome_1
SID=ORCL

# Healthy primary + one standby within lag policy.
jq -n --arg home "$HOME_PATH" --arg sid "$SID" --arg now "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '
  {schema_version:"1.0",collector:{name:"oracle.dataguard.observe",version:"1"},collected_at:$now,
   target:{oracle_home:$home,oracle_sid:$sid},
   primary:{database_role:"PRIMARY",open_mode:"READ WRITE",protection_mode:"MAXIMIZE PERFORMANCE"},
   broker:{status:"configured"},
   members:[{db_unique_name:"ORCL_STBY",status:"APPLYING_LOG",transport_lag_seconds:2,apply_lag_seconds:5}]}
' >"$TMP/observe.raw.json"
canonical=$(jq -cS . "$TMP/observe.raw.json")
hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" '.record_sha256=$hash' "$TMP/observe.raw.json" >"$TMP/observe.json"

jq -n '{schema_version:"1.0",dataguard:{max_transport_lag_seconds:30,max_apply_lag_seconds:60,require_broker:true}}' >"$TMP/policy.json"

OPU_DATAGUARD_TEST_MODE=1 OPU_DATAGUARD_TEST_FIXTURE="$TMP/observe.json" \
  "$ROOT/bin/opu-dataguard-observe" --oracle-home "$HOME_PATH" --oracle-sid "$SID" --output "$TMP/observe-out.json" >/dev/null
jq -e '(.collector.name == "oracle.dataguard.observe") and ((.members | length) == 1)' "$TMP/observe-out.json" >/dev/null

"$ROOT/bin/opu-dataguard-evaluate" --observe "$TMP/observe.json" --policy "$TMP/policy.json" --output "$TMP/eval.json" >/dev/null
jq -e '.status == "ready_for_standby_first"' "$TMP/eval.json" >/dev/null

"$ROOT/bin/opu-dataguard-plan-order" --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --output "$TMP/order.json" >/dev/null
jq -e '.strategy == "standby_first" and .order[0].role == "STANDBY" and .order[-1].role == "PRIMARY"' "$TMP/order.json" >/dev/null

OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-switchover-gate" --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --output "$TMP/sw.json" >/dev/null
jq -e '.status == "ready_for_switchover"' "$TMP/sw.json" >/dev/null

# Reinstate gate expects standby-shaped observation.
jq '.primary.database_role="PHYSICAL STANDBY"' "$TMP/observe.json" >"$TMP/observe-stby.raw.json"
canonical=$(jq -cS 'del(.record_sha256)' "$TMP/observe-stby.raw.json")
hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" 'del(.record_sha256) | .record_sha256=$hash' "$TMP/observe-stby.raw.json" >"$TMP/observe-stby.json"
# Re-bind evaluation observe digest for reinstate evidence check.
eval_sha_observe=$(sha256sum "$TMP/observe-stby.json" | awk '{print $1}')
jq --arg sha "$eval_sha_observe" '.evidence.observe_sha256=$sha | del(.record_sha256)' "$TMP/eval.json" >"$TMP/eval-stby.raw.json"
canonical=$(jq -cS . "$TMP/eval-stby.raw.json")
hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" '.record_sha256=$hash' "$TMP/eval-stby.raw.json" >"$TMP/eval-stby.json"

OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-reinstate-gate" --observe "$TMP/observe-stby.json" --evaluation "$TMP/eval-stby.json" --output "$TMP/re.json" >/dev/null
jq -e '.status == "ready_for_reinstate"' "$TMP/re.json" >/dev/null

"$ROOT/bin/opu-dataguard-orchestrate" \
  --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --order "$TMP/order.json" \
  --switchover-gate "$TMP/sw.json" --output "$TMP/orch.json" >/dev/null
jq -e '.status == "ready_for_orchestration" and any(.steps[]; .step == "patch_standbys")' "$TMP/orch.json" >/dev/null

# Lagging standby must block.
jq '.members[0].apply_lag_seconds = 9999' "$TMP/observe.json" >"$TMP/observe-lag.raw.json"
canonical=$(jq -cS 'del(.record_sha256)' "$TMP/observe-lag.raw.json")
hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" 'del(.record_sha256) | .record_sha256=$hash' "$TMP/observe-lag.raw.json" >"$TMP/observe-lag.json"
set +e
"$ROOT/bin/opu-dataguard-evaluate" --observe "$TMP/observe-lag.json" --policy "$TMP/policy.json" --output "$TMP/eval-lag.json" >/dev/null
lag_rc=$?
set -e
[ "$lag_rc" -eq 2 ]
jq -e '.status == "blocked" and any(.gates[]; .name == "lag" and .status == "blocker")' "$TMP/eval-lag.json" >/dev/null

printf '%s\n' 'Data Guard observe/evaluate/plan-order/gates/orchestrate test passed'
