#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/opu/python.sh
. "$ROOT/lib/opu/python.sh"
REAL_PYTHON=$(opu_find_python)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'python runtime regression: %s\n' "$*" >&2; exit 1; }
mkdir -p "$TMP/old only" "$TMP/versioned" "$TMP/default" "$TMP/empty"
printf '#!/bin/sh\nexit 69\n' > "$TMP/old only/python3"
chmod +x "$TMP/old only/python3"
cp "$TMP/old only/python3" "$TMP/versioned/python3"
ln -s "$REAL_PYTHON" "$TMP/versioned/python3.9"
ln -s "$REAL_PYTHON" "$TMP/versioned/python3.12"
printf '#!/bin/sh\nexit 88\n' > "$TMP/versioned/python3.14"
chmod +x "$TMP/versioned/python3.14"
ln -s "$REAL_PYTHON" "$TMP/default/python3"
ln -s "$REAL_PYTHON" "$TMP/default/python3.14"

# An older default and a broken newer candidate must not hide a supported
# versioned interpreter. A supported default keeps its normal precedence.
selected=$(PATH="$TMP/versioned" opu_find_python)
[ "$selected" = "$TMP/versioned/python3.12" ] || fail "wrong fallback: $selected"
selected=$(PATH="$TMP/default" opu_find_python)
[ "$selected" = "$TMP/default/python3" ] || fail "default lost precedence"

# Selection must execute the chosen interpreter with unmodified stdin,
# arguments and exit status; spaces in the interpreter path are supported.
mkdir -p "$TMP/runtime with spaces"
ln -s "$REAL_PYTHON" "$TMP/runtime with spaces/python3.9"
output=$(printf '%s' 'input bytes' | PATH="$TMP/runtime with spaces" opu_python -c \
  'import sys; print(sys.stdin.read() + ":" + sys.argv[1])' 'argument with spaces')
[ "$output" = 'input bytes:argument with spaces' ] || fail 'stdin or arguments changed'
if PATH="$TMP/versioned" opu_python -c 'raise SystemExit(23)'; then
  fail 'interpreter failure became success'
else
  [ "$?" -eq 23 ] || fail 'interpreter exit status changed'
fi

# Shell functions or an environment setting cannot substitute an unchecked
# executable for the interpreter found on the caller's PATH.
# shellcheck disable=SC2317,SC2329 # This guard fixture must remain uncalled by opu_python.
python3() { fail 'invoked a shell function instead of the executable'; }
output=$(OPU_PYTHON=/does/not/exist PATH="$TMP/default" opu_python -c 'print("verified")')
[ "$output" = verified ] || fail 'an environment override changed selection'
unset -f python3

for directory in 'old only' empty; do
  if PATH="$TMP/$directory" opu_python -c 'print("must not run")' >"$TMP/out" 2>"$TMP/err"; then
    fail "unsupported runtime was accepted: $directory"
  else
    [ "$?" -eq 69 ] || fail 'missing runtime did not return 69'
  fi
  [ ! -s "$TMP/out" ] || fail 'missing runtime wrote to stdout'
  grep -q 'Python 3.9 or newer is required; install' "$TMP/err" || fail 'missing actionable diagnostic'
done

printf '%s\n' 'Python runtime selection passed'
