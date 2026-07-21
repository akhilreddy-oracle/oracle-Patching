#!/usr/bin/env bash
set -euo pipefail
output=''
while [ "$#" -gt 0 ]; do
  case "$1" in --output) output=${2:-}; shift 2;; --pretty) shift;; *) exit 64;; esac
done
[ -n "$output" ]
jq --arg collected "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" '.collected_at=$collected' "$OPU_TEST_SNAPSHOT" >"$output"
cat "$output"
