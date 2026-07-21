#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-database-rolling.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# macOS does not provide util-linux flock; supply a narrow test double that
# acquires a real BSD flock on the inherited descriptor, matching the pattern
# used by tests/single_instance_patch.sh.
if ! command -v flock >/dev/null 2>&1; then
  mkdir -p "$TMP/test-bin"
  OPU_DATABASE_ROLLING_TEST_FLOCK="$TMP/test-bin/flock"
  export OPU_DATABASE_ROLLING_TEST_FLOCK
  cat >"$TMP/test-bin/flock" <<'PY'
#!/usr/bin/python3
import fcntl
import sys

if sys.argv[1:] != ["-n", "9"]:
    raise SystemExit(64)
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
PY
  chmod 0750 "$TMP/test-bin/flock"
fi

TOOL="$ROOT/bin/opu-database-rolling-patch"
NODE=$(hostname -s 2>/dev/null || hostname)
DB_HOME="$TMP/db-home"
GRID_HOME="$TMP/grid-home"
PATCH_DIR="$TMP/patch/39034528"
RUNTIME="$TMP/runtime"
LOG_ROOT="$TMP/logs"
SID=ORCL1

mkdir -p "$DB_HOME/bin" "$DB_HOME/OPatch" "$GRID_HOME/bin" "$PATCH_DIR" "$RUNTIME" "$LOG_ROOT"
printf 'patchID="39034528"\n' >"$PATCH_DIR/etc-config-inventory.txt"
printf 'running\n' >"$RUNTIME/instance.state"

cat >"$GRID_HOME/bin/srvctl" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cmd=\${1:-}; obj=\${2:-}; shift 2 || true
case "\$cmd:\$obj" in
  config:database)
    printf 'Oracle home: $DB_HOME\n'
    ;;
  status:instance)
    if [ "\$(cat "$RUNTIME/instance.state")" = running ]; then
      printf 'Instance $SID is running on node $NODE\n'
    else
      printf 'Instance $SID is not running on node $NODE\n'
    fi
    ;;
  stop:instance)
    printf 'stopped\n' >"$RUNTIME/instance.state"
    printf 'x' >>"$RUNTIME/stop-count"
    ;;
  start:instance)
    printf 'running\n' >"$RUNTIME/instance.state"
    ;;
  status:database)
    :
    ;;
  *)
    printf 'unsupported fake srvctl operation: %s %s\n' "\$cmd" "\$obj" >&2
    exit 64
    ;;
esac
EOF
chmod 700 "$GRID_HOME/bin/srvctl"

cat >"$DB_HOME/bin/sqlplus" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
printf '%s\n' 'PRIMARY|READ WRITE|ARCHIVELOG' '$SID|OPEN'
EOF
chmod 700 "$DB_HOME/bin/sqlplus"

cat >"$DB_HOME/OPatch/opatch" <<EOF
#!/usr/bin/env bash
set -euo pipefail
patch_state="$RUNTIME/patch.state"
case "\${1:-}" in
  lspatches)
    [ -f "\$patch_state" ] && printf '39034528;Database Release Update\n' || true
    ;;
  prereq)
    [ ! -f "$RUNTIME/fail-prereq" ] || { echo 'simulated prereq failure' >&2; exit 73; }
    printf 'Prereq check passed.\n'
    ;;
  apply)
    [ ! -f "$RUNTIME/fail-opatch" ] || { echo 'simulated OPatch apply failure' >&2; exit 73; }
    printf 'installed\n' >"\$patch_state"
    printf 'OPatch succeeded.\n'
    ;;
  rollback)
    rm -f "\$patch_state"
    printf 'OPatch succeeded.\n'
    ;;
  *)
    echo "unsupported fake opatch operation: \$1" >&2; exit 64
    ;;
esac
EOF
chmod 700 "$DB_HOME/OPatch/opatch"

cat >"$DB_HOME/OPatch/datapatch" <<EOF
#!/usr/bin/env bash
set -euo pipefail
[ ! -f "$RUNTIME/fail-datapatch" ] || { echo 'simulated datapatch failure' >&2; exit 1; }
printf 'datapatch succeeded.\n' >"$RUNTIME/datapatch.ran"
EOF
chmod 700 "$DB_HOME/OPatch/datapatch"

printf '%s\n' "$NODE" >"$TMP/nodes"
BACKUP_SHA=$(printf 'backup-evidence' | sha256sum | awk '{print $1}')

tool() {
  OPU_DATABASE_ROLLING_TEST_MODE=1 "$TOOL" --database ORCL --grid-home "$GRID_HOME" \
    --nodes "$TMP/nodes" --log-root "$LOG_ROOT" "$@"
}

# --- guard rails ---
if tool --patch-dir "$PATCH_DIR" --mode bogus >/dev/null 2>&1; then
  echo 'invalid mode was accepted' >&2; exit 1
fi
if tool --patch-dir "$PATCH_DIR" --mode apply >/dev/null 2>&1; then
  echo 'apply without --approve was accepted' >&2; exit 1
fi
if tool --patch-dir "$PATCH_DIR" --mode apply --approve --backup-evidence-sha256 not-a-digest >/dev/null 2>&1; then
  echo 'apply with a malformed backup-evidence digest was accepted' >&2; exit 1
fi

# --- analyze (read-only) ---
tool --patch-dir "$PATCH_DIR" --mode analyze >"$TMP/analyze.log"
grep -q 'checking DB-home patch conflicts' "$TMP/analyze.log"
[ ! -f "$RUNTIME/patch.state" ]

# --- apply ---
tool --patch-dir "$PATCH_DIR" --mode apply --approve --backup-evidence-sha256 "$BACKUP_SHA" >"$TMP/apply.log"
[ -f "$RUNTIME/patch.state" ]
[ -f "$RUNTIME/datapatch.ran" ]
[ "$(cat "$RUNTIME/instance.state")" = running ]
grep -q 'completed successfully' "$TMP/apply.log"

# --- apply is idempotent when already installed ---
tool --patch-dir "$PATCH_DIR" --mode apply --approve --backup-evidence-sha256 "$BACKUP_SHA" >"$TMP/apply-again.log"
grep -q 'already installed' "$TMP/apply-again.log"

# --- rollback ---
tool --patch-id 39034528 --mode rollback --approve --backup-evidence-sha256 "$BACKUP_SHA" >"$TMP/rollback.log"
[ ! -f "$RUNTIME/patch.state" ]
[ "$(cat "$RUNTIME/instance.state")" = running ]
grep -q 'completed successfully' "$TMP/rollback.log"

# --- a failed OPatch apply leaves the instance stopped, and does not retry
# blindly: preflight re-probes real instance state on every invocation, so an
# identical retry while the instance is still down is refused rather than
# silently skipping ahead. ---
rm -rf "$LOG_ROOT"; mkdir -p "$LOG_ROOT"
: >"$RUNTIME/fail-opatch"
if tool --patch-dir "$PATCH_DIR" --mode apply --approve --backup-evidence-sha256 "$BACKUP_SHA" >"$TMP/apply-fail.log" 2>&1; then
  echo 'OPatch failure was not surfaced' >&2; exit 1
fi
[ "$(cat "$RUNTIME/instance.state")" = stopped ]
rm -f "$RUNTIME/fail-opatch"
if tool --patch-dir "$PATCH_DIR" --mode apply --approve --backup-evidence-sha256 "$BACKUP_SHA" >"$TMP/apply-retry-while-down.log" 2>&1; then
  echo 'retry was accepted while the instance was still down' >&2; exit 1
fi
grep -q 'database instance is not running' "$TMP/apply-retry-while-down.log"

# Once the instance is confirmed running again (an operator/other-tool action
# outside this script's scope), the same command resumes: the already-done
# "stopped" step is skipped and only OPatch and start are retried.
stop_count_before=$(wc -c <"$RUNTIME/stop-count" | tr -d ' ')
printf 'running\n' >"$RUNTIME/instance.state"
tool --patch-dir "$PATCH_DIR" --mode apply --approve --backup-evidence-sha256 "$BACKUP_SHA" >"$TMP/apply-resume.log"
[ -f "$RUNTIME/patch.state" ]
[ "$(cat "$RUNTIME/instance.state")" = running ]
stop_count_after=$(wc -c <"$RUNTIME/stop-count" | tr -d ' ')
[ "$stop_count_after" = "$stop_count_before" ] || { echo 'resume re-ran the already-completed stop step' >&2; exit 1; }

printf '%s\n' 'database rolling patch utility test passed'
