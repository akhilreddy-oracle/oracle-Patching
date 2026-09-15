#!/usr/bin/env python3
"""Schema-check real fixture-mode collectors/evaluators/executor output."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp"))
import planctl


def invoke(tool: str, args: list[str], *, env: dict, accepted=(0,)) -> dict:
    process = subprocess.run([str(ROOT / "bin" / tool), *args], env=env, capture_output=True, text=True, timeout=120)
    if process.returncode not in accepted:
        raise RuntimeError(f"{tool} exited {process.returncode}: {process.stderr[-2000:]}")
    return json.loads(process.stdout)


def generate_outputs(base: Path) -> list[tuple[str, str, dict]]:
    env = {**os.environ, "OPU_TEST_MODE": "1", "OPU_TEST_KERNEL_NAME": "Linux", "OPU_TEST_KERNEL_RELEASE": "5.15.0-test", "OPU_TEST_ARCHITECTURE": "x86_64", "OPU_FS_ROOT": str(ROOT / "tests/fixtures/oracle-linux-8"), "OPU_STATE_DIR": str(base / "agent"), "OPU_PRODUCTION_MODE": "0", "OPU_TARGET_LOCK_DIR": str(base / "target-locks")}
    discovery = invoke("opu-agent", ["run", "--operation", "host.discover", "--operation-version", "1", "--task-id", "contract-host", "--idempotency-key", "contract-host"], env=env)["payload"]
    outputs = [("host discovery", "discovery/discovery-result-v1.schema.json", discovery)]
    with patch.object(planctl, "PLAN_STATE_DIR", base / "plans"), patch.object(planctl, "TESTMODE_DIR", base / "fixtures"), patch.object(planctl, "_execute_live", side_effect=AssertionError("runtime contracts must never execute against live hosts")), patch.dict(os.environ, env):
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        planctl.create_testmode_demo("runtime-contract", "requester", (now - timedelta(minutes=1)).strftime(fmt), (now + timedelta(hours=1)).strftime(fmt))
        fixture = base / "fixtures" / "runtime-contract"
        artifact = json.loads((fixture / "artifact.json").read_text())  # produced by opu-artifact-inspect
        outputs.append(("artifact inspection", "artifact/artifact-manifest-v1.schema.json", artifact))
        # Readiness inputs are fixtures; validate output from the real evaluator.
        # Keep original plan-bound files intact for the executor below.
        snapshot = json.loads((fixture / "topology-snapshot.json").read_text())
        reconciliation = json.loads((fixture / "reconciliation.json").read_text())
        home = copy.deepcopy(reconciliation["oracle_homes"][0])
        home["platform"]["source_sha256"] = "a" * 64
        home.update(patch_inventory_source="opatch_lsinventory_xml", opatch_inventory_xml_sha256="a" * 64)
        snapshot["oracle_homes"] = [home]
        snapshot["databases"] = [{"db_unique_name": "ORCL", "oracle_home": home["path"], "runtime": {"status": "complete", "database_role": "PRIMARY", "open_mode": "READ WRITE", "instance_state": "OPEN", "invalid_objects": 0, "sqlpatch_non_success": 0, "pdb_not_read_write": 0, "backup_age_minutes": 1, "fra_space_limit_bytes": 0, "fra_space_used_bytes": 0, "guaranteed_restore_points": 0}}]
        snapshot_path = base / "readiness-snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot))
        reconciliation["snapshot_evidence"] = [{"path": str(snapshot_path), "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest()}]
        reconciliation_path = base / "readiness-reconciliation.json"
        reconciliation_path.write_text(json.dumps(reconciliation))
        args = ["--snapshot", str(snapshot_path), "--reconciliation", str(reconciliation_path), "--artifact", str(fixture / "artifact.json"), "--procedure-validation", str(fixture / "procedure.json"), "--compatibility", str(fixture / "compatibility.json"), "--policy", str(fixture / "policy.json")]
        ready = invoke("opu-readiness-evaluate", args, env=env)
        if ready["status"] != "ready_for_approval":
            raise RuntimeError("positive readiness fixture did not pass")
        outputs.append(("ready evaluation", "readiness/patch-readiness-result-v1.schema.json", ready))
        snapshot["databases"][0]["runtime"]["invalid_objects"] = None
        snapshot_path.write_text(json.dumps(snapshot))
        reconciliation["snapshot_evidence"][0]["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        reconciliation_path.write_text(json.dumps(reconciliation))
        blocked = invoke("opu-readiness-evaluate", args, env=env, accepted=(2,))
        outputs.append(("blocked evaluation", "readiness/patch-readiness-result-v1.schema.json", blocked))
        planctl.approve("runtime-contract", "approver", "CONTRACT-TEST")
        planctl.authorize("runtime-contract", "operator")
        planctl.dispatch("runtime-contract", "operator")
        execution = planctl.execute_next_task("runtime-contract", "worker")
        outputs.append(("standalone precheck execution", "execution/standalone-database-execution-evidence-v1.schema.json", execution))
        task = planctl.list_tasks("runtime-contract")[0]
        custody = json.loads(Path(task["evidence_custody"]["manifest_path"]).read_text())
        outputs.append(("controller evidence custody", "execution/controller-evidence-custody-v1.schema.json", custody))
    return outputs


def check_runtime_contracts(validate_payload) -> list[str]:
    errors = []
    try:
        with tempfile.TemporaryDirectory(prefix="opu-runtime-contracts-") as directory:
            outputs = generate_outputs(Path(directory))
            for label, schema, payload in outputs:
                errors.extend(validate_payload(schema, payload, label))
                # Ensure required-field drift is caught by the same real-output gate.
                missing = copy.deepcopy(payload)
                missing.pop("schema_version", None)
                if not validate_payload(schema, missing, label):
                    errors.append(f"{label}: validator accepted missing schema_version")
            ready = next(payload for label, _, payload in outputs if label == "ready evaluation")
            malformed = {**ready, "evaluated_at": "not-a-timestamp"}
            if not validate_payload("readiness/patch-readiness-result-v1.schema.json", malformed, "invalid timestamp"):
                errors.append("runtime validator did not enforce date-time format")
            for label, schema, payload in outputs:
                malformed = copy.deepcopy(payload)
                if label == "host discovery":
                    malformed["coverage"] = {}
                elif label == "artifact inspection":
                    malformed["artifact"].pop("sha256")
                elif label in {"ready evaluation", "blocked evaluation"}:
                    malformed.pop("gates")
                elif label == "standalone precheck execution":
                    malformed.pop("postcondition")
                else:
                    malformed["files"] = "missing evidence files"
                if not validate_payload(schema, malformed, label):
                    errors.append(f"{label}: validator accepted missing required evidence or wrong shape")
                if label == "standalone precheck execution":
                    invalid_artifact = copy.deepcopy(payload)
                    invalid_artifact["artifacts"] = [{"path": "/fixture/log", "sha256": "invalid"}]
                    if not validate_payload(schema, invalid_artifact, label):
                        errors.append("standalone evidence accepted an invalid artifact digest")
    except Exception as exc:
        detail = getattr(exc, "stderr", "")
        errors.append(f"runtime contract generation failed: {exc}" + (f": {detail[-2000:]}" if detail else ""))
    return errors


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "scripts"))
    from validate_contracts import validate_payload
    failures = check_runtime_contracts(validate_payload)
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print("runtime contracts passed: discovery, artifact, readiness (ready/blocked), execution, custody")
    raise SystemExit(bool(failures))
