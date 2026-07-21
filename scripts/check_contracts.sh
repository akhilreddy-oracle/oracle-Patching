#!/usr/bin/env bash
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$PROJECT_ROOT"

if [ ! -x .venv/bin/python ]; then
    cat >&2 <<'EOF'
contract validation skipped: no scripts/validate_contracts.py environment.
Set one up once with:
  python3 -m venv .venv
  ./.venv/bin/pip install -r scripts/requirements.txt
EOF
    exit 0
fi

.venv/bin/python scripts/validate_contracts.py
