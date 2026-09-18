#!/usr/bin/env python3
"""Native lease/evidence boundaries in disposable runtimes; no Oracle or SSH.

Delays are inserted only into private copies of the executables. The normal
30-second minimum lease, real controller locks, evidence and custody code all
remain active; production code has no timing bypass or testing delay option.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp"))
import testmode_fixtures

ROOT_LINE = 'ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)'


class Fixture:
    def __init__(self, base, name, *, evidence_hook="", verification_hook="", renew_hook="", renew_failure=False, clock=False):
        self.base, self.name = base / name, name
        self.base.mkdir()
        self.fixture = testmode_fixtures.build(self.base / "fixture")
        self.state = self.base / "plans"
        self.env = dict(os.environ, **self.fixture["env"], OPU_PLAN_STATE_DIR=str(self.state),
                        OPU_SINGLE_INSTANCE_WAIT_ATTEMPTS="1")
        self.controller, self.executor = self.base / "controller", self.base / "executor"
        controller = (ROOT / "bin/opu-patch-plan").read_text().replace(ROOT_LINE, "ROOT=" + shlex.quote(str(ROOT)), 1)
        if clock:
            # A private controller clock covers exact window-edge seconds
            # without waiting an hour or altering any sealed plan/approval.
            replacement = ('\ndate() {\n'
                           '  if [ "$#" -eq 2 ] && [ "$1" = -u ] && [ "$2" = +%s ] && [ -n "${OPU_TEST_CONTROLLER_EPOCH:-}" ]; then\n'
                           '    printf "%s\\n" "$OPU_TEST_CONTROLLER_EPOCH"\n'
                           '  else command date "$@"; fi\n}\n')
            controller = controller.replace("STATE_DIR=${OPU_PLAN_STATE_DIR:-/var/lib/oracle-patching-plans}\n",
                                            "STATE_DIR=${OPU_PLAN_STATE_DIR:-/var/lib/oracle-patching-plans}\n" + replacement, 1)
        if verification_hook:
            controller = controller.replace("verify_execution_evidence() {\n", "verify_execution_evidence() {\n" + verification_hook, 1)
        if renew_hook:
            begin = controller.index("renew() (\n")
            end = controller.index("\n)\n", begin) + 3
            body = controller[begin:end].replace('  target_reservation "$id" verify', renew_hook + '  target_reservation "$id" verify', 1)
            controller = controller[:begin] + body + controller[end:]
        if renew_failure:
            controller = controller.replace("renew() (\n", "renew() (\n" +
                '  if [ -e ' + shlex.quote(str(self.base / "reject-renew")) + ' ]; then\n'
                '    opu_error "fixture renewal failure"; exit 75\n  fi\n', 1)
        self.controller.write_text(controller)
        executor = (ROOT / "bin/opu-database-single-instance-patch").read_text().replace(ROOT_LINE, "ROOT=" + shlex.quote(str(ROOT)), 1)
        executor = executor.replace('PLAN_TOOL="$ROOT/bin/opu-patch-plan"', "PLAN_TOOL=" + shlex.quote(str(self.controller)), 1)
        if evidence_hook:
            executor = executor.replace("emit_evidence() {\n", "emit_evidence() {\n" + evidence_hook, 1)
        self.executor.write_text(executor)
        self.controller.chmod(0o750)
        self.executor.chmod(0o750)
        self.commands = []
        now = time.time()
        iso = lambda stamp: dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        args = ["create", "--plan-id", name, "--requester", "requester",
                "--window-start", iso(now - 60), "--window-end", iso(now + 3600)]
        for key, flag in (("readiness", "--readiness"), ("reconciliation", "--reconciliation"),
                          ("artifact", "--artifact-manifest"), ("procedure", "--procedure-validation"),
                          ("compatibility", "--compatibility"), ("policy", "--policy"), ("recovery", "--recovery-evidence")):
            args.extend((flag, str(self.fixture["evidence"][key])))
        self.call(*args)
        self.call("approve", "--plan-id", name, "--actor", "approver", "--approval-ticket", "CHG-finalization")
        self.call("authorize", "--plan-id", name, "--actor", "operator")
        self.call("dispatch", "--plan-id", name, "--actor", "operator")
        self.task_id = json.loads(self.call("next", "--plan-id", name).stdout)["task_id"]
        self.plan = self.state / "plans" / name
        self.task_file = self.plan / "tasks" / (self.task_id + ".json")
        self.task_dir = Path(self.env["OPU_SINGLE_INSTANCE_STATE_DIR"]) / "plans" / name / "tasks" / self.task_id

    def run(self, args, *, rc=0):
        started = time.time()
        result = subprocess.run(list(map(str, args)), env=self.env, text=True, capture_output=True, timeout=150)
        self.commands.append({"args": list(map(str, args)), "started": started, "finished": time.time(),
                              "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        (self.base / "commands.json").write_text(json.dumps(self.commands, indent=2))
        assert result.returncode == rc, (self.name, result.returncode, rc, result.stderr)
        return result

    def call(self, *args, rc=0):
        return self.run([self.controller, *args], rc=rc)

    def execute(self, *, rc=0):
        return self.run([self.executor, "execute", "--plan-id", self.name, "--task-id", self.task_id,
                         "--actor", "worker", "--lease-seconds", "30"], rc=rc)

    def task(self):
        return json.loads(self.task_file.read_text())

    def events(self):
        return [json.loads(line) for line in (self.plan / "events.jsonl").read_text().splitlines()]

    def evidence(self):
        return self.task_dir / "evidence.json"

    def complete(self, *, actor="worker", rc=0):
        return self.call("complete", "--plan-id", self.name, "--task-id", self.task_id, "--actor", actor,
                         "--status", "succeeded", "--evidence", str(self.evidence()), rc=rc)


def slow_evidence(base):
    f = Fixture(base, "slow-evidence", evidence_hook='  date -u +%s >"$TASK_DIR/evidence-started"\n  sleep 32\n')
    f.execute()
    task = f.task()
    assert task["status"] == "succeeded" and task["completed_after_lease"] is False
    assert task["lease_renewed_at_epoch"] > int((f.task_dir / "evidence-started").read_text())
    assert task["completed_at_epoch"] > task["claimed_at_epoch"] + 30
    assert (f.plan / "state").read_text().strip() == "running"
    f.call("task-status", "--plan-id", f.name, "--task-id", f.task_id)
    assert json.loads(f.call("next", "--plan-id", f.name).stdout)["task_id"] != f.task_id
    assert not any(event["event"] == "late_evidence_recorded" for event in f.events())


def slow_verification(base):
    # Once complete holds the plan lock, wait beyond the genuine last renewed
    # lease. Renew cannot acquire that same lock while verification is running.
    hook = ('  local boundary_expiry boundary_now\n'
            '  boundary_expiry=$(jq -r .lease_expires_epoch "$(plan_dir "$1")/tasks/$2.json")\n'
            '  boundary_now=$(date -u +%s)\n'
            '  printf "%s %s\\n" "$boundary_now" "$boundary_expiry" >"$(plan_dir "$1")/verification-admission"\n'
            '  while [ "$(date -u +%s)" -le "$boundary_expiry" ]; do sleep 1; done\n')
    f = Fixture(base, "slow-verification", verification_hook=hook)
    f.execute()
    task = f.task()
    admitted, expiry = map(int, (f.plan / "verification-admission").read_text().split())
    assert admitted < expiry < task["completed_at_epoch"]
    assert task["completion_admitted_at_epoch"] <= admitted < expiry
    assert task["status"] == "succeeded" and task["completed_after_lease"] is False
    assert (f.plan / "state").read_text().strip() == "running"
    f.call("task-status", "--plan-id", f.name, "--task-id", f.task_id)


def failed_stage(base):
    f = Fixture(base, "failed-stage")
    f.fixture["state"]["fail_applicability"].touch()
    f.execute(rc=73)
    task = f.task()
    evidence = json.loads(f.evidence().read_text())
    assert task["status"] == "failed" and task["completed_after_lease"] is False
    assert evidence["postcondition"]["status"] == "failed"
    f.call("retry-task", "--plan-id", f.name, "--task-id", f.task_id, "--actor", "operator")
    assert "completion_admitted_at_epoch" not in f.task()
    assert "lease_renewed_at_epoch" not in f.task()


def slow_renewal(base):
    hook = ('  local boundary_expiry\n'
            '  boundary_expiry=$(jq -r .lease_expires_epoch "$(plan_dir "$id")/tasks/$task_id.json")\n'
            '  printf "%s %s\\n" "$admitted" "$boundary_expiry" >"$(plan_dir "$id")/renewal-admission"\n'
            '  while [ "$(date -u +%s)" -le "$boundary_expiry" ]; do sleep 1; done\n')
    f = Fixture(base, "slow-renewal", renew_hook=hook)
    args = ("--plan-id", f.name, "--task-id", f.task_id, "--actor", "worker", "--lease-seconds", "30")
    f.call("claim", *args)
    f.call("renew", *args)
    admitted, expiry = map(int, (f.plan / "renewal-admission").read_text().split())
    task = f.task()
    assert admitted < expiry < task["lease_renewed_at_epoch"] < task["lease_expires_epoch"]
    # Do not revive ownership already stale at admission, even with this
    # identical worker and unchanged task definition.
    task["lease_expires_epoch"] = int(time.time()) - 1
    f.task_file.write_text(json.dumps(task))
    f.call("renew", *args, rc=75)
    assert f.task()["lease_expires_epoch"] == task["lease_expires_epoch"]


def failed_heartbeat_during_evidence(base):
    case = base / "failed-heartbeat"
    hook = ('  : >' + shlex.quote(str(case / "reject-renew")) + '\n  sleep 12\n')
    f = Fixture(base, "failed-heartbeat", evidence_hook=hook, renew_failure=True)
    f.execute(rc=75)
    assert f.evidence().is_file() and (f.task_dir / "heartbeat-failed").is_file()
    assert f.task()["status"] == "running"
    assert not (f.plan / "evidence" / f.task_id).exists()
    assert "fixture renewal failure" in (f.task_dir / "heartbeat-error.log").read_text()
    assert not any(event["event"] == "task_completed" for event in f.events())


def final_window(base):
    f = Fixture(base, "final-window", clock=True)
    plan = json.loads((f.plan / "plan.json").read_text())
    end = int(dt.datetime.fromisoformat(plan["maintenance_window"]["end"].replace("Z", "+00:00")).timestamp())
    args = ("--plan-id", f.name, "--task-id", f.task_id, "--actor", "worker", "--lease-seconds", "3600")
    # New ownership still requires a full 30-second opportunity.
    f.env["OPU_TEST_CONTROLLER_EPOCH"] = str(end - 20)
    result = f.call("claim", *args, rc=65)
    assert "less than 30 seconds" in result.stderr and f.task()["status"] == "pending"
    del f.env["OPU_TEST_CONTROLLER_EPOCH"]
    f.call("claim", *args)
    assert f.task()["lease_expires_epoch"] == end
    for remaining in (20, 1):
        f.env["OPU_TEST_CONTROLLER_EPOCH"] = str(end - remaining)
        f.call("renew", *args)
        task = f.task()
        assert task["lease_expires_epoch"] == end and task["lease_renewed_at_epoch"] == end - remaining
    f.call("renew", "--plan-id", f.name, "--task-id", f.task_id, "--actor", "stranger", rc=77)
    f.env["OPU_TEST_CONTROLLER_EPOCH"] = str(end)
    before = f.task_file.read_bytes()
    result = f.call("renew", *args, rc=65)
    assert "maintenance window is not open" in result.stderr and f.task_file.read_bytes() == before


def failed_handoff(base):
    f = Fixture(base, "failed-handoff", renew_failure=True)
    # Fail only the synchronous handoff after the immutable record was sealed.
    source = f.executor.read_text()
    needle = '  opu_execution_finish_heartbeat "$status" "$postcondition" "$heartbeat_failed" || return $?'
    assert source.count(needle) == 1
    source = source.replace(needle, '  : >' + shlex.quote(str(f.base / "reject-renew")) + '\n' + needle)
    f.executor.write_text(source)
    f.execute(rc=75)
    assert f.task()["status"] == "running" and f.evidence().is_file()
    assert not (f.plan / "evidence" / f.task_id).exists()

    # Stale ownership already present at controller admission still pauses;
    # evidence itself never authorizes a fresh claim or unsafe retry.
    task = f.task()
    task["lease_expires_epoch"] = int(time.time()) - 1
    f.task_file.write_text(json.dumps(task))
    f.complete(actor="stranger", rc=77)
    f.complete()
    assert f.task()["completed_after_lease"] is True
    assert f.task()["completion_admitted_at_epoch"] > f.task()["lease_expires_epoch"]
    assert (f.plan / "state").read_text().strip() == "paused"
    f.call("retry-task", "--plan-id", f.name, "--task-id", f.task_id, "--actor", "operator", rc=65)


def expired_beyond_grace(base):
    f = Fixture(base, "expired-beyond-grace", evidence_hook='  return 74\n')
    f.execute(rc=74)
    task = f.task()
    task["lease_expires_epoch"] = int(time.time()) - 901
    f.task_file.write_text(json.dumps(task))
    f.complete(rc=75)
    assert f.task()["status"] == "running"
    f.call("reconcile", "--plan-id", f.name, "--actor", "operator")
    assert f.task()["status"] == "unknown"
    f.complete(rc=65)


def failed_evidence_cleanup(base):
    f = Fixture(base, "failed-evidence", evidence_hook='  printf "%s\\n" "$OPU_EXECUTION_HEARTBEAT_PID" >"$TASK_DIR/heartbeat-pid"\n  return 74\n')
    f.execute(rc=74)
    assert f.task()["status"] == "running" and not f.evidence().exists()
    pid = int((f.task_dir / "heartbeat-pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("failed evidence left its heartbeat running")


def invalid_evidence(base):
    f = Fixture(base, "invalid-evidence")
    source = f.executor.read_text()
    needle = '  opu_execution_finish_heartbeat "$status" "$postcondition" "$heartbeat_failed" || return $?'
    source = source.replace(needle, needle + '\n  printf "tampered\\n" >>"$TASK_DIR/stdout.log"')
    f.executor.write_text(source)
    f.execute(rc=65)
    assert f.task()["status"] == "running" and not (f.plan / "evidence" / f.task_id).exists()
    assert "digest changed" in f.commands[-1]["stderr"]


def heartbeat_failure_classification(base):
    case = base / "heartbeat-classification"
    case.mkdir()
    (case / "heartbeat-failed").touch()
    source = '. ' + shlex.quote(str(ROOT / "lib/opu/common.sh")) + '\n. ' + shlex.quote(str(ROOT / "lib/opu/execution.sh"))
    for previously_failed, expected in (("0", 75), ("1", 0)):
        result = subprocess.run(["bash", "-c", source + '\nTASK_DIR=$1\nopu_execution_finish_heartbeat failed unknown "$2"',
                                 "fixture", str(case), previously_failed], capture_output=True, text=True, timeout=10)
        assert result.returncode == expected, (result.returncode, result.stderr)


def supervisor_signal_cleanup(base):
    case = base / "signal-cleanup"
    case.mkdir()
    tool = case / "renew"
    tool.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$$" >"$TASK_DIR/renew-pid"\nsleep 2\nprintf done >"$TASK_DIR/renew-finished"\n')
    tool.chmod(0o750)
    source = ('set -euo pipefail\n. ' + shlex.quote(str(ROOT / "lib/opu/common.sh")) + '\n. ' +
              shlex.quote(str(ROOT / "lib/opu/execution.sh")) + '\n'
              'export TASK_DIR=$1; PLAN_TOOL=$2; PLAN_STATE_DIR=$1; PLAN_ID=fixture; TASK_ID=fixture; ACTOR=worker; LEASE_SECONDS=30\n'
              # Shorten only the unit-test sleep, not a native lease or policy.
              'sleep() { command sleep 0.01; }\n'
              'opu_execution_start_heartbeat\nprintf "%s\\n" "$OPU_EXECUTION_HEARTBEAT_PID" >"$TASK_DIR/heartbeat-pid"\n'
              'wait "$OPU_EXECUTION_HEARTBEAT_PID"\n')
    process = subprocess.Popen(["bash", "-c", source, "fixture", str(case), str(tool)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not (case / "renew-pid").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (case / "renew-pid").exists(), "heartbeat did not begin renewal"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 143, (process.returncode, stdout, stderr)
        assert (case / "renew-finished").read_text() == "done", "supervisor orphaned in-flight renew"
        for path in (case / "heartbeat-pid", case / "renew-pid"):
            try:
                os.kill(int(path.read_text()), 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(f"supervisor exit left {path.name} running")
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)


def main():
    base = Path(tempfile.mkdtemp(prefix="opu-execution-finalization-")).resolve()
    cases = (slow_evidence, slow_verification, slow_renewal, final_window, failed_stage, failed_heartbeat_during_evidence,
             failed_handoff, expired_beyond_grace, failed_evidence_cleanup, invalid_evidence,
             heartbeat_failure_classification, supervisor_signal_cleanup)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = {pool.submit(case, base): case.__name__ for case in cases}
            for result in as_completed(results):
                result.result()
                print("execution finalization:", results[result], "passed", flush=True)
    except BaseException:
        print("execution finalization evidence retained:", base, file=sys.stderr)
        raise
    else:
        shutil.rmtree(base)
    print(f"execution finalization: {len(cases)} cases passed")


if __name__ == "__main__":
    main()
