#!/usr/bin/env bash
# Regression suite for shell fail-open defects (cert substring, empty DG members,
# reinstate-on-blocked).
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-shell-failopen.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
. "$ROOT/lib/opu/common.sh"

# Optional trace output; set OPU_TEST_TRACE=1 to print each checkpoint.
dbg() {
  [ "${OPU_TEST_TRACE:-0}" = 1 ] || return 0
  printf 'trace %s %s %s\n' "$1" "$2" "$3" >&2
}

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# H1: substring cert must not pass
printf 'OPU_PRODUCTION_CERTIFIED=10\n' >"$TMP/bad.cert"
set +e
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/bad.cert" opu_require_production_certified
rc=$?
set -e
dbg H1 "cert_substring_rejected" "{\"rc\":$rc}"
[ "$rc" -eq 77 ] || fail "OPU_PRODUCTION_CERTIFIED=10 was accepted (rc=$rc)"

printf 'OPU_PRODUCTION_CERTIFIED=1\n' >"$TMP/ok.cert"
OPU_PRODUCTION_MODE=1 OPU_PRODUCTION_CERT_FILE="$TMP/ok.cert" opu_require_production_certified \
  || fail "exact cert line should pass"
dbg H1 "cert_exact_accepted" "{\"ok\":true}"

# H2: empty members on STANDBY-shaped observe must block evaluate
HOME_PATH=/u01/app/oracle/product/19.0.0/dbhome_1
SID=ORCL
jq -n --arg home "$HOME_PATH" --arg sid "$SID" --arg now "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '
  {schema_version:"1.0",collector:{name:"oracle.dataguard.observe",version:"1"},collected_at:$now,
   target:{oracle_home:$home,oracle_sid:$sid},
   primary:{database_role:"PHYSICAL STANDBY",open_mode:"MOUNTED",protection_mode:"MAXIMIZE PERFORMANCE"},
   broker:{status:"configured"},members:[]}
' >"$TMP/empty-members.raw.json"
canonical=$(jq -cS . "$TMP/empty-members.raw.json")
hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" '.record_sha256=$hash' "$TMP/empty-members.raw.json" >"$TMP/empty-members.json"
jq -n '{schema_version:"1.0",dataguard:{max_transport_lag_seconds:30,max_apply_lag_seconds:60,require_broker:true}}' >"$TMP/policy.json"
set +e
"$ROOT/bin/opu-dataguard-evaluate" --observe "$TMP/empty-members.json" --policy "$TMP/policy.json" --output "$TMP/empty-eval.json" >/dev/null
rc=$?
set -e
dbg H2 "empty_members_evaluate" "{\"rc\":$rc,\"status\":$(jq -c '.status' "$TMP/empty-eval.json")}"
[ "$rc" -eq 2 ] || fail "empty members evaluate should exit 2"
jq -e '.status == "blocked"' "$TMP/empty-eval.json" >/dev/null || fail "empty members should be blocked"

# H3: reinstate must refuse blocked evaluation
jq -n --arg home "$HOME_PATH" --arg sid "$SID" --arg now "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '
  {schema_version:"1.0",collector:{name:"oracle.dataguard.observe",version:"1"},collected_at:$now,
   target:{oracle_home:$home,oracle_sid:$sid},
   primary:{database_role:"PHYSICAL STANDBY",open_mode:"MOUNTED",protection_mode:"MAXIMIZE PERFORMANCE"},
   broker:{status:"configured"},
   members:[{db_unique_name:"ORCL_STBY",status:"APPLYING_LOG",transport_lag_seconds:1,apply_lag_seconds:1}]}
' >"$TMP/stby.raw.json"
canonical=$(jq -cS . "$TMP/stby.raw.json"); hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" '.record_sha256=$hash' "$TMP/stby.raw.json" >"$TMP/stby.json"
obs_sha=$(opu_hash_file "$TMP/stby.json")
jq -n --arg sha "$obs_sha" --arg now "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{schema_version:"1.0",status:"blocked",evaluated_at:$now,gates:[{name:"lag",status:"blocker",detail:"lag"}],evidence:{observe_sha256:$sha,policy_sha256:"deadbeef"}}' \
  >"$TMP/blocked-eval.raw.json"
canonical=$(jq -cS . "$TMP/blocked-eval.raw.json"); hash=$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')
jq --arg hash "$hash" '.record_sha256=$hash' "$TMP/blocked-eval.raw.json" >"$TMP/blocked-eval.json"
set +e
OPU_DATAGUARD_TEST_MODE=1 "$ROOT/bin/opu-dataguard-reinstate-gate" \
  --observe "$TMP/stby.json" --evaluation "$TMP/blocked-eval.json" --output "$TMP/re-blocked.json" >/dev/null
rc=$?
set -e
dbg H3 "reinstate_blocked_eval" "{\"rc\":$rc,\"status\":$(jq -c '.status' "$TMP/re-blocked.json")}"
[ "$rc" -eq 2 ] || fail "reinstate with blocked eval should exit 2"
jq -e '.status == "blocked"' "$TMP/re-blocked.json" >/dev/null || fail "reinstate should be blocked"

# Cross-process controller locking is exercised by native_controller_edges.py.

# H4: document lag formula expectation (live SQL path) — fixture lag still authoritative in TEST_MODE
dbg H4 "lag_sql_total_seconds" "{\"note\":\"live SQL uses day/hour/minute/second sum; fixture path unchanged\"}"

printf '%s\n' 'shell fail-open regression suite passed'
