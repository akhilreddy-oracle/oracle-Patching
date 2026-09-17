"""One shared Oracle fixture behind the actual managed-host controller routes.

This is controller acceptance with SIMULATED host transport, never live proof.
Only package deployment, SSH/file transport and Oracle binaries are replaced.
Recovery, custody/import, selection, readiness and plan executors stay real.
No completed recovery or ready-for-approval document is seeded at the seam.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

HOST_ID = "connected-fixture"
ALIAS = "simulated-offline-transport-only"
REQUEST_ID = "connected-backup"


def install(base: Path, checkout: Path) -> None:
    import evidence
    import pipeline_steps
    import planctl
    import recovery_fixtures
    import recoveryctl
    import remote
    import testmode_fixtures
    import tools_sync

    fixture = planctl.TESTMODE_DIR / "connected-home"
    patch = testmode_fixtures.build(fixture)
    (fixture / "patch/39034528/etc/config/actions.xml").write_text("<actions/>\n")
    testmode_fixtures._run_tool("opu-artifact-inspect", ["--artifact", str(fixture / "patch/39034528"),
                                "--output", str(patch["evidence"]["artifact"])])
    components = base / "connected-recovery-components"
    recovery = recovery_fixtures.build(components, REQUEST_ID)
    home = fixture / "oracle/dbhome_1"
    runtime = components / "runtime"
    backup_parent = fixture / "backups"
    backup_parent.mkdir()
    remote_root = base / "simulated-node"
    runtime_fingerprint = "a" * 64
    runtime_root = remote_root / ".opu-runtimes" / runtime_fingerprint
    (runtime_root / "bin").mkdir(parents=True)
    remote_state = base / "simulated-native-recovery-state"

    # Both workflows mutate the SAME database/listener state and Oracle home.
    # Adapt only the fixture's vocabulary (OPEN/up), never controller evidence.
    recovery_sql = runtime / "recovery-sqlplus"
    recovery_sql.write_text((components / "oracle/dbhome_1/bin/sqlplus").read_text()
        .replace('state_file="$OPU_TEST_RUNTIME/database.state"', 'state_file="$OPU_TEST_DATABASE_STATE"')
        .replace('"$state" = OPEN', '"$state" = up')
        .replace("printf 'OPEN\\n'", "printf 'up\\n'")
        .replace("printf 'DOWN\\n'", "printf 'down\\n'"))
    recovery_sql.chmod(0o750)
    patch_sql = runtime / "patch-sqlplus"
    patch_sql.write_text((home / "bin/sqlplus").read_text().replace('LOG_MODE=ARCHIVELOG', 'LOG_MODE=NOARCHIVELOG'))
    patch_sql.chmod(0o750)
    (home / "bin/sqlplus").write_text("#!/bin/bash\nset -eu\n"
        f'if [ "${{OPU_RECOVERY_PREP_TEST_MODE:-0}}" = 1 ]; then exec {shlex.quote(str(recovery_sql))} "$@"; '
        f'else exec {shlex.quote(str(patch_sql))} "$@"; fi\n')
    for name in ("rman", "oracle"):
        shutil.copy(components / "oracle/dbhome_1/bin" / name, home / "bin" / name)
    shutil.copy(components / "oracle/dbhome_1/oraInst.loc", home / "oraInst.loc")
    # Listener READY is conditional on the shared service state, not a constant.
    listener = (components / "oracle/dbhome_1/bin/lsnrctl").read_text()
    listener = listener.replace('case "${1:-}" in', 'case "${1:-}" in\n'
        '  start) printf "up\\n" >"$OPU_TEST_LISTENER_STATE";;\n'
        '  stop) printf "down\\n" >"$OPU_TEST_LISTENER_STATE";;')
    listener = listener.replace('  services|status)\n', '  services|status)\n'
        '    [ "$(cat "$OPU_TEST_LISTENER_STATE")" = up ] && [ "$(cat "$OPU_TEST_DATABASE_STATE")" = up ] || exit 1\n')
    (home / "bin/lsnrctl").write_text(listener)
    env = {**patch["env"], "OPU_TEST_RUNTIME": str(runtime)}
    (fixture / "env.json").write_text(json.dumps(env))
    transport_env = {**os.environ, **env, **recovery["env"],
        "OPU_RECOVERY_PREP_STATE_DIR": str(remote_state),
        "OPU_TEST_SUCCESS_ROOT": str(backup_parent / REQUEST_ID)}
    transport_env["OPU_TEST_DATABASE_STATE"] = str(patch["state"]["database"])

    # Source-only native executables run under the fixture environment even
    # when the managed controller correctly invokes them with env -i.
    native_names = {"opu-database-recovery-prepare", "opu-recovery-evidence-collect", "opu-opatch-compatibility-collect"}
    for name in native_names:
        wrapper = runtime_root / "bin" / name
        exports = {key: value for key, value in transport_env.items()
                   if key.startswith("OPU_") or key in {"PATH", "PYTHONDONTWRITEBYTECODE", "TMPDIR"}}
        wrapper.write_text("#!/bin/bash\nset -eu\n" + "\n".join(
            f"export {key}={shlex.quote(value)}" for key, value in exports.items()) +
            f"\nexec {shlex.quote(str(checkout / 'bin' / name))} \"$@\"\n")
        wrapper.chmod(0o750)
    # macOS has no setsid binary. This shim performs the actual OS operation;
    # the production detached launch, PID/rc polling and import remain intact.
    transport_bin = base / "transport-bin"
    transport_bin.mkdir()
    setsid = transport_bin / "setsid"
    setsid.write_text("#!/usr/bin/env python3\nimport os,sys\nos.setsid()\nos.execvp(sys.argv[1],sys.argv[1:])\n")
    setsid.chmod(0o750)
    transport_env["PATH"] = str(transport_bin) + os.pathsep + transport_env["PATH"]

    def scoped(path):
        value = Path(path)
        if not value.is_absolute() or not value.resolve().is_relative_to(base) or value.is_symlink():
            raise RuntimeError(f"Fixture transport denied a path outside disposable state: {path}")
        return value

    def alias(value):
        if value != ALIAS:
            raise RuntimeError("Only the single simulated fixture alias is permitted")

    def raw(ssh_alias, argv, timeout=45, sudo=False):
        alias(ssh_alias)
        command = list(argv)
        if command[:2] == ["env", "-i"]:
            expected = ["env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C",
                        f"OPU_RECOVERY_PREP_STATE_DIR={remote_state}"]
            if command[:6] != expected:
                raise RuntimeError("Unexpected clean environment at fixture boundary")
            executable = command[6]
        else:
            executable = command[0]
        if executable not in {str(runtime_root / "bin" / name) for name in native_names}:
            raise RuntimeError("Unexpected native command at fixture transport boundary")
        for item in command[command.index(executable) + 1:]:
            if item.startswith("/"):
                scoped(item)
        return subprocess.run(command, capture_output=True, text=True, timeout=min(timeout, 120), env=transport_env)

    def shell(ssh_alias, script, timeout=45, sudo=False):
        alias(ssh_alias)
        if not script.startswith(("set -eu; umask 077;", 'set -eu; [ "$(sha256sum ', "cd ")):
            raise RuntimeError("Unexpected generated script at fixture transport boundary")
        # Dynamic paths are already shell-quoted by the actual controller.
        # Reject any absolute path outside this fixture before executing it.
        for value in re.findall(r"/[A-Za-z0-9_./-]+", script):
            if value in {"/dev/null", "/usr/sbin", "/usr/bin", "/sbin", "/bin"}:
                continue
            scoped(value)
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                              timeout=min(timeout, 120), env=transport_env)

    def push(ssh_alias, path, content, **kwargs):
        alias(ssh_alias)
        target = scoped(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() == content:
            return  # Same absolute snapshot path on the simulated local node.
        target.write_bytes(content)

    def pull(ssh_alias, path, max_bytes=None, **kwargs):
        alias(ssh_alias)
        data = scoped(path).read_bytes()
        if max_bytes is not None and len(data) > max_bytes:
            raise RuntimeError("Fixture transfer exceeded native evidence size limit")
        return data

    def ensure_tools(ssh_alias, root, sudo=False, **kwargs):
        alias(ssh_alias)
        if root != str(remote_root) or not all((runtime_root / "bin" / n).is_file() for n in native_names):
            raise RuntimeError("Fixture package deployment target is not the isolated node")
        return {"ssh_alias": ssh_alias, "runtime_root": str(runtime_root), "fingerprint": runtime_fingerprint, "synced": False, "cached": False}

    remote.run_remote_raw = raw
    remote.run_remote_shell = shell
    remote.push_file = push
    remote.pull_file = pull
    tools_sync.ensure_tools = ensure_tools
    tools_sync.ensure_host_tools = lambda host, **kwargs: [ensure_tools(host["ssh_alias"], host["remote_root"])]
    recoveryctl.REMOTE_STATE_DIR = str(remote_state)
    pipeline_steps.REMOTE_SCRATCH_DIR = str(base / "scratch" / "{host_id}")
    host = {"id": HOST_ID, "label": "SIMULATED connected Oracle fixture — no live SSH", "ssh_alias": ALIAS,
            "remote_root": str(remote_root), "sudo": True, "nodes": [{"name": "testnode", "ssh_alias": ALIAS}]}
    (checkout / "webapp/hosts.json").write_text(json.dumps({"hosts": [host]}))

    # Seed discovered Oracle observations and patch media, then derive every
    # pure controller result using the actual native tools. The demo builder's
    # synthetic backup/readiness documents are not admitted to host evidence.
    snapshot = json.loads(recovery["snapshot"].read_text())
    snapshot["host"]["name"] = "testnode.example"
    snapshot["oracle_homes"][0].update(path=str(home), patch_inventory_source="opatch_lsinventory_xml",
                                     opatch_inventory_xml_sha256="a" * 64)
    snapshot["databases"][0]["oracle_home"] = str(home)
    snapshot["databases"][0]["runtime"].update(invalid_objects=0, sqlpatch_non_success=0,
        pdb_not_read_write=0, backup_age_minutes=0, fra_space_limit_bytes=0, fra_space_used_bytes=0,
        guaranteed_restore_points=0)
    recovery["snapshot"].write_text(json.dumps(snapshot, indent=2))
    evidence.write_evidence(HOST_ID, "snapshot", snapshot)
    artifact = json.loads(patch["evidence"]["artifact"].read_text())
    evidence.write_evidence(HOST_ID, "artifact", artifact)
    policy = json.loads(patch["evidence"]["policy"].read_text())
    policy["recovery"].update(storage_mode="filesystem", capacity_basis="allocated", minimum_filesystem_free_bytes=0)
    evidence.write_evidence(HOST_ID, "policy", policy)
    procedure = json.loads(patch["evidence"]["procedure"].read_text())["procedure"]
    procedure["artifact_sha256"] = artifact["artifact"]["sha256"]
    procedure["oracle_references"][0]["identifier"] = artifact["artifact"]["readme_files"][0]["path"]
    procedure["mandatory_prechecks"] = ["artifact_integrity", "platform_applicability", "opatch_version", "conflict_check", "backup_or_restore"]
    procedure["mandatory_postchecks"] = ["binary_inventory", "service_health"]
    for name in ("recovery", "readiness"):
        patch["evidence"][name].unlink()
    for result in (pipeline_steps.step_reconcile(HOST_ID, host, {}),
                   pipeline_steps.step_procedure_validate(HOST_ID, host, {"procedure": procedure})):
        if result.get("status") not in {"consistent", "ready_for_planning"}:
            raise RuntimeError(f"Connected fixture input validation failed: {result}")
    # Native OPatch prerequisites use the same shim that will later apply the
    # patch. No compatibility success payload is fabricated here.
    result = pipeline_steps.step_compatibility_collect(HOST_ID, host, {"artifact_dir": str(fixture / "patch/39034528")})
    if result.get("status") != "passed":
        raise RuntimeError(f"Connected fixture compatibility failed: {result}")
    result = pipeline_steps.step_compatibility_reconcile(HOST_ID, host, {})
    if result.get("status") != "passed":
        raise RuntimeError(f"Connected fixture compatibility reconciliation failed: {result}")
