#!/usr/bin/env bash
# Exercise the actual non-TEST_MODE SQL collector using temporary fake Oracle
# binaries. This test never connects to Oracle, SSH, or a broker service.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-dg-live.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
. "$ROOT/lib/opu/common.sh"
export TZ=UTC
export OPU_DG_FAKE_HOME="$TMP/dbhome" OPU_DG_SQL_LOG="$TMP/sql.log"
export OPU_DG_LOCAL_NOW OPU_DG_COMPUTED OPU_DG_DATUM
OPU_DG_LOCAL_NOW=$(date -u '+%Y-%m-%d %H:%M:%S')
OPU_DG_COMPUTED=$(date -u '+%m/%d/%Y %H:%M:%S')
OPU_DG_DATUM=$OPU_DG_COMPUTED
mkdir -p "$OPU_DG_FAKE_HOME/bin"
cat >"$OPU_DG_FAKE_HOME/bin/sqlplus" <<'SQLPLUS'
#!/usr/bin/env bash
set -euo pipefail
sql=$(cat)
printf '%s\n' "$sql" >>"$OPU_DG_SQL_LOG"
[[ "$sql" == *'whenever sqlerror exit failure rollback'* ]] || exit 91
[[ "$sql" == *'whenever oserror exit failure rollback'* ]] || exit 92
case "${OPU_DG_CASE:-healthy}" in
  sql_error) printf 'ORA-00904: invalid identifier\n'; exit 9 ;;
  zero_exit_error) printf 'SP2-0734: unknown command\n'; exit 0 ;;
esac
if [[ "$sql" == *'from v$database;'* ]]; then
  printf 'DG_DATABASE|%s|%s|MAXIMUM PERFORMANCE|%s|DB_PRIMARY|%s|ENABLED\n' \
    "${OPU_DG_ROLE:-PHYSICAL STANDBY}" "${OPU_DG_OPEN:-MOUNTED}" "${OPU_DG_NAME:-DB_STBY}" "$OPU_DG_LOCAL_NOW"
elif [[ "$sql" == *'from v$archive_dest_status'* ]]; then
  [[ "${OPU_DG_ROLE:-}" == PRIMARY ]] || exit 93
  printf 'DG_DEST|DB_STBY|VALID|MANAGED REAL TIME APPLY|PHYSICAL|~\n'
elif [[ "$sql" == *'from v$dataguard_stats'* ]]; then
  [[ "${OPU_DG_ROLE:-}" != PRIMARY ]] || exit 94
  # Reject the reviewed invalid column query even if wrapped in new SQL.
  [[ "$sql" == *"nvl(value,'~')"* && "$sql" == *"nvl(time_computed,'~')"* && "$sql" == *"nvl(datum_time,'~')"* ]] || exit 95
  [[ "$sql" == *'from v$dataguard_process'* ]] || exit 96
  if [ "${OPU_DG_CASE:-}" = metrics_sql_error ]; then printf 'ORA-00942: table or view does not exist\n'; exit 7; fi
  printf 'DG_CLOCK|%s\n' "$OPU_DG_LOCAL_NOW"
  if [ "${OPU_DG_CASE:-}" != no_apply ]; then printf 'DG_APPLY|MRP0|%s\n' "${OPU_DG_ACTION:-APPLYING_LOG}"; fi
  [ "${OPU_DG_CASE:-}" = missing_metrics ] && exit 0
  printf 'DG_METRIC|transport lag|%s|day(2) to second(0) interval|%s|%s|DB_PRIMARY\n' \
    "${OPU_DG_TRANSPORT:-+00 00:00:00}" "$OPU_DG_COMPUTED" "$OPU_DG_DATUM"
  printf 'DG_METRIC|apply lag|%s|day(2) to second(0) interval|%s|%s|DB_PRIMARY\n' \
    "${OPU_DG_APPLY:-+00 00:00:05}" "$OPU_DG_COMPUTED" "$OPU_DG_DATUM"
  if [ "${OPU_DG_CASE:-}" = duplicate ]; then
    printf 'DG_METRIC|apply lag|+00 00:00:00|day(2) to second(0) interval|%s|%s|DB_PRIMARY\n' "$OPU_DG_COMPUTED" "$OPU_DG_DATUM"
  fi
else
  printf 'unexpected SQL query\n' >&2
  exit 97
fi
SQLPLUS
cat >"$OPU_DG_FAKE_HOME/bin/dgmgrl" <<'DGMGRL'
#!/usr/bin/env bash
set -euo pipefail
[ "${3:-}" = 'SHOW CONFIGURATION;' ] || exit 98
if [ "${OPU_DG_CASE:-}" = broker_error ]; then printf 'ORA-16698: member error\nConfiguration Status:\nSUCCESS\n'; exit 0; fi
printf 'Configuration - fixture\n  Protection Mode: MaxPerformance\nConfiguration Status:\nSUCCESS (status updated 1 second ago)\n'
DGMGRL
chmod +x "$OPU_DG_FAKE_HOME/bin/sqlplus" "$OPU_DG_FAKE_HOME/bin/dgmgrl"
jq -n '{schema_version:"1.0",dataguard:{max_transport_lag_seconds:30,max_apply_lag_seconds:60,require_broker:true,maximum_sample_age_seconds:60,maximum_observation_age_seconds:300}}' >"$TMP/policy.json"
observe() {
  OPU_DATAGUARD_TEST_MODE=0 "$ROOT/bin/opu-dataguard-observe" --oracle-home "$OPU_DG_FAKE_HOME" --oracle-sid INSTANCE1 --output "$1" >/dev/null
}
evaluate() {
  "$ROOT/bin/opu-dataguard-evaluate" --observe "$1" --policy "$TMP/policy.json" --output "$2" >/dev/null
}
blocked() {
  local rc
  if evaluate "$1" "$TMP/evaluated.json"; then echo 'unexpected Data Guard ready result' >&2; exit 1; else rc=$?; fi
  [ "$rc" -eq 2 ]
  jq -e '.status == "blocked"' "$TMP/evaluated.json" >/dev/null
}
seal() {
  local source=$1 dest=$2 canonical
  canonical=$(jq -cS 'del(.record_sha256)' "$source")
  jq --arg hash "$(opu_hash_string "$canonical")" 'del(.record_sha256) + {record_sha256:$hash}' "$source" >"$dest"
}

# Real live-path command sequence: native database identity differs from SID.
observe "$TMP/healthy.json"
jq -e '.collector.version == "2" and .target.oracle_sid == "INSTANCE1" and .target.db_unique_name == "DB_STBY" and .primary.db_unique_name == "DB_STBY" and .members[0].db_unique_name == "DB_STBY" and .members[0].apply_lag_seconds == 5 and .members[0].lag_samples.apply.datum_time and .members[0].lag_samples.apply.time_computed and .broker.status == "configured"' "$TMP/healthy.json" >/dev/null
evaluate "$TMP/healthy.json" "$TMP/ready.json"
jq -e '.status == "ready_for_standby_first" and all(.gates[]; .status != "blocker")' "$TMP/ready.json" >/dev/null
"$ROOT/bin/opu-dataguard-plan-order" --observe "$TMP/healthy.json" --evaluation "$TMP/ready.json" --output "$TMP/order.json" >/dev/null
jq -e '.order[0].db_unique_name == "DB_STBY"' "$TMP/order.json" >/dev/null

# Waiting for the next redo log is safe only while lag data remains fresh.
OPU_DG_ACTION=WAIT_FOR_LOG observe "$TMP/waiting.json"
evaluate "$TMP/waiting.json" "$TMP/waiting-eval.json"

# Day/hour/minute/fractional seconds are retained, rounded upward conservatively.
OPU_DG_APPLY='+01 02:03:04.5' observe "$TMP/long-lag.json"
jq -e '.members[0].apply_lag_seconds == 93785' "$TMP/long-lag.json" >/dev/null
blocked "$TMP/long-lag.json"

# A zero value with stale/future or absent freshness information cannot pass.
OPU_DG_DATUM='01/01/2000 00:00:00' OPU_DG_APPLY='+00 00:00:00' observe "$TMP/stale-datum.json"
blocked "$TMP/stale-datum.json"
jq -e 'any(.gates[]; .name == "sample_freshness" and .status == "blocker")' "$TMP/evaluated.json" >/dev/null
OPU_DG_COMPUTED='01/01/2099 00:00:00' observe "$TMP/future-computed.json"
blocked "$TMP/future-computed.json"
OPU_DG_DATUM='~' observe "$TMP/null-datum.json"
blocked "$TMP/null-datum.json"
OPU_DG_APPLY='~' observe "$TMP/null-lag.json"
jq -e '.members[0].apply_lag_seconds == -1' "$TMP/null-lag.json" >/dev/null
blocked "$TMP/null-lag.json"
OPU_DG_CASE=missing_metrics observe "$TMP/no-metrics.json"
blocked "$TMP/no-metrics.json"
OPU_DG_CASE=no_apply observe "$TMP/no-apply.json"
blocked "$TMP/no-apply.json"
OPU_DG_CASE=broker_error observe "$TMP/broker-error.json"
blocked "$TMP/broker-error.json"

# Primary collection queries destination health, never nonexistent lag columns.
OPU_DG_ROLE=PRIMARY OPU_DG_OPEN='READ WRITE' OPU_DG_NAME=DB_PRIMARY observe "$TMP/primary.json"
jq -e '.target.db_unique_name == "DB_PRIMARY" and .members[0].db_unique_name == "DB_STBY" and .members[0].apply_lag_seconds == -1 and .members[0].transport_lag_seconds == -1 and .members[0].lag_source == "unavailable_on_primary"' "$TMP/primary.json" >/dev/null
blocked "$TMP/primary.json"

# SQL nonzero, SQL*Plus zero-exit diagnostics, duplicates and malformed values fail collection.
for scenario in sql_error zero_exit_error metrics_sql_error duplicate; do
  if OPU_DG_CASE="$scenario" observe "$TMP/rejected-$scenario.json" 2>/dev/null; then echo "accepted $scenario" >&2; exit 1; fi
  [ ! -e "$TMP/rejected-$scenario.json" ]
done
if OPU_DG_APPLY='not an interval' observe "$TMP/malformed.json" 2>/dev/null; then echo 'accepted malformed lag' >&2; exit 1; fi
if OPU_DG_DATUM='99/99/2026 25:00:00' observe "$TMP/malformed-date.json" 2>/dev/null; then echo 'accepted malformed timestamp' >&2; exit 1; fi

# Replayed observations and malformed members cannot turn into a ready result.
jq '.collected_at="2000-01-01T00:00:00Z"' "$TMP/healthy.json" >"$TMP/old.raw.json"
seal "$TMP/old.raw.json" "$TMP/old.json"
blocked "$TMP/old.json"
jq '.members[0].db_unique_name=""' "$TMP/healthy.json" >"$TMP/invalid.raw.json"
seal "$TMP/invalid.raw.json" "$TMP/invalid.json"
if evaluate "$TMP/invalid.json" "$TMP/invalid-eval.json" 2>/dev/null; then echo 'accepted unnamed member' >&2; exit 1; fi
jq '.members[0].apply_lag_seconds=0' "$TMP/healthy.json" >"$TMP/tampered.json"
if evaluate "$TMP/tampered.json" "$TMP/tampered-eval.json" 2>/dev/null; then echo 'accepted stale record checksum' >&2; exit 1; fi
printf '%s\n' 'Data Guard live SQL/identity/lag/freshness regression tests passed'
