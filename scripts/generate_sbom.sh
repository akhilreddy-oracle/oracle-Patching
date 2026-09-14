#!/usr/bin/env bash
# Inventory the release payload, including new source and the agent runtime.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
OUT=${1:-"$ROOT/dist/sbom.json"}
python3 -B - "$ROOT" "$OUT" "${2:-}" <<'PY_SBOM'
import datetime
import hashlib
import json
from pathlib import Path
import sys
import tarfile
root, out = map(Path, sys.argv[1:3])
components = []
def component(name, content):
    return {'type': 'file', 'name': name, 'bom-ref': name,
            'hashes': [{'alg': 'SHA-256', 'content': hashlib.sha256(content).hexdigest()}]}

if sys.argv[3]:
    with tarfile.open(sys.argv[3], 'r:gz') as archive:
        for member in archive:
            if member.isfile():
                components.append(component(member.name, archive.extractfile(member).read()))
else:
    for name in ('bin', 'lib', 'operations', 'webapp', 'contracts', 'scripts', 'docs', 'tests', 'Makefile', 'README.md'):
        base = root / name
        paths = sorted(base.rglob('*')) if base.is_dir() else [base]
        for path in paths:
            rel = path.relative_to(root)
            if not path.is_file() or path.is_symlink() or path.suffix == '.pyc' or '__pycache__' in rel.parts:
                continue
            if rel.parts[:2] == ('webapp', 'var'):
                continue
            components.append(component(rel.as_posix(), path.read_bytes()))
sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'version': 1,
        'metadata': {'timestamp': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'component': {'type': 'application', 'name': 'oracle-patching-utility', 'version': '0.1.0'}},
        'components': components}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(sbom, indent=2) + '\n')
print(f'wrote {out} ({len(components)} components)')
PY_SBOM
