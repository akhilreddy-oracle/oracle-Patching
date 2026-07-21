#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-grid-rolling.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# macOS does not provide util-linux flock; supply a narrow test double that
# acquires a real BSD flock on the inherited descriptor, matching the pattern
# used by tests/single_instance_patch.sh.
if ! command -v flock >/dev/null 2>&1; then
  mkdir -p "$TMP/test-bin"
  OPU_GRID_ROLLING_TEST_FLOCK="$TMP/test-bin/flock"
  export OPU_GRID_ROLLING_TEST_FLOCK
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

TOOL="$ROOT/bin/opu-grid-rolling-patch"
NODE=$(hostname -s 2>/dev/null || hostname)
GRID_HOME="$TMP/grid-home"
PATCH_DIR="$TMP/patch/39034528"
RUNTIME="$TMP/runtime"
LOG_ROOT="$TMP/logs"

mkdir -p "$GRID_HOME/bin" "$GRID_HOME/OPatch" "$GRID_HOME/crs/install" \
  "$GRID_HOME/rdbms/install" "$PATCH_DIR" "$RUNTIME" "$LOG_ROOT"
printf 'patchID="39034528"\n' >"$PATCH_DIR/etc-config-inventory.txt"

cat >"$GRID_HOME/bin/olsnodes" <<EOF
#!/usr/bin/env bash
printf '%s\tActive\n' '$NODE'
EOF
chmod 700 "$GRID_HOME/bin/olsnodes"

cat >"$GRID_HOME/bin/crsctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 700 "$GRID_HOME/bin/crsctl"

cat >"$GRID_HOME/OPatch/opatch" <<EOF
#!/usr/bin/env bash
set -euo pipefail
patch_state="$RUNTIME/patch.state"
case "\${1:-}" in
  lspatches)
    [ -f "\$patch_state" ] && printf '39034528;Grid Infrastructure Release Update\n' || true
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
chmod 700 "$GRID_HOME/OPatch/opatch"

cat >"$GRID_HOME/crs/install/rootcrs.sh" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
  -prepatch) printf 'x' >>"$RUNTIME/prepatch-count" ;;
  -postpatch) printf 'x' >>"$RUNTIME/postpatch-count" ;;
  *) exit 64 ;;
esac
EOF
chmod 700 "$GRID_HOME/crs/install/rootcrs.sh"

cat >"$GRID_HOME/rdbms/install/rootadd_rdbms.sh" <<EOF
#!/usr/bin/env bash
printf 'x' >>"$RUNTIME/rootadd-count"
EOF
chmod 700 "$GRID_HOME/rdbms/install/rootadd_rdbms.sh"

printf '%s\n' "$NODE" >"$TMP/nodes"

tool() {
  OPU_GRID_ROLLING_TEST_MODE=1 "$TOOL" --grid-home "$GRID_HOME" \
    --nodes "$TMP/nodes" --log-root "$LOG_ROOT" "$@"
}

# --- guard rails ---
if tool --patch-dir "$PATCH_DIR" --mode bogus >/dev/null 2>&1; then
  echo 'invalid mode was accepted' >&2; exit 1
fi
if tool --patch-dir "$PATCH_DIR" --mode apply >/dev/null 2>&1; then
  echo 'apply without --approve was accepted' >&2; exit 1
fi

# --- analyze (read-only) ---
tool --patch-dir "$PATCH_DIR" --mode analyze >"$TMP/analyze.log"
grep -q 'checking patch conflicts' "$TMP/analyze.log"
[ ! -f "$RUNTIME/patch.state" ]

# --- apply ---
tool --patch-dir "$PATCH_DIR" --mode apply --approve >"$TMP/apply.log"
[ -f "$RUNTIME/patch.state" ]
[ "$(cat "$RUNTIME/prepatch-count" 2>/dev/null | wc -c | tr -d ' ')" = 1 ]
[ "$(cat "$RUNTIME/postpatch-count" 2>/dev/null | wc -c | tr -d ' ')" = 1 ]
grep -q 'completed successfully' "$TMP/apply.log"

# --- apply is idempotent when already installed ---
tool --patch-dir "$PATCH_DIR" --mode apply --approve >"$TMP/apply-again.log"
grep -q 'already applied' "$TMP/apply-again.log"

# --- rollback ---
tool --patch-id 39034528 --mode rollback --approve >"$TMP/rollback.log"
[ ! -f "$RUNTIME/patch.state" ]
grep -q 'completed successfully' "$TMP/rollback.log"

# --- a failed OPatch apply is not blindly retried; a resumed run only
# re-runs the steps that were not already completed ---
rm -rf "$LOG_ROOT"; mkdir -p "$LOG_ROOT"
prepatch_count() { wc -c <"$RUNTIME/prepatch-count" 2>/dev/null | tr -d ' '; }
: >"$RUNTIME/fail-opatch"
prepatch_before_fail=$(prepatch_count)
if tool --patch-dir "$PATCH_DIR" --mode apply --approve >"$TMP/apply-fail.log" 2>&1; then
  echo 'OPatch failure was not surfaced' >&2; exit 1
fi
[ "$(prepatch_count)" = "$((prepatch_before_fail + 1))" ]
[ ! -f "$RUNTIME/patch.state" ]
rm -f "$RUNTIME/fail-opatch"
prepatch_before_resume=$(prepatch_count)
tool --patch-dir "$PATCH_DIR" --mode apply --approve >"$TMP/apply-resume.log"
[ -f "$RUNTIME/patch.state" ]
[ "$(prepatch_count)" = "$prepatch_before_resume" ] || {
  echo 'resume re-ran the already-completed prepatch step' >&2; exit 1
}

printf '%s\n' 'grid rolling patch utility test passed'
