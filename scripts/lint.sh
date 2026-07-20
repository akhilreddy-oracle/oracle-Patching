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
    printf '%s\n' 'shellcheck is not installed; Bash syntax validation completed.'
fi
