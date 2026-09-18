#!/usr/bin/env bash
# Use a supported interpreter already available on the caller's PATH. Keep
# system python3 and alternatives unchanged, including on Oracle Linux 8.

opu_find_python() {
  local candidate executable
  for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if executable=$(type -P -- "$candidate") &&
       "$executable" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 69)' >/dev/null 2>&1; then
      printf '%s\n' "$executable"
      return 0
    fi
  done
  printf '%s\n' 'opu-agent: Python 3.9 or newer is required; install python3 or a versioned python3.9 through python3.14 executable on PATH.' >&2
  return 69
}

opu_python() {
  local executable
  executable=$(opu_find_python) || return $?
  "$executable" -B "$@"
}
