#!/usr/bin/env python3
"""Exercise native upgrade interruption recovery using isolated service doubles."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
script = (ROOT / "bin/opu-opatch-upgrade").read_text()
definitions = script[script.index("usage() {"):script.rindex('\nmain "$@"')]


def seal(path, payload):
    canonical = json.dumps(payload, separators=(",", ":")) + "\n"
    payload["record_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    path.write_text(json.dumps(payload))


DOUBLES = r'''
require_root() { :; }
pmon_sids() { if [ -f "$RUNTIME/db-up" ]; then printf 'DB1\n'; fi; }
listener_names() { if [ -f "$RUNTIME/listener-up" ]; then printf 'LISTENER1\n'; fi; }
running_from_home() { if [ -f "$RUNTIME/db-up" ] || [ -f "$RUNTIME/listener-up" ]; then printf '123\n'; fi; }
as_owner() {
    local input
    case "$*" in
      *sqlplus*)
        input=$(cat)
        if [[ "$input" == *'shutdown immediate'* ]]; then
            rm -f "$RUNTIME/db-up"
            [ ! -f "$RUNTIME/fail-shutdown" ] || return 72
        elif [[ "$input" == *'startup;'* ]]; then
            if (: >&7) 2>/dev/null || (: >&8) 2>/dev/null; then return 99; fi
            printf 'start\n' >>"$RUNTIME/database-starts"
            : >"$RUNTIME/db-up"
        fi
        ;;
      *'lsnrctl stop'*)
        [ ! -f "$RUNTIME/fail-listener-stop" ] || return 73
        rm -f "$RUNTIME/listener-up"
        ;;
      *'lsnrctl status'*) [ -f "$RUNTIME/listener-up" ] || return 1 ;;
      *'lsnrctl start'*)
        if (: >&7) 2>/dev/null || (: >&8) 2>/dev/null; then return 99; fi
        printf 'start\n' >>"$RUNTIME/listener-starts"
        [ ! -f "$RUNTIME/fail-listener-start" ] || return 74
        : >"$RUNTIME/listener-up"
        ;;
      *) return 98 ;;
    esac
}
'''

with tempfile.TemporaryDirectory(prefix="opu-upgrade-lifecycle-") as temporary:
    base = Path(temporary)
    request = base / "state/upgrade1/request.json"
    request.parent.mkdir(parents=True)
    runtime = base / "runtime"
    runtime.mkdir()
    locks = base / "locks"
    locks.mkdir()
    env = {**os.environ, "RUNTIME": str(runtime), "OPU_EXECUTION_LOCK_DIR": str(locks)}
    common = '. ' + shlex.quote(str(ROOT / "lib/opu/common.sh")) + '\n. ' + shlex.quote(str(ROOT / "lib/opu/execution.sh"))
    prefix = "set -euo pipefail\n" + common + "\n" + definitions + "\nSTATE_ROOT=" + shlex.quote(str(base / "state")) + "\n"
    payload = {"schema_version": "1.0", "request_id": "upgrade1", "state": "approved",
               "requester": "requester", "target": {"oracle_home": "/fixture/home", "owner": "oracle"},
               "artifact": {"zip_sha256": "a" * 64, "stage_sha256": "b" * 64},
               "required_opatch_version": "12.2.0.1.51"}

    def run(command, doubles=True):
        return subprocess.run(["bash", "-c", prefix + (DOUBLES if doubles else "") + "\n" + command],
                              env=env, text=True, capture_output=True, timeout=10)

    # A shared Oracle owner is not sufficient provenance to stop a database;
    # the observed PMON must execute from the requested home.
    result = run(r'''
ps() { printf '101 oracle ora_pmon_OTHER\n102 oracle ora_pmon_DB1\n103 oracle ora_pmon_HIDDEN\n104 another ora_pmon_FOREIGN\n'; }
readlink() { case "$2" in /proc/101/exe) printf /other/home/bin/oracle;; /proc/102/exe|/proc/104/exe) printf /fixture/home/bin/oracle;; *) return 1;; esac; }
pmon_sids oracle /fixture/home
''', doubles=False)
    assert result.returncode == 0 and result.stdout == "DB1\n", result

    # Failures after shutdown retain enough sealed context to restore exactly
    # the stopped services, even if the process is retried in a new invocation.
    for failure in ("fail-shutdown", "fail-listener-stop"):
        seal(request, dict(payload))
        (runtime / "db-up").touch()
        (runtime / "listener-up").touch()
        (runtime / failure).touch()
        result = run('quiesce --request-id upgrade1 --actor worker')
        assert result.returncode != 0, result.stdout
        saved = json.loads(request.read_text())
        assert saved["state"] == "quiescing", saved
        assert saved["quiesce"]["oracle_sid"] == "DB1" and saved["quiesce"]["listener"] == "LISTENER1"
        (runtime / failure).unlink()
        result = run('exec 7>"$RUNTIME/fd7" 8>"$RUNTIME/fd8"; resume --request-id upgrade1 --actor worker')
        assert result.returncode == 0, result.stderr
        assert (runtime / "db-up").exists() and (runtime / "listener-up").exists()

    # A listener startup failure is retryable without issuing another database
    # startup against an already running instance.
    seal(request, {**payload, "state": "applied", "quiesce": {"oracle_sid": "DB1", "listener": "LISTENER1"}})
    (runtime / "db-up").unlink()
    (runtime / "listener-up").unlink()
    before = (runtime / "database-starts").read_text()
    (runtime / "fail-listener-start").touch()
    result = run('resume --request-id upgrade1 --actor worker')
    assert result.returncode != 0, result.stdout
    assert json.loads(request.read_text())["state"] == "resuming"
    (runtime / "fail-listener-start").unlink()
    result = run('resume --request-id upgrade1 --actor worker')
    assert result.returncode == 0, result.stderr
    assert (runtime / "database-starts").read_text() == before + "start\n"

    # Read operations cannot observe a record while its writer holds the
    # request lock. Mutations also honor the shared native host lock.
    seal(request, dict(payload))
    for lock_path, command in ((request.parent / "request.lock", 'main status --request-id upgrade1'),
                               (locks / "host-mutation.lock", 'main quiesce --request-id upgrade1 --actor worker')):
        with lock_path.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(command + '; printf "UNEXPECTED_SUCCESS\\n"')
        assert result.returncode != 0 and "UNEXPECTED_SUCCESS" not in result.stdout, result
        assert "lock" in result.stderr.lower(), result.stderr

    # Binary replacement records its attempt before the first move. A failed
    # move or interruption cannot leave the request eligible to resume services
    # while the OPatch directory's state is unresolved.
    home = base / "oracle-home"
    (home / "OPatch").mkdir(parents=True)
    (home / "OPatch/original").write_text("original")
    seal(request, {**payload, "target": {"oracle_home": str(home), "owner": pwd.getpwuid(os.getuid()).pw_name},
                   "artifact": {**payload["artifact"], "stage_dir": str(base / "stage")},
                   "backup_root": str(base / "backups")})
    result = run(r'''
analyze() { printf '{"runtime":{"process_ids":[]}}\n'; }
as_owner() { return 81; }
apply --request-id upgrade1 --actor worker
''')
    assert result.returncode != 0, result.stdout
    assert json.loads(request.read_text())["state"] == "applying"
    assert (home / "OPatch/original").read_text() == "original"
    for state in ("applying", "rolling_back"):
        seal(request, {**payload, "state": state, "quiesce": {"oracle_sid": "DB1", "listener": "LISTENER1"}})
        before = (runtime / "database-starts").read_text()
        result = run('resume --request-id upgrade1 --actor worker')
        assert result.returncode != 0 and (runtime / "database-starts").read_text() == before

print("OPatch upgrade lifecycle: interrupted service recovery, retry and native lock exclusion passed")
