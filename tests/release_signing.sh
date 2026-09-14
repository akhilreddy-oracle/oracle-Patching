#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-release.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
chmod +x "$ROOT/scripts/generate_sbom.sh" "$ROOT/scripts/sign_release.sh" "$ROOT/scripts/verify_release.sh"
"$ROOT/scripts/sign_release.sh" "$TMP"
"$ROOT/scripts/verify_release.sh" "$TMP" "$TMP/lab-signing.pub"
jq -e '.bomFormat == "CycloneDX" and (.components | length) > 10' "$TMP/sbom.json" >/dev/null
mkdir "$TMP/extracted"
tar -xzf "$TMP/opu-source.tgz" -C "$TMP/extracted"
"$TMP/extracted/bin/opu-agent" operations >/dev/null
OPU_AGENT_QUEUE_DIR="$TMP/queue" OPU_AGENT_ENROLLMENT_REQUIRED=0 "$TMP/extracted/bin/opu-agent-work-pull" --node node1 --agent-id agent1 | jq -e '.status == "idle"' >/dev/null
jq -e 'any(.components[]; .name == "operations/discovery/host.sh") and any(.components[]; .name == "webapp/agent_worker.py")' "$TMP/sbom.json" >/dev/null
python3 -B - "$TMP" <<'PY_VERIFY_SBOM'
import hashlib, json, sys, tarfile
from pathlib import Path
root = Path(sys.argv[1])
expected = {item['name']: item['hashes'][0]['content'] for item in json.loads((root / 'sbom.json').read_text())['components']}
with tarfile.open(root / 'opu-source.tgz', 'r:gz') as archive:
    actual = {m.name: hashlib.sha256(archive.extractfile(m).read()).hexdigest() for m in archive if m.isfile()}
assert expected == actual, 'SBOM must cover the exact captured release bytes'
PY_VERIFY_SBOM
printf '%s\n' 'release SBOM/sign/verify and extracted runtime test passed'
