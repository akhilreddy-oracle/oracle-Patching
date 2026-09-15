#!/usr/bin/env bash
# TEST_MODE only: exactly the shipped catcon/utlrp invocation, no script runner.
set -euo pipefail
[ "$#" -eq 9 ] && [ "$1" = "$ORACLE_HOME/rdbms/admin/catcon.pl" ] && \
  [ "$2" = -n ] && [ "$3" = 1 ] && [ "$4" = -e ] && [ "$5" = -b ] && \
  [ "$6" = utlrp ] && [ "$7" = -d ] && [ "$8" = "$ORACLE_HOME/rdbms/admin" ] && [ "$9" = utlrp.sql ] || exit 64
[ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1
printf '%s\n' 'ERRORS DURING RECOMPILATION' '0' >utlrp0.log
