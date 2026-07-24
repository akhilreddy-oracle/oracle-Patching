#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-dataguard-swx.XXXXXX")
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

"$ROOT/bin/opu-dataguard-evaluate" --observe "$TMP/observe.json" --policy "$TMP/policy.json" --output "$TMP/eval.json" >/dev/null
jq -e '.status == "ready_for_standby_first"' "$TMP/eval.json" >/dev/null

"$ROOT/bin/opu-dataguard-plan-order" --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --output "$TMP/order.json" >/dev/null

OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-switchover-gate" --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --output "$TMP/sw.json" >/dev/null
jq -e '.status == "ready_for_switchover"' "$TMP/sw.json" >/dev/null

# TEST_MODE switchover succeeds with sealed simulated evidence.
OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-switchover" execute --gate "$TMP/sw.json" --observe "$TMP/observe.json" \
  --target-standby ORCL_STBY --actor opu-lab --output "$TMP/sw-exec.json" >/dev/null
jq -e '.operation == "dataguard.switchover" and .status == "succeeded" and .simulated == true and .target_standby == "ORCL_STBY"' "$TMP/sw-exec.json" >/dev/null
sealed=$(jq -r '.record_sha256' "$TMP/sw-exec.json")
canonical=$(jq -cS 'del(.record_sha256)' "$TMP/sw-exec.json")
recomputed=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
[ "$sealed" = "$recomputed" ]
jq -e --arg observe "$(sha256sum "$TMP/observe.json" | awk '{print $1}')" '.evidence.observe_sha256 == $observe' "$TMP/sw-exec.json" >/dev/null

# Target not present in observe members must refuse.
set +e
OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-switchover" execute --gate "$TMP/sw.json" --observe "$TMP/observe.json" \
  --target-standby NOT_A_MEMBER --actor opu-lab --output "$TMP/sw-exec-bad-target.json" >/dev/null 2>&1
bad_target_rc=$?
set -e
[ "$bad_target_rc" -ne 0 ]

# Tampered gate keeping the stale record_sha256 must refuse.
jq '.evaluated_at="1999-01-01T00:00:00Z"' "$TMP/sw.json" >"$TMP/sw-tampered.json"
set +e
OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-switchover" execute --gate "$TMP/sw-tampered.json" --observe "$TMP/observe.json" \
  --target-standby ORCL_STBY --actor opu-lab --output "$TMP/sw-exec-tampered.json" >/dev/null 2>&1
tampered_rc=$?
set -e
[ "$tampered_rc" -ne 0 ]

# Production mode without a certification marker must refuse mutation authority.
set +e
OPU_DATAGUARD_TEST_MODE=1 OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/missing.cert" \
  "$ROOT/bin/opu-dataguard-switchover" execute --gate "$TMP/sw.json" --observe "$TMP/observe.json" \
  --target-standby ORCL_STBY --actor opu-lab --output "$TMP/sw-exec-prod.json" >/dev/null 2>&1
prod_rc=$?
set -e
[ "$prod_rc" -eq 77 ]

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

# TEST_MODE reinstate succeeds with sealed simulated evidence.
OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-reinstate" execute --gate "$TMP/re.json" --observe "$TMP/observe-stby.json" \
  --database ORCL_STBY --actor opu-lab --output "$TMP/re-exec.json" >/dev/null
jq -e '.operation == "dataguard.reinstate" and .status == "succeeded" and .simulated == true and .database == "ORCL_STBY"' "$TMP/re-exec.json" >/dev/null
sealed=$(jq -r '.record_sha256' "$TMP/re-exec.json")
canonical=$(jq -cS 'del(.record_sha256)' "$TMP/re-exec.json")
recomputed=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
[ "$sealed" = "$recomputed" ]

# Tampered reinstate gate must refuse.
jq '.evaluated_at="1999-01-01T00:00:00Z"' "$TMP/re.json" >"$TMP/re-tampered.json"
set +e
OPU_DATAGUARD_TEST_MODE=1 \
  "$ROOT/bin/opu-dataguard-reinstate" execute --gate "$TMP/re-tampered.json" --observe "$TMP/observe-stby.json" \
  --database ORCL_STBY --actor opu-lab --output "$TMP/re-exec-tampered.json" >/dev/null 2>&1
re_tampered_rc=$?
set -e
[ "$re_tampered_rc" -ne 0 ]

# Orchestrate with a sealed switchover gate delegates the step to the executor.
"$ROOT/bin/opu-dataguard-orchestrate" \
  --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --order "$TMP/order.json" \
  --switchover-gate "$TMP/sw.json" --reinstate-gate "$TMP/re.json" --output "$TMP/orch-gated.json" >/dev/null
jq -e 'any(.steps[]; .step == "switchover" and .operator_executed == false and .executor == "opu-dataguard-switchover" and .mode == "test_mode_or_live")' "$TMP/orch-gated.json" >/dev/null
jq -e 'any(.steps[]; .step == "reinstate_former_primary" and .operator_executed == false and .executor == "opu-dataguard-reinstate")' "$TMP/orch-gated.json" >/dev/null

# Orchestrate without gates keeps switchover/reinstate operator-executed.
"$ROOT/bin/opu-dataguard-orchestrate" \
  --observe "$TMP/observe.json" --evaluation "$TMP/eval.json" --order "$TMP/order.json" \
  --output "$TMP/orch-plain.json" >/dev/null
jq -e 'any(.steps[]; .step == "switchover" and .operator_executed == true)' "$TMP/orch-plain.json" >/dev/null
jq -e 'any(.steps[]; .step == "reinstate_former_primary" and .operator_executed == true)' "$TMP/orch-plain.json" >/dev/null

printf '%s\n' 'Data Guard switchover/reinstate executor test passed'
