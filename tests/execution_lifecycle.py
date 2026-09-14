#!/usr/bin/env python3
"""Real controller/worker boundaries with disposable sealed state, never Oracle."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin/opu-patch-plan"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def seal(value, field="record_sha256"):
    value = dict(value)
    value.pop(field, None)
    value[field] = digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fixture:
    def __init__(self, base):
        self.base = base
        self.state = base / "controller"
        self.env = dict(os.environ, OPU_PLAN_STATE_DIR=str(self.state))

    def call(self, *args, ok=True):
        result = subprocess.run([str(TOOL), *args], env=self.env, capture_output=True, text=True)
        if ok:
            assert result.returncode == 0, (args, result.returncode, result.stderr)
        else:
            assert result.returncode != 0, (args, result.stdout)
        return result

    def path(self, name):
        return self.state / "plans" / name

    def seed(self, name, *, nodes=None, dispatched=True, count=2):
        nodes = nodes or [name]
        now = int(time.time())
        plan = seal({"schema_version": "1.0", "intent": "patch_apply" if dispatched else "patch_rollback",
                     "plan_id": name, "requester": "requester", "maintenance_window": {"start": iso(now - 60), "end": iso(now + 3600)},
                     "target": {"family": "database", "method": "opatch", "platform_id": "226", "database_unique_name": "ORCL",
                                "oracle_home": str(self.base / "oracle"), "owner": "oracle"},
                     "nodes": nodes, "patch_id": "12345678", "artifact": {"path": str(self.base / "media"), "sha256": "a" * 64, "platforms": [{"id": "226"}]},
                     "procedure": {"adapter": "database_single_instance_opatch" if dispatched else "database_single_instance_opatch_rollback",
                                   "operations": ["database_shutdown", "database_opatch_apply", "database_startup", "database_datapatch"],
                                   "platform_id": "226", "required_opatch_version": "12.2.0.1.51"},
                     "source_documents": {}, "recovery": {"waived": True, "require_backup": False},
                     "source_apply": {"plan_sha256": "b" * 64, "final_evidence_sha256": "c" * 64, "final_evidence_record_sha256": "d" * 64}}, "plan_sha256")
        directory = self.path(name)
        write(directory / "plan.json", plan)
        approval = seal({"schema_version": "1.0", "decision": "approved", "plan_id": name, "plan_sha256": plan["plan_sha256"], "actor": "approver"})
        write(directory / "approval.json", approval)
        authorization = seal({"schema_version": "1.0", "decision": "execution_authorized", "plan_id": name,
                              "plan_sha256": plan["plan_sha256"], "actor": "operator", "approval": {
                                  "sha256": digest((directory / "approval.json").read_bytes()), "record_sha256": approval["record_sha256"]}})
        write(directory / "authorization.json", authorization)
        (directory / "state").write_text("running\n" if dispatched else "execution_authorized\n")
        if not dispatched:
            return
        manifest = []
        for index in range(count):
            task = {"schema_version": "1.0", "task_id": f"{index + 1:03d}-precheck-{nodes[0]}", "plan_id": name,
                    "plan_sha256": plan["plan_sha256"], "stage": "precheck", "node": nodes[0], "adapter": "database_single_instance_opatch",
                    "authorization_sha256": digest((directory / "authorization.json").read_bytes()),
                    "authorization_record_sha256": authorization["record_sha256"]}
            task = seal(task, "task_definition_sha256")
            task["status"] = "pending"
            write(directory / "tasks" / (task["task_id"] + ".json"), task)
            manifest.append({key: task[key] for key in ("task_id", "task_definition_sha256")})
        write(directory / "task-manifest.json", seal({"schema_version": "1.0", "plan_id": name, "plan_sha256": plan["plan_sha256"], "tasks": manifest}))
        reservations = {}
        registry = self.state / "target-reservations.json"
        if registry.exists():
            reservations = json.loads(registry.read_text())["reservations"]
        reservations.update({node: {"plan_id": name, "plan_sha256": plan["plan_sha256"]} for node in nodes})
        write(registry, seal({"schema_version": "1.0", "reservations": reservations}))

    def task(self, name, index=0):
        return sorted((self.path(name) / "tasks").glob("*.json"))[index]

    def claim(self, name, index=0):
        task_id = self.task(name, index).stem
        self.call("claim", "--plan-id", name, "--task-id", task_id, "--actor", "worker", "--lease-seconds", "3600")
        return task_id

    def evidence(self, name, task_id, *, failed=False, unknown=False, generation=None):
        task = json.loads((self.path(name) / "tasks" / (task_id + ".json")).read_text())
        plan = json.loads((self.path(name) / "plan.json").read_text())
        generation = task.get("retry_count", 0) if generation is None else generation
        output = self.base / "worker-evidence" / name / f"{task_id}-{generation}"
        output.mkdir(parents=True, exist_ok=True)
        logs = {}
        for stream in ("stdout", "stderr"):
            path = output / (stream + ".log")
            path.write_text("synthetic fixture; no Oracle command\n")
            logs[stream] = {"path": str(path), "sha256": digest(path.read_bytes())}
        rollback = task["adapter"].endswith("_rollback")
        value = {"schema_version": "1.0", "collector": {"name": "oracle.database.single_instance.rollback.executor" if rollback else "oracle.database.single_instance.executor", "version": "1"},
                 "intent": "patch_rollback" if rollback else "patch_apply", "plan_id": name, "task_id": task_id,
                 "plan_sha256": plan["plan_sha256"], "actor": "worker", "stage": task["stage"], "retry_count": generation,
                 "status": "failed" if failed else "succeeded", "postcondition": {"status": "unknown" if unknown else ("failed" if failed else "passed")},
                 "outcome_class": "binary_state_unknown" if unknown else ("no_mutation" if failed or "precheck" in task["stage"] else "binary_state_known"), "started_at": iso(time.time()), "finished_at": iso(time.time()),
                 "exit_code": 73 if failed else 0, "target": plan["target"], "patch": {"patch_id": plan["patch_id"], "artifact_sha256": plan["artifact"]["sha256"]},
                 "recovery": {"manifest_sha256": None, "record_sha256": None}, "logs": logs}
        if rollback:
            value["source_apply"] = plan["source_apply"]
        write(output / "evidence.json", seal(value))
        return output / "evidence.json"

    def complete(self, name, task_id, evidence, *, failed=False, ok=True):
        return self.call("complete", "--plan-id", name, "--task-id", task_id, "--actor", "worker", "--status", "failed" if failed else "succeeded", "--evidence", str(evidence), ok=ok)


with tempfile.TemporaryDirectory(prefix="opu-execution-lifecycle-") as temporary:
    base = Path(temporary).resolve()
    f = Fixture(base)
    for damage in ("malformed", "missing", "definition"):
        f.seed(damage)
        task_id = f.claim(damage)
        other = f.task(damage, 1)
        if damage == "malformed":
            other.write_text("{invalid\n")
        elif damage == "missing":
            other.unlink()
        else:
            value = json.loads(other.read_text()); value["stage"] = "apply"; write(other, value)
        f.complete(damage, task_id, f.evidence(damage, task_id), ok=False)
        assert (f.path(damage) / "state").read_text().strip() == "paused"

    for name, unknown in (("unsafe-retry", True), ("safe-retry", False), ("custody-tamper", False)):
        f.seed(name)
        task_id = f.claim(name)
        evidence = f.evidence(name, task_id, failed=True, unknown=unknown)
        f.complete(name, task_id, evidence, failed=True)
        if name == "custody-tamper":
            (f.path(name) / "evidence" / task_id / "stdout.log").write_text("tampered")
            f.call("task-status", "--plan-id", name, "--task-id", task_id, ok=False)
        f.call("retry-task", "--plan-id", name, "--task-id", task_id, "--actor", "operator", ok=name == "safe-retry")
        if name == "safe-retry":
            assert (f.path(name) / "attempts" / task_id / "attempt-0.json").exists()
            f.claim(name)
            f.complete(name, task_id, evidence, failed=True, ok=False)
            f.complete(name, task_id, f.evidence(name, task_id))
            verified = json.loads(f.call("task-status", "--plan-id", name, "--task-id", task_id).stdout)
            assert verified["retry_count"] == 1 and verified["status"] == "succeeded"
            assert (f.path(name) / "evidence" / task_id / "evidence.json").exists()
            assert (f.path(name) / "evidence" / (task_id + "-retry1") / "evidence.json").exists()

    f.seed("success", count=1)
    task_id = f.claim("success")
    f.complete("success", task_id, f.evidence("success", task_id))
    assert (f.path("success") / "state").read_text().strip() == "succeeded"
    assert "success" not in json.loads((f.state / "target-reservations.json").read_text())["reservations"]

    f.seed("late-custody-damage")
    first = f.claim("late-custody-damage")
    f.complete("late-custody-damage", first, f.evidence("late-custody-damage", first))
    (f.path("late-custody-damage") / "evidence" / first / "stdout.log").write_text("damaged after first task completed")
    second = f.claim("late-custody-damage", 1)
    f.complete("late-custody-damage", second, f.evidence("late-custody-damage", second), ok=False)
    assert (f.path("late-custody-damage") / "state").read_text().strip() == "paused"
    assert "late-custody-damage" in json.loads((f.state / "target-reservations.json").read_text())["reservations"]

    for historical_state in ("running", "paused"):
        legacy = Fixture(base / ("legacy-" + historical_state))
        legacy.seed("old-plan", nodes=["legacy-node"])
        (legacy.path("old-plan") / "state").write_text(historical_state + "\n")
        (legacy.path("old-plan") / "task-manifest.json").unlink()
        (legacy.state / "target-reservations.json").unlink()
        legacy.seed("new-plan", nodes=["legacy-node"], dispatched=False)
        legacy.call("dispatch", "--plan-id", "new-plan", "--actor", "operator", ok=False)
        assert not (legacy.state / "target-reservations.json").exists()

    # Actual dispatch reserves overlapping targets atomically; an unsuccessful
    # multi-host reservation cannot strand the nonoverlapping host.
    for name, nodes in (("reservation-a", ["node1", "node2"]), ("reservation-b", ["node2", "node3"])):
        f.seed(name, nodes=nodes, dispatched=False)
    jobs = [subprocess.Popen([str(TOOL), "dispatch", "--plan-id", name, "--actor", "operator"], env=f.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("reservation-a", "reservation-b")]
    results = [job.communicate() for job in jobs]
    assert sorted(job.returncode for job in jobs) == [0, 75], [(job.returncode, result) for job, result in zip(jobs, results)]
    winner = "reservation-a" if jobs[0].returncode == 0 else "reservation-b"
    loser = "reservation-b" if winner == "reservation-a" else "reservation-a"
    reservations = json.loads((f.state / "target-reservations.json").read_text())["reservations"]
    assert not any(value["plan_id"] == loser for value in reservations.values())
    task_id = f.claim(winner)
    f.complete(winner, task_id, f.evidence(winner, task_id, failed=True, unknown=True), failed=True)
    f.call("dispatch", "--plan-id", loser, "--actor", "operator", ok=False)

    # A worker receives only one plan snapshot, never the global registry. It
    # can execute existing tasks, cannot issue retries/authority, and returning
    # absolute-path evidence remains verifiable after the worker is offline.
    f.seed("remote-plan", nodes=["remote-node"], dispatched=False)
    f.call("dispatch", "--plan-id", "remote-plan", "--actor", "operator")
    worker = Fixture(base / "worker")
    shutil.copytree(f.path("remote-plan"), worker.path("remote-plan"))
    worker.env["OPU_PLAN_WORKER_SNAPSHOT"] = "1"
    worker.call("dispatch", "--plan-id", "remote-plan", "--actor", "operator", ok=False)
    worker.call("retry-task", "--plan-id", "remote-plan", "--task-id", "ignored", "--actor", "operator", ok=False)
    receipt = worker.path("remote-plan") / "target-reservation.json"
    original_receipt = receipt.read_bytes()
    receipt.unlink()
    worker.call("claim", "--plan-id", "remote-plan", "--task-id", worker.task("remote-plan").stem, "--actor", "worker", ok=False)
    receipt.write_bytes(original_receipt)
    for index in range(len(list((worker.path("remote-plan") / "tasks").glob("*.json")))):
        task_id = worker.claim("remote-plan", index)
        if index == 0:
            receipt.write_text("{invalid")
            worker.call("renew", "--plan-id", "remote-plan", "--task-id", task_id, "--actor", "worker", ok=False)
            worker.complete("remote-plan", task_id, worker.evidence("remote-plan", task_id), ok=False)
            receipt.write_bytes(original_receipt)
        worker.complete("remote-plan", task_id, worker.evidence("remote-plan", task_id))
    assert not (worker.state / "target-reservations.json").exists()
    shutil.copytree(worker.path("remote-plan"), f.path("remote-plan"), dirs_exist_ok=True)
    worker.base.rename(base / "offline-worker")
    f.call("task-status", "--plan-id", "remote-plan", "--task-id", task_id)
    assert "remote-node" in json.loads((f.state / "target-reservations.json").read_text())["reservations"]
    f.call("reconcile", "--plan-id", "remote-plan", "--actor", "operator")
    assert "remote-node" not in json.loads((f.state / "target-reservations.json").read_text())["reservations"]

    # Every public managed adapter competes for the same host lock before it
    # can claim work, even with a different per-adapter state directory.
    env = dict(f.env, OPU_EXECUTION_LOCK_DIR=str(base / "host-locks"), TEST_MODE="1", PLAN_STATE_DIR=str(f.state))
    for flag in ("OPU_SINGLE_INSTANCE_TEST_MODE", "OPU_RAC_DATABASE_TEST_MODE", "OPU_GRID_NODE_TEST_MODE", "OPU_GRID_OPATCHAUTO_TEST_MODE", "OPU_OJVM_TEST_MODE", "OPU_OOP_TEST_MODE"):
        env[flag] = "1"
    command = f'. {shlex.quote(str(ROOT / "lib/opu/common.sh"))}; . {shlex.quote(str(ROOT / "lib/opu/execution.sh"))}; opu_execution_host_lock || exit; echo ready; read -r done'
    holder = subprocess.Popen(["bash", "-c", command], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        for executable in ("opu-database-single-instance-patch", "opu-database-single-instance-rollback", "opu-database-rac-node-patch", "opu-database-rac-node-rollback",
                           "opu-grid-node-patch", "opu-grid-node-rollback", "opu-grid-opatchauto-patch", "opu-grid-opatchauto-rollback",
                           "opu-database-ojvm-patch", "opu-database-ojvm-rollback", "opu-database-out-of-place-patch", "opu-database-out-of-place-switchback"):
            result = subprocess.run([str(ROOT / "bin" / executable), "execute", "--plan-id", "other", "--task-id", "other", "--actor", "worker"], env=env, capture_output=True, text=True)
            assert result.returncode == 75 and "another Oracle executor owns this host" in result.stderr, (executable, result.returncode, result.stderr)
    finally:
        holder.communicate("done\n", timeout=10)

print("execution lifecycle: passed (task integrity, custody, retry generations, target reservations, all adapter host locks)")
