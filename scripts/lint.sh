#!/usr/bin/env bash

set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$PROJECT_ROOT"

find bin lib operations scripts tests -type f \( -name '*.sh' -o -path 'bin/opu-*' \) \
    -print | sort | while IFS= read -r file; do
    bash -n "$file"
done

if command -v shellcheck >/dev/null 2>&1; then
    find bin lib operations scripts tests -type f \( -name '*.sh' -o -path 'bin/opu-*' \) \
        -print0 | xargs -0 shellcheck
else
    printf '%s\n' 'shellcheck is required for the lint gate.' >&2
    exit 1
fi

python3 -B - <<'PYTHON_LINT'
import ast
from pathlib import Path
files = sorted(p for base in ('webapp', 'lib', 'scripts', 'tests') for p in Path(base).rglob('*.py')
               if not any(x in p.parts for x in ('var', '__pycache__')))
for path in files:
    ast.parse(path.read_text(), filename=str(path))
print(f'Python syntax passed: {len(files)} files')
PYTHON_LINT
command -v node >/dev/null 2>&1 || { printf '%s\n' 'node is required for JavaScript checks' >&2; exit 1; }
while IFS= read -r file; do
    node --input-type=module --check < "$file"
done < <(find webapp/static tests -type f \( -name '*.js' -o -name '*.mjs' \) | sort)
printf '%s\n' 'JavaScript syntax passed'
