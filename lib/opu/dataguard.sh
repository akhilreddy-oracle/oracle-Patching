#!/usr/bin/env bash
# Integrity and output primitives for the standalone Data Guard commands.

opu_dataguard_verify_document() {
  local file=$1 expected canonical
  [ -f "$file" ] && [ ! -L "$file" ] || { opu_error 'Data Guard evidence is missing or linked'; return 65; }
  expected=$(jq -er '.record_sha256 | select(type == "string" and test("^[a-f0-9]{64}$"))' "$file") || return 65
  canonical=$(jq -cS 'del(.record_sha256)' "$file") || return 65
  [ "$expected" = "$(opu_hash_string "$canonical")" ] || { opu_error 'Data Guard evidence checksum does not match its content'; return 65; }
}

opu_dataguard_require_recent() {
  local file=$1 field=$2 maximum=${OPU_DATAGUARD_MAX_EXECUTION_AGE_SECONDS:-300}
  [[ $maximum =~ ^[0-9]{1,4}$ ]] && [ "$maximum" -ge 1 ] && [ "$maximum" -le 3600 ] || {
    opu_error 'OPU_DATAGUARD_MAX_EXECUTION_AGE_SECONDS must be 1..3600'; return 65;
  }
  jq -e --arg field "$field" --argjson maximum "$maximum" '
    (now - (.[$field] | fromdateiso8601)) as $age | $age >= 0 and $age <= $maximum
  ' "$file" >/dev/null || { opu_error 'Data Guard execution evidence is stale, future-dated or missing its timestamp'; return 65; }
}

opu_dataguard_validate_output() {
  [ -n "${1:-}" ] || return 0
  opu_validate_absolute_path "$1" output || return $?
  [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -f "$1" ]; } || {
    opu_error 'Data Guard output must be a regular file, not a link or special file'; return 65;
  }
}

opu_dataguard_broker_execute() {
  local operation=$1 target=$2 home sid broker proof configuration database rc=0
  home=$(jq -er '.target.oracle_home' "$OBSERVE") || return 65
  sid=$(jq -er '.target.oracle_sid' "$OBSERVE") || return 65
  opu_validate_absolute_path "$home" observed-oracle-home || return $?
  opu_validate_identifier "$sid" observed-oracle-sid || return $?
  broker=$home/bin/dgmgrl
  [ -x "$broker" ] || { opu_error 'dgmgrl is missing from the observed Oracle home'; return 69; }
  export ORACLE_HOME="$home" ORACLE_SID="$sid"
  if [ -n "$OUT" ]; then
    mkdir -p "$(dirname -- "$OUT")" || return 73
    EXEC_LOG=$(mktemp "${OUT}.dgmgrl.XXXXXX") || return 73
  else
    EXEC_LOG=$(mktemp "${TMPDIR:-/tmp}/opu-dg-broker.XXXXXX") || return 73
  fi
  "$broker" -silent / "$operation '$target'" >"$EXEC_LOG" 2>&1 || rc=$?
  # A disconnect/error can occur after the role mutation. It is not safe to
  # retry blindly, and exit zero alone does not establish a terminal result.
  EXEC_STATUS=unknown
  [ "$rc" -eq 0 ] || return 0
  if grep -Eiq 'ORA-[0-9]+|DGM-[0-9]+|error' "$EXEC_LOG"; then return 0; fi
  case "$operation" in
    SWITCHOVER\ TO) proof="Switchover succeeded, new primary is \"$target\"" ;;
    REINSTATE\ DATABASE) proof="Reinstatement of database \"$target\" succeeded" ;;
    *) return 64 ;;
  esac
  grep -Fiq "$proof" "$EXEC_LOG" || return 0
  configuration=$("$broker" -silent / 'SHOW CONFIGURATION;' 2>&1) || rc=$?
  printf '\nSHOW CONFIGURATION:\n%s\n' "$configuration" >>"$EXEC_LOG"
  [ "$rc" -eq 0 ] || return 0
  database=$("$broker" -silent / "SHOW DATABASE '$target';" 2>&1) || rc=$?
  printf '\nSHOW DATABASE %s:\n%s\n' "$target" "$database" >>"$EXEC_LOG"
  [ "$rc" -eq 0 ] || return 0
  if grep -Eiq 'ORA-[0-9]+|DGM-[0-9]+|error' <<<"$configuration $database"; then return 0; fi
  grep -Eq '^[[:space:]]*SUCCESS([[:space:]]|$)' <<<"$configuration" || return 0
  grep -Eq '^[[:space:]]*SUCCESS([[:space:]]|$)' <<<"$database" || return 0
  case "$operation" in
    SWITCHOVER\ TO) grep -Eq '^[[:space:]]*Role:[[:space:]]+PRIMARY[[:space:]]*$' <<<"$database" || return 0 ;;
    REINSTATE\ DATABASE) grep -Eq '^[[:space:]]*Role:[[:space:]]+(PHYSICAL|LOGICAL) STANDBY[[:space:]]*$' <<<"$database" || return 0 ;;
  esac
  # shellcheck disable=SC2034 # Consumed by the sourcing executor's main().
  EXEC_STATUS=succeeded
}

opu_dataguard_publish() {
  local source=$1 destination=${2:-}
  if [ -n "$destination" ]; then
    opu_dataguard_validate_output "$destination" || return $?
    opu_python - "$source" "$destination" <<'PY'
import os
import stat
import sys
import tempfile
source, destination = sys.argv[1:]
temporary = None
try:
    if os.path.lexists(destination) and not stat.S_ISREG(os.lstat(destination).st_mode):
        raise ValueError("output is not a regular file")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".opu-dg-output-", dir=os.path.dirname(destination))
    with os.fdopen(fd, "wb") as target, open(source, "rb") as origin:
        target.write(origin.read())
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, destination)
    temporary = None
except (OSError, ValueError) as exc:
    print("Cannot publish Data Guard output: " + str(exc), file=sys.stderr)
    sys.exit(74)
finally:
    if temporary is not None:
        os.unlink(temporary)
PY
    # Function callers may be in conditional contexts where errexit is off.
    local result=$?
    [ "$result" -eq 0 ] || return "$result"
  fi
  cat "$source"
}
