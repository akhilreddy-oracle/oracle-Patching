"""Readiness-evidence pipeline: one function per bin/opu-* stage.

Mirrors docs/HIGH_ASSURANCE_ACCEPTANCE.md's evidence chain exactly. Each step
reads whatever evidence files earlier steps already produced, runs the real
CLI tool (locally for pure JSON transforms, over SSH for anything touching
the target filesystem/Oracle home), and caches the result. Nothing here
re-derives or overrides what a tool decided — a step that finds its inputs
missing fails with an instructive message rather than guessing.
"""
from __future__ import annotations

import json

import evidence
import localtools
import remote

REMOTE_SCRATCH_DIR = "/tmp/opu-webapp-evidence/{host_id}"


def _require(path, tool: str, label: str) -> None:
    if not path.is_file():
        raise localtools.LocalToolError(tool, f"Run {label} first — no cached evidence for this host yet.")


def step_reconcile(host_id: str, host: dict, body: dict) -> dict:
    snapshot = evidence.evidence_path(host_id, "snapshot")
    _require(snapshot, "opu-snapshot-reconcile", "discovery")
    result = localtools.run_tool("opu-snapshot-reconcile", ["--snapshot", str(snapshot)])
    evidence.write_evidence(host_id, "reconciliation", result)
    return result


def step_artifact_inspect(host_id: str, host: dict, body: dict) -> dict:
    artifact_dir = (body.get("artifact_dir") or "").strip()
    if not artifact_dir.startswith("/"):
        raise remote.RemoteError("invalid_input", "artifact_dir must be an absolute path on the target host")
    argv = [f"{host['remote_root']}/bin/opu-artifact-inspect", "--artifact", artifact_dir]
    stdout = remote.run_remote(host["ssh_alias"], argv, timeout=60)
    result = json.loads(stdout)
    evidence.write_evidence(host_id, "artifact", result)
    return result


def step_procedure_validate(host_id: str, host: dict, body: dict) -> dict:
    procedure_input = body.get("procedure")
    if not isinstance(procedure_input, dict):
        raise localtools.LocalToolError("opu-procedure-validate", "Missing 'procedure' document in request body.")
    artifact = evidence.evidence_path(host_id, "artifact")
    _require(artifact, "opu-procedure-validate", "artifact-inspect")
    procedure_path = evidence.write_evidence(host_id, "procedure_input", procedure_input)
    result = localtools.run_tool("opu-procedure-validate", ["--procedure", str(procedure_path), "--artifact", str(artifact)])
    evidence.write_evidence(host_id, "procedure", result)
    return result


def step_compatibility_collect(host_id: str, host: dict, body: dict) -> dict:
    artifact_dir = (body.get("artifact_dir") or "").strip()
    if not artifact_dir.startswith("/"):
        raise remote.RemoteError("invalid_input", "artifact_dir must be an absolute path on the target host")

    snapshot = evidence.evidence_path(host_id, "snapshot")
    artifact = evidence.evidence_path(host_id, "artifact")
    procedure = evidence.evidence_path(host_id, "procedure")
    _require(snapshot, "opu-opatch-compatibility-collect", "discovery")
    _require(artifact, "opu-opatch-compatibility-collect", "artifact-inspect")
    _require(procedure, "opu-opatch-compatibility-collect", "procedure-validate")

    scratch = REMOTE_SCRATCH_DIR.format(host_id=host_id)
    remote.push_file(host["ssh_alias"], f"{scratch}/snapshot.json", snapshot.read_bytes())
    remote.push_file(host["ssh_alias"], f"{scratch}/artifact.json", artifact.read_bytes())
    remote.push_file(host["ssh_alias"], f"{scratch}/procedure.json", procedure.read_bytes())

    argv = [
        f"{host['remote_root']}/bin/opu-opatch-compatibility-collect",
        "--snapshot", f"{scratch}/snapshot.json",
        "--artifact", artifact_dir,
        "--artifact-manifest", f"{scratch}/artifact.json",
        "--procedure-validation", f"{scratch}/procedure.json",
    ]
    stdout = remote.run_remote(host["ssh_alias"], argv, timeout=120)
    result = json.loads(stdout)
    evidence.write_evidence(host_id, "compatibility", result)
    return result


def step_compatibility_reconcile(host_id: str, host: dict, body: dict) -> dict:
    reconciliation = evidence.evidence_path(host_id, "reconciliation")
    procedure = evidence.evidence_path(host_id, "procedure")
    compatibility = evidence.evidence_path(host_id, "compatibility")
    _require(reconciliation, "opu-compatibility-reconcile", "reconcile")
    _require(procedure, "opu-compatibility-reconcile", "procedure-validate")
    _require(compatibility, "opu-compatibility-reconcile", "compatibility-collect")

    result = localtools.run_tool(
        "opu-compatibility-reconcile",
        [
            "--reconciliation", str(reconciliation),
            "--procedure-validation", str(procedure),
            "--compatibility", str(compatibility),
        ],
    )
    evidence.write_evidence(host_id, "compatibility_reconciliation", result)
    return result


def step_readiness_evaluate(host_id: str, host: dict, body: dict) -> dict:
    policy = body.get("policy")
    if not isinstance(policy, dict):
        raise localtools.LocalToolError("opu-readiness-evaluate", "Missing 'policy' document in request body.")

    reconciliation = evidence.evidence_path(host_id, "reconciliation")
    snapshot = evidence.evidence_path(host_id, "snapshot")
    artifact = evidence.evidence_path(host_id, "artifact")
    procedure = evidence.evidence_path(host_id, "procedure")
    compat_reconciliation = evidence.evidence_path(host_id, "compatibility_reconciliation")
    _require(reconciliation, "opu-readiness-evaluate", "reconcile")
    _require(snapshot, "opu-readiness-evaluate", "discovery")
    _require(artifact, "opu-readiness-evaluate", "artifact-inspect")
    _require(procedure, "opu-readiness-evaluate", "procedure-validate")
    _require(compat_reconciliation, "opu-readiness-evaluate", "compatibility-reconcile")

    policy_path = evidence.write_evidence(host_id, "policy", policy)
    result = localtools.run_tool(
        "opu-readiness-evaluate",
        [
            "--reconciliation", str(reconciliation),
            "--snapshot", str(snapshot),
            "--artifact", str(artifact),
            "--procedure-validation", str(procedure),
            "--compatibility", str(compat_reconciliation),
            "--policy", str(policy_path),
        ],
    )
    evidence.write_evidence(host_id, "readiness", result)
    return result


STEPS = {
    "reconcile": step_reconcile,
    "artifact-inspect": step_artifact_inspect,
    "procedure-validate": step_procedure_validate,
    "compatibility-collect": step_compatibility_collect,
    "compatibility-reconcile": step_compatibility_reconcile,
    "readiness-evaluate": step_readiness_evaluate,
}

STEP_ORDER = [
    "discovery",
    "reconcile",
    "artifact-inspect",
    "procedure-validate",
    "compatibility-collect",
    "compatibility-reconcile",
    "readiness-evaluate",
]

EVIDENCE_NAME_FOR_STEP = {
    "discovery": "snapshot",
    "reconcile": "reconciliation",
    "artifact-inspect": "artifact",
    "procedure-validate": "procedure",
    "compatibility-collect": "compatibility",
    "compatibility-reconcile": "compatibility_reconciliation",
    "readiness-evaluate": "readiness",
}


def pipeline_state(host_id: str) -> list[dict]:
    state = []
    for step in STEP_ORDER:
        payload = evidence.read_evidence(host_id, EVIDENCE_NAME_FOR_STEP[step])
        state.append({
            "step": step,
            "done": payload is not None,
            "status": (payload or {}).get("status"),
            "evidence": payload,
        })
    return state
