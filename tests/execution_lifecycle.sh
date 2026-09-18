#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
python3 "$ROOT/tests/execution_lifecycle.py"
exec python3 "$ROOT/tests/execution_finalization.py"
