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
import re
import shlex
import hashlib
import uuid

import discovery_phases
import evidence
import localtools
import remote
import tools_sync

REMOTE_SCRATCH_DIR = "/tmp/opu-webapp-evidence/{host_id}"
# Full topology discovery (opatch lsinventory -xml per home) commonly takes
# ~35–60s on lab RAC hosts; keep headroom above the webapp SSH default (45s).
DISCOVERY_TIMEOUT_SECONDS = 180
# Full RU media hashes tens of thousands of files under files/; lab runs ~2 min.
ARTIFACT_INSPECT_TIMEOUT_SECONDS = 300
# CheckPatchApplicable + CheckConflictAgainstOHWithDetail each take ~60–90s.
COMPATIBILITY_COLLECT_TIMEOUT_SECONDS = 360
RECOVERY_COLLECT_TIMEOUT_SECONDS = 3600

_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _require(path, tool: str, label: str) -> None:
    if not path.is_file():
        raise localtools.LocalToolError(tool, f"Run {label} first — no cached evidence for this host yet.")


def _configured_nodes(host: dict) -> list[dict]:
    """Return [{name, ssh_alias}] for live per-node discovery.

    hosts.json ``nodes`` is authoritative for RAC. When absent, the host-level
    ssh_alias is treated as a single-node estate.
    """
    raw = host.get("nodes")
    if not raw:
        alias = host.get("ssh_alias")
        if not alias:
            raise remote.RemoteError("invalid_host_config", f"Host {host.get('id')!r} has no ssh_alias")
        return [{"name": str(host.get("id")), "ssh_alias": str(alias)}]

    nodes: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise remote.RemoteError("invalid_host_config", "hosts.json nodes entries must be objects")
        name = str(entry.get("name") or "").split(".", 1)[0]
        if not name or not _NODE_NAME_RE.match(name):
            raise remote.RemoteError(
                "invalid_host_config",
                f"hosts.json node name is missing or unsupported: {entry.get('name')!r}",
            )
        if "ssh_alias" in entry:
            alias = entry.get("ssh_alias")
        else:
            alias = host.get("ssh_alias")
        if not alias:
            raise remote.RemoteError(
                "invalid_host_config",
                f"hosts.json node {name!r} has no ssh_alias; multi-node discovery requires SSH to every node",
            )
        nodes.append({"name": name, "ssh_alias": str(alias)})
    if not nodes:
        raise remote.RemoteError("invalid_host_config", f"Host {host.get('id')!r} has an empty nodes list")
    return nodes


def _snapshot_cli_args(host_id: str, tool: str) -> list[str]:
    paths = evidence.list_snapshot_paths(host_id)
    if not paths:
        raise localtools.LocalToolError(tool, "Run discovery first — no cached evidence for this host yet.")
    for path in paths:
        _require(path, tool, "discovery")
    args: list[str] = []
    for path in paths:
        args.extend(["--snapshot", str(path)])
    return args


def step_discovery(host_id: str, host: dict, body: dict) -> dict:
    """Live SSH topology discovery via opu-topology-discover on every configured node.

    RAC reconcile requires one immutable snapshot per active node. Discovery
    therefore SSHes each hosts.json node alias, stores ``snapshot_<node>.json``,
    keeps ``snapshot.json`` as the primary-node view for estate/UI phases, and
    writes ``snapshot_nodes.json`` listing what was captured.
    """
    nodes = _configured_nodes(host)
    tools_sync.ensure_host_tools(host)
    argv = [f"{host['remote_root']}/bin/opu-topology-discover", "--pretty"]
    sudo = bool(host.get("sudo"))
    index_nodes: list[dict] = []
    primary_payload: dict | None = None
    primary_alias = str(host.get("ssh_alias") or "")

    for node in nodes:
        try:
            payload = remote.run_remote_json(
                node["ssh_alias"],
                argv,
                timeout=DISCOVERY_TIMEOUT_SECONDS,
                sudo=sudo,
            )
        except remote.RemoteError as exc:
            raise remote.RemoteError(
                exc.error,
                (
                    f"Discovery failed on node {node['name']} "
                    f"(ssh_alias={node['ssh_alias']}): {exc.message}"
                ),
                stderr=exc.stderr,
            ) from exc

        evidence_name = evidence.node_snapshot_evidence_name(node["name"])
        evidence.write_evidence(host_id, evidence_name, payload)
        index_nodes.append(
            {
                "name": node["name"],
                "ssh_alias": node["ssh_alias"],
                "evidence": evidence_name,
                "host_name": (payload.get("host") or {}).get("name"),
                "collected_at": payload.get("collected_at"),
            }
        )
        if primary_payload is None or node["ssh_alias"] == primary_alias:
            primary_payload = payload

    if primary_payload is None:
        raise remote.RemoteError("discovery_failed", "No topology snapshots were collected")

    evidence.write_evidence(host_id, "snapshot", primary_payload)
    evidence.write_evidence(
        host_id,
        "snapshot_nodes",
        {"schema_version": "1.0", "nodes": index_nodes},
    )
    # New topology digests invalidate every digest-bound downstream document.
    # Leaving stale reconciliation/readiness "done" is what produces
    # snapshot_binding blockers after a rediscovery.
    for name in (
        "reconciliation",
        "compatibility",
        "compatibility_reconciliation",
        "readiness",
        "recovery",
    ):
        evidence.clear_evidence(host_id, name)
    return primary_payload


def step_reconcile(host_id: str, host: dict, body: dict) -> dict:
    args = _snapshot_cli_args(host_id, "opu-snapshot-reconcile")
    result = localtools.run_tool("opu-snapshot-reconcile", args)
    evidence.write_evidence(host_id, "reconciliation", result)
    # Reconcile reseals snapshot digests; readiness must be re-evaluated against them.
    for name in ("compatibility_reconciliation", "readiness"):
        evidence.clear_evidence(host_id, name)
    return result


def step_artifact_inspect(host_id: str, host: dict, body: dict) -> dict:
    artifact_dir = (body.get("artifact_dir") or "").strip()
    if not artifact_dir.startswith("/"):
        raise remote.RemoteError("invalid_input", "artifact_dir must be an absolute path on the target host")
    tools_sync.ensure_host_tools(host)
    argv = [f"{host['remote_root']}/bin/opu-artifact-inspect", "--artifact", artifact_dir]
    stdout = remote.run_remote(
        host["ssh_alias"], argv, timeout=ARTIFACT_INSPECT_TIMEOUT_SECONDS, sudo=bool(host.get("sudo")),
    )
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
    # A submitted draft supersedes the previous validation. A failed attempt
    # must not leave a green result (or downstream readiness) for older inputs.
    for name in ("procedure", "compatibility", "compatibility_reconciliation", "readiness"):
        evidence.clear_evidence(host_id, name)
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

    tools_sync.ensure_host_tools(host)
    scratch = REMOTE_SCRATCH_DIR.format(host_id=host_id)
    sudo = bool(host.get("sudo"))

    # OPatch prerequisites are node-local (each node has its own home and
    # staged media), so collect on every configured node and merge. The
    # compatibility reconciler flags any node without a result.
    nodes = _configured_nodes(host)
    merged: dict | None = None
    for node in nodes:
        alias = node["ssh_alias"]
        node_snapshot = evidence.evidence_path(host_id, evidence.node_snapshot_evidence_name(node["name"]))
        snapshot_bytes = node_snapshot.read_bytes() if node_snapshot.is_file() else snapshot.read_bytes()
        remote.push_file(alias, f"{scratch}/snapshot.json", snapshot_bytes)
        remote.push_file(alias, f"{scratch}/artifact.json", artifact.read_bytes())
        remote.push_file(alias, f"{scratch}/procedure.json", procedure.read_bytes())
        argv = [
            f"{host['remote_root']}/bin/opu-opatch-compatibility-collect",
            "--snapshot", f"{scratch}/snapshot.json",
            "--artifact", artifact_dir,
            "--artifact-manifest", f"{scratch}/artifact.json",
            "--procedure-validation", f"{scratch}/procedure.json",
        ]
        try:
            stdout = remote.run_remote(alias, argv, timeout=COMPATIBILITY_COLLECT_TIMEOUT_SECONDS, sudo=sudo)
        except remote.RemoteError as exc:
            raise remote.RemoteError(
                exc.error,
                f"OPatch compatibility collection failed on node {node['name']} (ssh_alias={alias}): {exc.message}",
                stderr=exc.stderr,
            ) from exc
        result = json.loads(stdout)
        if merged is None:
            merged = result
            if len(nodes) > 1:
                merged["findings"] = [f"{node['name']}: {f}" for f in (result.get("findings") or [])]
            continue
        merged["checks"] = [*(merged.get("checks") or []), *(result.get("checks") or [])]
        merged["findings"] = [*(merged.get("findings") or []), *(f"{node['name']}: {f}" for f in (result.get("findings") or []))]
        if result.get("status") != "passed":
            merged["status"] = "blocked"

    assert merged is not None
    evidence.write_evidence(host_id, "compatibility", merged)
    return merged


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
    artifact = evidence.evidence_path(host_id, "artifact")
    procedure = evidence.evidence_path(host_id, "procedure")
    compat_reconciliation = evidence.evidence_path(host_id, "compatibility_reconciliation")
    _require(reconciliation, "opu-readiness-evaluate", "reconcile")
    _require(artifact, "opu-readiness-evaluate", "artifact-inspect")
    _require(procedure, "opu-readiness-evaluate", "procedure-validate")
    _require(compat_reconciliation, "opu-readiness-evaluate", "compatibility-reconcile")

    policy_path = evidence.write_evidence(host_id, "policy", policy)
    recovery_args: list[str] = []
    recovery_path = evidence.evidence_path(host_id, "recovery")
    if (policy.get("recovery") or {}).get("require_backup", True) and recovery_path.is_file():
        recovery_args = ["--recovery-evidence", str(recovery_path)]
    result = localtools.run_tool(
        "opu-readiness-evaluate",
        [
            "--reconciliation", str(reconciliation),
            *_snapshot_cli_args(host_id, "opu-readiness-evaluate"),
            "--artifact", str(artifact),
            "--procedure-validation", str(procedure),
            "--compatibility", str(compat_reconciliation),
            "--policy", str(policy_path),
            *recovery_args,
        ],
    )
    evidence.write_evidence(host_id, "readiness", result)
    return result


def step_recovery_collect(host_id: str, host: dict, body: dict) -> dict:
    """Revalidate a completed managed backup against this exact app snapshot.

    Mirror the snapshot at its control-plane path, as live plan execution does.
    The native collector seals that same path and digest; never rewrite or
    re-sign its source_snapshot to conceal a different collection input.
    """
    import recoveryctl

    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise remote.RemoteError("invalid_input", "Select a completed live recovery request")
    for stale in ("recovery", "readiness", "recovery_selection"):
        evidence.clear_evidence(host_id, stale)
    request = recoveryctl.selection_status(request_id, host_id=host_id, host=host)
    if request.get("mode") != "live" or request.get("host_id") != host_id or request.get("state") != "completed":
        raise remote.RemoteError("invalid_recovery", "Recovery must be completed on this live host; demo or other-host evidence cannot be used")
    nodes = _configured_nodes(host)
    paths = evidence.list_snapshot_paths(host_id)
    if len(nodes) != 1 or len(paths) != 1:
        raise remote.RemoteError("unsupported_recovery", "Live recovery selection currently supports one standalone database node")
    snapshot_path = paths[0]
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_sha = hashlib.sha256(snapshot_bytes).hexdigest()
    snapshot = json.loads(snapshot_bytes)
    if (snapshot.get("cluster") or {}).get("status") != "unavailable":
        raise remote.RemoteError("unsupported_recovery", "A standalone topology snapshot is required")
    target = request.get("target") or {}
    database = target.get("database_unique_name")
    matches = [db for db in snapshot.get("databases", []) if db.get("db_unique_name") == database and db.get("oracle_home") == target.get("oracle_home")]
    if len(matches) != 1:
        raise remote.RemoteError("invalid_recovery", "Recovery request does not match the current database and Oracle home")
    backup_root = (request.get("result") or {}).get("backup_root")
    if not isinstance(backup_root, str) or not backup_root.startswith("/"):
        raise remote.RemoteError("invalid_recovery", "Completed recovery request has no validated backup root")
    tools_sync.ensure_host_tools(host)
    alias = nodes[0]["ssh_alias"]
    sudo = bool(host.get("sudo"))
    remote.push_file(alias, str(snapshot_path), snapshot_bytes, sudo=sudo)
    output = f"{REMOTE_SCRATCH_DIR.format(host_id=host_id)}/recovery-{uuid.uuid4().hex}.json"
    result = remote.run_remote_raw(alias, [
        f"{host['remote_root']}/bin/opu-recovery-evidence-collect",
        "--snapshot", str(snapshot_path), "--database", database,
        "--backup-root", backup_root, "--output", output,
    ], timeout=RECOVERY_COLLECT_TIMEOUT_SECONDS, sudo=sudo)
    if result.returncode != 0:
        raise remote.RemoteError("recovery_validation_failed", "Selected recovery set failed native validation", stderr=result.stderr.strip() or result.stdout[-4000:])
    payload = json.loads(result.stdout)
    if payload.get("status") != "passed" or payload.get("source_snapshot") != {"path": str(snapshot_path), "sha256": snapshot_sha}:
        raise remote.RemoteError("invalid_recovery", "Native recovery result is not bound to the supplied snapshot")
    collected_target = payload.get("target") or {}
    if any(collected_target.get(key) != target.get(key) for key in ("database_unique_name", "oracle_home", "owner")):
        raise remote.RemoteError("invalid_recovery", "Native recovery result targets a different database, home or owner")
    if hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != snapshot_sha:
        raise remote.RemoteError("snapshot_changed", "Discovery changed during backup validation; collect again against the current snapshot")
    evidence.write_evidence(host_id, "recovery", payload)
    evidence.write_evidence(host_id, "recovery_selection", {"request_id": request_id, "host_id": host_id,
        "backup_root": backup_root, "policy": request.get("preparation_policy")})
    return payload


def _refresh_procedure_input(host_id: str, procedure_input: dict) -> dict:
    """Rebind a saved procedure input to the artifact evidence just produced.

    Only the digests that opu-artifact-inspect derives (artifact sha, README
    sha) are refreshed; every operator-chosen field is kept as sealed.
    """
    artifact = (evidence.read_evidence(host_id, "artifact") or {}).get("artifact") or {}
    out = json.loads(json.dumps(procedure_input))
    if artifact.get("sha256"):
        out["artifact_sha256"] = artifact["sha256"]
    readmes = {r.get("path"): r.get("sha256") for r in artifact.get("readme_files") or [] if isinstance(r, dict)}
    refs = []
    for ref in out.get("oracle_references") or []:
        if isinstance(ref, dict) and ref.get("kind") == "patch_readme" and ref.get("identifier") in readmes:
            ref = {**ref, "sha256": readmes[ref["identifier"]]}
        refs.append(ref)
    if refs:
        out["oracle_references"] = refs
    return out


_CHAIN_OK = {
    "discovery": lambda r: True,
    "reconcile": lambda r: r.get("status") == "consistent",
    "artifact-inspect": lambda r: (r.get("artifact") or {}).get("status") == "ready_for_catalog",
    "procedure-validate": lambda r: r.get("status") == "ready_for_planning",
    "compatibility-collect": lambda r: r.get("status") == "passed",
    "compatibility-reconcile": lambda r: r.get("status") == "passed",
    "readiness-evaluate": lambda r: r.get("status") == "ready_for_approval",
    "recovery-collect": lambda r: r.get("status") == "passed",
}


def step_readiness_chain(host_id: str, host: dict, body: dict) -> dict:
    """Run discovery → … → readiness-evaluate back to back.

    Readiness policies bound snapshot age (commonly 30 min) and OPatch checks
    take minutes, so evaluating a hand-run chain often fails snapshot_freshness
    through no fault of the estate. This re-derives every document in one
    sitting using the operator's saved inputs (artifact path, procedure input,
    policy) unless overridden in the body, refreshing only tool-derived
    digests. Stops at the first blocked/failed step; every step's evidence is
    still written, so the stage cards show exactly where it stopped.
    """
    record = body.get("_record")
    log = record.log if record is not None and hasattr(record, "log") else (lambda line: None)

    artifact_dir = (body.get("artifact_dir") or "").strip()
    if not artifact_dir:
        artifact_dir = ((evidence.read_evidence(host_id, "artifact") or {}).get("artifact") or {}).get("path") or ""
    if not artifact_dir.startswith("/"):
        raise localtools.LocalToolError("readiness-chain", "No artifact path known for this host — run Artifact inspection once first.")
    procedure_input = body.get("procedure") if isinstance(body.get("procedure"), dict) else evidence.read_evidence(host_id, "procedure_input")
    if not isinstance(procedure_input, dict):
        raise localtools.LocalToolError("readiness-chain", "No procedure input saved for this host — run Procedure validation once first.")
    policy = body.get("policy") if isinstance(body.get("policy"), dict) else evidence.read_evidence(host_id, "policy")
    if not isinstance(policy, dict):
        raise localtools.LocalToolError("readiness-chain", "No readiness policy saved for this host — run Readiness evaluation once first.")

    plan = [
        ("discovery", lambda: step_discovery(host_id, host, {})),
        ("reconcile", lambda: step_reconcile(host_id, host, {})),
        ("artifact-inspect", lambda: step_artifact_inspect(host_id, host, {"artifact_dir": artifact_dir})),
        ("procedure-validate", lambda: step_procedure_validate(host_id, host, {"procedure": _refresh_procedure_input(host_id, procedure_input)})),
        ("compatibility-collect", lambda: step_compatibility_collect(host_id, host, {"artifact_dir": artifact_dir})),
        ("compatibility-reconcile", lambda: step_compatibility_reconcile(host_id, host, {})),
        ("readiness-evaluate", lambda: step_readiness_evaluate(host_id, host, {"policy": policy})),
    ]
    recovery_policy = policy.get("recovery") or {}
    selection = evidence.read_evidence(host_id, "recovery_selection") or {}
    if recovery_policy.get("require_backup", True) and selection.get("request_id"):
        plan.insert(-1, ("recovery-collect", lambda: step_recovery_collect(host_id, host, {"request_id": selection["request_id"]})))
    import time as _time

    steps: list[dict] = []
    started = _time.time()
    for name, fn in plan:
        log(f"[{_time.strftime('%H:%M:%S')}] {name}: running")
        t0 = _time.time()
        result = fn()
        status = _step_status(name, result)
        ok = _CHAIN_OK[name](result)
        steps.append({"step": name, "status": status, "ok": ok, "seconds": round(_time.time() - t0, 1)})
        log(f"[{_time.strftime('%H:%M:%S')}] {name}: {status or 'done'} ({steps[-1]['seconds']}s)")
        if not ok:
            findings = result.get("findings") if isinstance(result, dict) else None
            return {
                "schema_version": "1.0",
                "status": "blocked",
                "stopped_at": name,
                "steps": steps,
                "findings": findings if isinstance(findings, list) else [],
                "elapsed_seconds": round(_time.time() - started, 1),
                "readiness": result if name == "readiness-evaluate" else None,
            }
    return {
        "schema_version": "1.0",
        "status": "ready_for_approval",
        "stopped_at": None,
        "steps": steps,
        "findings": [],
        "elapsed_seconds": round(_time.time() - started, 1),
        "readiness": steps and evidence.read_evidence(host_id, "readiness"),
    }


# Replicating a full RU (~5 GB) through the control plane takes minutes.
STAGE_ARTIFACT_TIMEOUT_SECONDS = 3600
_OWNER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,31})?$")
# Evidence that binds to artifact contents; all of it is stale once media changes.
_ARTIFACT_BOUND_EVIDENCE = ("artifact", "procedure", "compatibility", "compatibility_reconciliation", "readiness")


def _stage_owner_from_snapshot(host_id: str) -> str | None:
    """Oracle Home owner the procedure targets (grid home or first database home)."""
    snapshot = evidence.read_evidence(host_id, "snapshot")
    if not isinstance(snapshot, dict):
        return None
    procedure = evidence.read_evidence(host_id, "procedure")
    family = ((procedure or {}).get("procedure") or {}).get("target", {}).get("family") if isinstance(procedure, dict) else None
    homes = {h.get("path"): h.get("owner") for h in snapshot.get("oracle_homes") or [] if isinstance(h, dict)}
    if family == "grid":
        grid_home = (snapshot.get("cluster") or {}).get("grid_home")
        owner = homes.get(grid_home)
        if owner and owner != "unknown":
            return str(owner)
    for db in snapshot.get("databases") or []:
        if isinstance(db, dict):
            owner = homes.get(db.get("oracle_home"))
            if owner and owner != "unknown":
                return str(owner)
    for owner in homes.values():
        if owner and owner != "unknown":
            return str(owner)
    return None


def probe_artifact_media(alias: str, artifact_dir: str, sudo: bool) -> dict:
    """Is complete media (metadata + files/ payload) present at artifact_dir on alias?"""
    script = (
        f"d={shlex.quote(artifact_dir)}; if [ ! -d \"$d\" ]; then echo MISSING; exit 0; fi; "
        f"if [ -f \"$d/etc/config/inventory.xml\" ] && [ -d \"$d/files\" ] && "
        f"[ -n \"$(find \"$d/files\" -type f -print -quit 2>/dev/null)\" ]; then echo COMPLETE; else echo INCOMPLETE; fi; "
        f"du -sk \"$d\" 2>/dev/null | awk '{{print $1*1024}}'; stat -c %U:%G \"$d\" 2>/dev/null"
    )
    result = remote.run_remote_shell(alias, script, timeout=90, sudo=sudo)
    if result.returncode != 0:
        raise remote.RemoteError("probe_failed", f"Could not inspect {artifact_dir} on {alias}", stderr=result.stderr.strip())
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    state = lines[0] if lines else "MISSING"
    out = {"ssh_alias": alias, "artifact_dir": artifact_dir, "state": state.lower(), "bytes": None, "owner": None}
    if state != "MISSING":
        try:
            out["bytes"] = int(lines[1]) if len(lines) > 1 else None
        except ValueError:
            out["bytes"] = None
        out["owner"] = lines[2] if len(lines) > 2 else None
    return out


def artifact_sources(host_id: str, host: dict, hosts: dict[str, dict], artifact_dir: str) -> dict:
    """Where complete media at artifact_dir exists across the estate, and what this host has."""
    if not artifact_dir.startswith("/"):
        raise remote.RemoteError("invalid_input", "artifact_dir must be an absolute path")
    targets = []
    for node in _configured_nodes(host):
        try:
            targets.append({"node": node["name"], **probe_artifact_media(node["ssh_alias"], artifact_dir, bool(host.get("sudo")))})
        except remote.RemoteError as exc:
            targets.append({"node": node["name"], "ssh_alias": node["ssh_alias"], "artifact_dir": artifact_dir, "state": "unreachable", "error": exc.message})
    sources = []
    for other_id, other in hosts.items():
        if other_id == host_id or not other.get("ssh_alias"):
            continue
        try:
            probe = probe_artifact_media(str(other["ssh_alias"]), artifact_dir, bool(other.get("sudo")))
        except remote.RemoteError as exc:
            probe = {"ssh_alias": other["ssh_alias"], "artifact_dir": artifact_dir, "state": "unreachable", "error": exc.message}
        sources.append({"host_id": other_id, "label": other.get("label") or other_id, **probe})
    return {
        "artifact_dir": artifact_dir,
        "owner": _stage_owner_from_snapshot(host_id),
        "targets": targets,
        "sources": sources,
    }


def step_stage_artifact(host_id: str, host: dict, body: dict) -> dict:
    """Stage complete patch media on every node of this host without a login.

    body: {artifact_dir, owner?, replace?, source: {host_id} | {zip_path}}
    - source.host_id: replicate ``artifact_dir`` from another managed host that
      holds complete media (tar stream through the control plane).
    - source.zip_path: unzip a patch zip already present on each node.
    """
    artifact_dir = (body.get("artifact_dir") or "").strip().rstrip("/")
    if not artifact_dir.startswith("/") or artifact_dir == "":
        raise remote.RemoteError("invalid_input", "artifact_dir must be an absolute path on the target host")
    owner = (body.get("owner") or "").strip() or _stage_owner_from_snapshot(host_id)
    if not owner:
        raise remote.RemoteError("invalid_input", "owner is required (run discovery first so the Oracle Home owner is known)")
    if not _OWNER_RE.match(owner):
        raise remote.RemoteError("invalid_input", f"owner is not a valid user[:group]: {owner!r}")
    replace = bool(body.get("replace"))
    source = body.get("source") or {}
    src_host_id = (source.get("host_id") or "").strip()
    zip_path = (source.get("zip_path") or "").strip()
    if bool(src_host_id) == bool(zip_path):
        raise remote.RemoteError("invalid_input", "source must be exactly one of {host_id} or {zip_path}")
    if zip_path and not zip_path.startswith("/"):
        raise remote.RemoteError("invalid_input", "zip_path must be an absolute path on the target host")

    hosts = body.get("_hosts") or {}
    src_host = None
    if src_host_id:
        src_host = hosts.get(src_host_id)
        if src_host is None or not src_host.get("ssh_alias"):
            raise remote.RemoteError("invalid_input", f"unknown source host: {src_host_id}")
        probe = probe_artifact_media(str(src_host["ssh_alias"]), artifact_dir, bool(src_host.get("sudo")))
        if probe["state"] != "complete":
            raise remote.RemoteError(
                "source_incomplete",
                f"{src_host_id} has no complete media at {artifact_dir} (state: {probe['state']}); pick a host that does",
            )

    transfer = (body.get("transfer") or "auto").strip().lower()
    if transfer not in ("auto", "direct", "relay"):
        raise remote.RemoteError("invalid_input", "transfer must be auto, direct or relay")

    tools_sync.ensure_host_tools(host)
    sudo = bool(host.get("sudo"))
    tool = f"{host['remote_root'].rstrip('/')}/bin/opu-artifact-stage"
    src_ips: list[str] = []
    if src_host is not None and transfer != "relay":
        try:
            src_ips = remote.host_ips(str(src_host["ssh_alias"]))
        except remote.RemoteError:
            src_ips = []
    results = []
    for node in _configured_nodes(host):
        alias = node["ssh_alias"]
        current = probe_artifact_media(alias, artifact_dir, sudo)
        if current["state"] == "complete" and not replace:
            results.append({"node": node["name"], "ssh_alias": alias, "status": "already_complete", "bytes": current.get("bytes")})
            continue
        dst_argv = [tool, "--artifact", artifact_dir, "--owner", owner]
        if replace:
            dst_argv.append("--replace")
        path_used = "zip"
        if src_host is not None:
            parent, _, name = artifact_dir.rpartition("/")
            src_argv = ["tar", "-C", parent or "/", "-cf", "-", name]
            src_alias = str(src_host["ssh_alias"])
            src_sudo = bool(src_host.get("sudo"))
            proc = None
            # Prefer the hosts' own network: relaying a multi-GB RU through the
            # control plane is an order of magnitude slower.
            if transfer != "relay" and src_ips:
                try:
                    dst_ip = remote.reachable_from(src_alias, remote.host_ips(alias))
                except remote.RemoteError:
                    dst_ip = None
                if dst_ip:
                    try:
                        proc = remote.direct_transfer(
                            src_alias, src_argv, alias, [*dst_argv, "--from-tar-stdin"], dst_ip, src_ips,
                            timeout=STAGE_ARTIFACT_TIMEOUT_SECONDS, src_sudo=src_sudo, dst_sudo=sudo,
                        )
                        path_used = f"direct:{dst_ip}"
                    except remote.RemoteError as exc:
                        if transfer == "direct":
                            raise
                        print(f"[webapp] direct transfer setup failed ({exc.message}); relaying via control plane", flush=True)
                        proc = None
                elif transfer == "direct":
                    raise remote.RemoteError(
                        "unreachable", f"{src_alias} cannot reach node {node['name']} on port 22; use transfer=relay"
                    )
            if proc is None:
                proc = remote.pipe_remote(
                    src_alias, src_argv, alias, [*dst_argv, "--from-tar-stdin"],
                    timeout=STAGE_ARTIFACT_TIMEOUT_SECONDS, src_sudo=src_sudo, dst_sudo=sudo,
                )
                path_used = "relay"
        else:
            proc = remote.run_remote_raw(alias, [*dst_argv, "--from-zip", zip_path], timeout=STAGE_ARTIFACT_TIMEOUT_SECONDS, sudo=sudo)
        if proc.returncode != 0:
            raise remote.RemoteError(
                "stage_failed",
                f"Staging {artifact_dir} on node {node['name']} failed (exit {proc.returncode}); earlier nodes: "
                + (", ".join(f"{r['node']}={r['status']}" for r in results) or "none"),
                stderr=(proc.stderr.strip() or proc.stdout.strip())[-2000:],
            )
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise remote.RemoteError("stage_failed", f"opu-artifact-stage on {node['name']} produced unparsable output: {exc}", stderr=proc.stdout[-2000:]) from exc
        results.append({"node": node["name"], "ssh_alias": alias, "status": "staged", "transfer": path_used, **payload})

    # Media changed: every artifact-bound evidence document must be regenerated.
    for name in _ARTIFACT_BOUND_EVIDENCE:
        evidence.clear_evidence(host_id, name)
    return {
        "schema_version": "1.0",
        "status": "staged",
        "artifact_dir": artifact_dir,
        "owner": owner,
        "source": {"host_id": src_host_id} if src_host_id else {"zip_path": zip_path},
        "nodes": results,
        "next": "Re-run Artifact inspection, Procedure validation and OPatch compatibility for this host.",
    }


STEPS = {
    "discovery": step_discovery,
    "reconcile": step_reconcile,
    "artifact-inspect": step_artifact_inspect,
    "procedure-validate": step_procedure_validate,
    "compatibility-collect": step_compatibility_collect,
    "compatibility-reconcile": step_compatibility_reconcile,
    "readiness-evaluate": step_readiness_evaluate,
    # Remediation actions (not part of the evidence chain / STEP_ORDER).
    "stage-artifact": step_stage_artifact,
    "readiness-chain": step_readiness_chain,
    "recovery-collect": step_recovery_collect,
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


def _step_status(step: str, payload: dict | None) -> str | None:
    """Surface the operator-facing status for a cached evidence document.

    Most tools put ``status`` at the top level. ``opu-artifact-inspect`` nests
    it under ``artifact.status`` (``ready_for_catalog`` / blocked variants).
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if status is not None:
        return status
    if step == "artifact-inspect":
        artifact = payload.get("artifact")
        if isinstance(artifact, dict) and artifact.get("status") is not None:
            return artifact.get("status")
    return None


def pipeline_state(host_id: str) -> list[dict]:
    state = []
    for step in STEP_ORDER:
        payload = evidence.read_evidence(host_id, EVIDENCE_NAME_FOR_STEP[step])
        entry = {
            "step": step,
            "done": payload is not None,
            "status": _step_status(step, payload),
            "evidence": payload,
        }
        if step == "procedure-validate":
            entry["input"] = evidence.read_evidence(host_id, "procedure_input")
        if step == "readiness-evaluate":
            entry["input"] = evidence.read_evidence(host_id, "policy")
            entry["recovery_selection"] = evidence.read_evidence(host_id, "recovery_selection")
        if step == "discovery":
            phases = discovery_phases.derive_discovery_phases(payload)
            entry["phases"] = phases
            entry["phases_status"] = discovery_phases.discovery_phases_rollup(phases)
            # Topology snapshots have no top-level status; surface phase rollup.
            if entry["status"] is None and payload is not None:
                entry["status"] = entry["phases_status"]
            node_index = evidence.read_evidence(host_id, "snapshot_nodes")
            if isinstance(node_index, dict) and node_index.get("nodes"):
                entry["captured_nodes"] = [
                    n.get("name") for n in node_index["nodes"] if isinstance(n, dict) and n.get("name")
                ]
        state.append(entry)
    return state
