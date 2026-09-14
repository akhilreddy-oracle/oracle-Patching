#!/usr/bin/env bash
# Existing adapter suites run independent simulated estates in one temporary
# tree. Preserve their completed assertions/evidence, then retire unfinished
# scenario plans before resetting the simulated estate for the next scenario.
# Production reservations have no equivalent automatic cleanup.
retire_fixture_plans() {
  local state_root=$1 state_file state plan_id
  case "$state_root" in "$TMP"/*) ;; *) printf 'refusing to retire non-fixture plan state\n' >&2; return 65;; esac
  mkdir -p "$state_root/retired-fixtures"
  for state_file in "$state_root"/plans/*/state; do
    [ -f "$state_file" ] || continue
    IFS= read -r state <"$state_file"
    case "$state" in
      running|paused)
        plan_id=$(basename -- "$(dirname -- "$state_file")")
        mv "$(dirname -- "$state_file")" "$state_root/retired-fixtures/$plan_id"
        ;;
    esac
  done
  rm -f "$state_root/target-reservations.json"
}
