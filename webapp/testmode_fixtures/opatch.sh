#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$OPU_TEST_OPATCH_CALLS"
case "${1:-}" in
  version) printf '%s\n' 'OPatch Version: 12.2.0.1.51' ;;
  lspatches) [ -f "$OPU_TEST_PATCH_STATE" ] && printf '%s\n' '39034528;Database Release Update' || true ;;
  lsinventory)
    { [ "$#" -eq 5 ] || { [ "$#" -eq 7 ] && [ "$6" = -jdk ] && [ "$7" = "$ORACLE_HOME/jdk" ]; }; } && \
      [ "$2" = -xml ] && [ -n "$3" ] && [ "$4" = -oh ] && [ "$5" = "$ORACLE_HOME" ] || exit 64
    printf '<inventory><home>%s</home><patches>%s</patches></inventory>\n' "$ORACLE_HOME" "$(cat "$OPU_TEST_PATCH_STATE" 2>/dev/null || true)" >"$3"
    ;;
  prereq)
    case "${2:-}" in
      CheckPatchApplicableOnCurrentPlatform)
        [ ! -f "$OPU_TEST_FAIL_APPLICABILITY" ] || { printf 'Prerequisite check "CheckPatchApplicableOnCurrentPlatform" failed.\nOPatch failed with error code 73\n' >&2; exit 73; }
        printf '%s\n' 'Prereq "CheckPatchApplicableOnCurrentPlatform" passed.'
        ;;
      CheckConflictAgainstOHWithDetail) printf '%s\n' 'Prereq "CheckConflictAgainstOHWithDetail" passed.' ;;
      *) printf 'unsupported fake OPatch prerequisite: %s\n' "${2:-}" >&2; exit 64 ;;
    esac
    ;;
  apply)
    [ ! -f "$OPU_TEST_FAIL_OPATCH" ] || { printf 'simulated OPatch failure\n' >&2; exit 73; }
    printf '39034528\n' >"$OPU_TEST_PATCH_STATE"
    printf '%s\n' 'OPatch succeeded.'
    ;;
  rollback)
    [ ! -f "$OPU_TEST_FAIL_ROLLBACK" ] || { printf 'simulated OPatch rollback failure\n' >&2; exit 73; }
    rm -f "$OPU_TEST_PATCH_STATE"
    printf '%s\n' 'OPatch rollback succeeded.'
    ;;
  *) printf 'unsupported fake OPatch command: %s\n' "${1:-}" >&2; exit 64 ;;
esac
