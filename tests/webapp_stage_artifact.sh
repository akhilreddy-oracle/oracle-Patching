#!/usr/bin/env bash
# Webapp remediation path: probe where complete media lives, stage it on every
# node of a host (direct host-to-host when reachable, relay otherwise), and
# invalidate artifact-bound evidence — all with a fake SSH layer so the test
# never touches a real host. Also covers tools_sync fingerprint/stamp logic.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-webapp-stage.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
cd "$ROOT/webapp"
OPU_TEST_TMP="$TMP" python3 - <<'PY'
import base64, json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, ".")
import evidence
evidence.VAR_DIR = Path(os.environ["OPU_TEST_TMP"]) / "hosts"
import pipeline_steps, remote, tools_sync

HOST_ID = "rac"
HOST = {"id": HOST_ID, "ssh_alias": "n1", "remote_root": "/opt/opu", "sudo": True,
        "nodes": [{"name": "n1", "ssh_alias": "n1"}, {"name": "n2", "ssh_alias": "n2"}]}
SRC = {"id": "src", "ssh_alias": "src", "remote_root": "/opt/opu", "sudo": True}
HOSTS = {HOST_ID: HOST, "src": SRC, "other": {"id": "other", "ssh_alias": "oth", "remote_root": "/opt/opu", "sudo": False}}
DIR = "/u01/stage/39034528"

# Fake estate: media state per alias; src complete, n1/n2 metadata-only, oth missing.
media = {"src": "COMPLETE", "n1": "INCOMPLETE", "n2": "INCOMPLETE", "oth": "MISSING"}
calls = []

def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)

def fake_run_remote_shell(alias, script, timeout=45, sudo=False):
    calls.append(("shell", alias, script))
    if "echo MISSING" in script:  # probe
        state = media[alias]
        return cp(0, f"{state}\n5000\noracle:oinstall\n" if state != "MISSING" else "MISSING\n")
    if script.startswith("hostname -I"):
        return cp(0, {"src": "10.0.0.1\n", "n1": "10.0.0.2\n", "n2": "10.0.0.3\n"}.get(alias, "\n"))
    if script.startswith("for ip in"):  # reachability from src
        return cp(0, "10.0.0.2\n") if "10.0.0.2" in script else cp(1, "")
    if script.strip() == "id -un":
        return cp(0, "opc\n")
    if "/etc/ssh/ssh_host_ed25519_key.pub" in script:
        blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
        return cp(0, "ssh-ed25519 " + base64.b64encode(blob).decode() + " fixture\n")
    if "mktemp -d /tmp/opu-transfer." in script:
        return cp(0, "/tmp/opu-transfer.ABCDEFGHIJKL\n")
    if "authorized_keys" in script:
        return cp(0, "")
    if "set -o pipefail" in script:  # direct transfer executed on src
        assert script.count("tar -C /u01/stage -cf - 39034528") == 1 and "opu-forced-command" in script
        assert "-o IdentitiesOnly=yes" in script and "opc@10.0.0.2" in script
        assert "-o StrictHostKeyChecking=yes" in script and "accept-new" not in script
        media["n1"] = "COMPLETE"
        return cp(0, json.dumps({"status": "staged", "artifact": DIR, "patch_id": "39034528", "files": 3, "bytes": 999}) + "\n")
    if script.startswith("set -eu; rm -f -- /tmp/opu-transfer."):
        return cp(0, "")
    if "cat" in script and tools_sync.STAMP_NAME in script:
        return cp(1, "", "missing")
    raise AssertionError(f"unexpected shell on {alias}: {script}")

def fake_pipe_remote(src_alias, src_argv, dst_alias, dst_argv, timeout=45, src_sudo=False, dst_sudo=False):
    calls.append(("pipe", src_alias, dst_alias))
    assert src_argv[:2] == ["tar", "-C"] and dst_argv[-1] == "--from-tar-stdin"
    media[dst_alias] = "COMPLETE"
    return cp(0, json.dumps({"status": "staged", "artifact": DIR, "patch_id": "39034528", "files": 3, "bytes": 999}) + "\n")

remote.run_remote_shell = fake_run_remote_shell
remote.pipe_remote = fake_pipe_remote
def fake_push(alias, path, data, timeout=45, private=False):
    assert private and path.startswith("/tmp/opu-transfer.ABCDEFGHIJKL/")
    if path.endswith("/known_hosts"):
        assert data.startswith(b"10.0.0.2 ssh-ed25519 ")
    calls.append(("push", alias, path))
remote.push_file = fake_push
remote.pull_file = lambda alias, path, timeout=45, sudo=False: (_ for _ in ()).throw(remote.RemoteError("x", "no stamp"))
tools_sync.ensure_host_tools = lambda host, force=False: calls.append(("tools", host["id"])) or []

# Snapshot evidence supplies the Oracle Home owner.
evidence.write_evidence(HOST_ID, "snapshot", {"oracle_homes": [{"path": "/u01/app/oracle/product/19/dbhome_1", "owner": "oracle"}],
                                                "databases": [{"oracle_home": "/u01/app/oracle/product/19/dbhome_1"}]})
for name in ("artifact", "procedure", "compatibility", "readiness"):
    evidence.write_evidence(HOST_ID, name, {"status": "blocked"})

# 1. Probe: this host's nodes + which other hosts have complete media.
probe = pipeline_steps.artifact_sources(HOST_ID, HOST, HOSTS, DIR)
assert probe["owner"] == "oracle"
assert [t["state"] for t in probe["targets"]] == ["incomplete", "incomplete"], probe["targets"]
assert {s["host_id"]: s["state"] for s in probe["sources"]} == {"src": "complete", "other": "missing"}, probe["sources"]

# 2. Stage from src: n1 reachable directly, n2 not -> relay. Evidence cleared.
result = pipeline_steps.step_stage_artifact(HOST_ID, HOST, {"artifact_dir": DIR, "source": {"host_id": "src"}, "_hosts": HOSTS})
assert result["status"] == "staged" and result["owner"] == "oracle", result
by_node = {n["node"]: n for n in result["nodes"]}
assert by_node["n1"]["status"] == "staged" and by_node["n1"]["transfer"] == "direct:10.0.0.2", by_node["n1"]
assert by_node["n2"]["status"] == "staged" and by_node["n2"]["transfer"] == "relay", by_node["n2"]
assert ("tools", HOST_ID) in calls
assert evidence.read_evidence(HOST_ID, "artifact") is None and evidence.read_evidence(HOST_ID, "readiness") is None
assert evidence.read_evidence(HOST_ID, "snapshot") is not None
# The restricted transfer key was installed and then removed on n1; src key removed.
installs = [c for c in calls if c[0] == "shell" and c[1] == "n1" and "authorized_keys" in c[2]]
assert len(installs) == 2 and ">>" in installs[0][2] and "awk -v marker=" in installs[1][2], installs
# The forced command pins the exact staging invocation and the source IPs; no pty/forwarding.
auth = installs[0][2]
for needle in ('from="10.0.0.1"', "opu-artifact-stage", "--from-tar-stdin", "--owner oracle", "no-pty", "no-port-forwarding", "no-agent-forwarding"):
    assert needle in auth, (needle, auth)
assert any(c[0] == "shell" and c[1] == "src" and c[2].startswith("set -eu; rm -f -- /tmp/opu-transfer.") for c in calls)

# 3. Idempotent: complete nodes are skipped unless replace=true.
result2 = pipeline_steps.step_stage_artifact(HOST_ID, HOST, {"artifact_dir": DIR, "source": {"host_id": "src"}, "_hosts": HOSTS})
assert all(n["status"] == "already_complete" for n in result2["nodes"]), result2

# 4. Validation is fail-closed.
def expect_error(body, code):
    try:
        pipeline_steps.step_stage_artifact(HOST_ID, HOST, {**body, "_hosts": HOSTS})
    except remote.RemoteError as exc:
        assert exc.error == code, (exc.error, exc.message)
    else:
        raise AssertionError(f"expected {code} for {body}")
expect_error({"artifact_dir": "relative", "source": {"host_id": "src"}}, "invalid_input")
expect_error({"artifact_dir": DIR}, "invalid_input")
expect_error({"artifact_dir": DIR, "source": {"host_id": "src", "zip_path": "/x.zip"}}, "invalid_input")
expect_error({"artifact_dir": DIR, "source": {"host_id": "nope"}}, "invalid_input")
expect_error({"artifact_dir": DIR, "source": {"host_id": "other"}}, "source_incomplete")
expect_error({"artifact_dir": DIR, "owner": "bad owner;rm", "source": {"host_id": "src"}}, "invalid_input")
expect_error({"artifact_dir": DIR, "source": {"host_id": "src"}, "transfer": "teleport"}, "invalid_input")
# transfer=direct must not silently fall back when the node is unreachable.
media["n2"] = "INCOMPLETE"
expect_error({"artifact_dir": DIR, "source": {"host_id": "src"}, "transfer": "direct"}, "unreachable")

# Cleanup uncertainty after direct transfer must never start a relay fallback.
media["n1"] = "INCOMPLETE"
before_relay = len([c for c in calls if c[0] == "pipe"])
def cleanup_failure(alias, script, **kwargs):
    if alias == "n1" and "awk -v marker=" in script:
        calls.append(("shell", alias, script))
        return cp(1, "", "fixture cleanup failed")
    return fake_run_remote_shell(alias, script, **kwargs)
remote.run_remote_shell = cleanup_failure
expect_error({"artifact_dir": DIR, "source": {"host_id": "src"}}, "transfer_cleanup_failed")
assert len([c for c in calls if c[0] == "pipe"]) == before_relay
remote.run_remote_shell = fake_run_remote_shell

# 5. tools_sync fingerprint is stable and changes with content.
fp1 = tools_sync.local_fingerprint(); fp2 = tools_sync.local_fingerprint()
assert fp1 == fp2 and len(fp1) == 64

# 6. readiness-chain: changed media requires renewed requirements review;
#    matching reviewed inputs preserve their bindings and stop at a blocker.
CH = "chainhost"
evidence.write_evidence(CH, "artifact", {"artifact": {"path": DIR, "sha256": "a" * 64, "readme_files": [{"path": "README.html", "sha256": "b" * 64}]}})
evidence.write_evidence(CH, "procedure_input", {"patch_id": "39034528", "artifact_sha256": "old", "oracle_references": [{"kind": "patch_readme", "identifier": "README.html", "sha256": "old"}]})
evidence.write_evidence(CH, "policy", {"schema_version": "1.0", "maximum_snapshot_age_seconds": 1800})
order, seen = [], {}
def mk(name, result, capture=None):
    def fn(host_id, host, body):
        order.append(name)
        if capture: seen[name] = body
        return result
    return fn
pipeline_steps.step_discovery = mk("discovery", {"host": {"name": "n1"}})
pipeline_steps.step_reconcile = mk("reconcile", {"status": "consistent"})
pipeline_steps.step_artifact_inspect = mk("artifact-inspect", {"artifact": {"status": "ready_for_catalog"}}, capture=True)
pipeline_steps.step_procedure_validate = mk("procedure-validate", {"status": "ready_for_planning"}, capture=True)
pipeline_steps.step_compatibility_collect = mk("compatibility-collect", {"status": "blocked", "findings": ["n1: Incomplete patch media"]})
pipeline_steps.step_compatibility_reconcile = mk("compatibility-reconcile", {"status": "passed"})
pipeline_steps.step_readiness_evaluate = mk("readiness-evaluate", {"status": "ready_for_approval"}, capture=True)
class Rec:
    def __init__(self): self.lines = []
    def log(self, line): self.lines.append(line)
rec = Rec()
try:
    pipeline_steps.step_readiness_chain(CH, HOST, {"_record": rec})
except pipeline_steps.localtools.LocalToolError as exc:
    assert "reviewed procedure" in str(exc), exc
else:
    raise AssertionError("chain rebound old requirements to changed patch media")
assert order == ["discovery", "reconcile", "artifact-inspect"], order
assert "procedure-validate" not in seen
assert evidence.read_evidence(CH, "procedure_input")["artifact_sha256"] == "old"
# Simulate the operator reviewing this exact media and saving its requirements.
evidence.write_evidence(CH, "procedure_input", {"patch_id": "39034528", "artifact_sha256": "a" * 64,
    "oracle_references": [{"kind": "patch_readme", "identifier": "README.html", "sha256": "b" * 64}]})
order.clear()
out = pipeline_steps.step_readiness_chain(CH, HOST, {"_record": rec})
assert order == ["discovery", "reconcile", "artifact-inspect", "procedure-validate", "compatibility-collect"], order
assert out["status"] == "blocked" and out["stopped_at"] == "compatibility-collect" and out["findings"] == ["n1: Incomplete patch media"], out
assert seen["artifact-inspect"]["artifact_dir"] == DIR
proc = seen["procedure-validate"]["procedure"]
assert proc["artifact_sha256"] == "a" * 64 and proc["oracle_references"][0]["sha256"] == "b" * 64 and proc["patch_id"] == "39034528", proc
assert any("compatibility-collect: blocked" in l for l in rec.lines), rec.lines
# Full pass uses the body policy over the saved one and reaches readiness.
pipeline_steps.step_compatibility_collect = mk("compatibility-collect", {"status": "passed"})
order.clear()
out = pipeline_steps.step_readiness_chain(CH, HOST, {"policy": {"schema_version": "1.0", "maximum_snapshot_age_seconds": 60}})
assert out["status"] == "ready_for_approval" and out["stopped_at"] is None and len(out["steps"]) == 7, out
assert seen["readiness-evaluate"]["policy"]["maximum_snapshot_age_seconds"] == 60
# Missing saved inputs fail closed with an instructive message.
evidence.clear_evidence(CH, "procedure_input")
try:
    pipeline_steps.step_readiness_chain(CH, HOST, {})
except Exception as exc:
    assert "Procedure validation" in str(exc), exc
else:
    raise AssertionError("chain ran without a procedure input")
print("webapp_stage_artifact_ok")
PY
