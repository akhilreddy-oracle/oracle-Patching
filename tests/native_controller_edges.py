#!/usr/bin/env python3
"""Real controller helpers: stable locks, known-state retries and UTC windows."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def command(tool, script, *args):
    return ["bash", "-c", '. ' + shlex.quote(str(ROOT / "bin" / tool)) + ' help >/dev/null\n' + script,
            "fixture", *map(str, args)]


def wait_for(path, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"fixture marker missing: {path}; process={process.poll()}")


class ControllerEdges(unittest.TestCase):
    def test_plan_lock_keeps_inode_and_serializes_real_holders(self):
        with tempfile.TemporaryDirectory(prefix="opu-controller-") as tmp:
            base = Path(tmp)
            directory = base / "plans" / "test"
            directory.mkdir(parents=True)
            env = dict(os.environ, OPU_PLAN_STATE_DIR=tmp)
            first = subprocess.Popen(command("opu-patch-plan", '''
lock test
trap unlock EXIT
touch "$1/held"
while [ ! -e "$1/release" ]; do sleep 0.02; done
''', base), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second = None
            try:
                wait_for(base / "held", first)
                inode = (directory / ".task-lock").stat().st_ino
                second = subprocess.Popen(command("opu-patch-plan", '''
touch "$1/waiting"
lock test
trap unlock EXIT
touch "$1/entered"
''', base), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                wait_for(base / "waiting", second)
                time.sleep(0.25)
                self.assertFalse((base / "entered").exists(), "second writer bypassed a live holder")
                self.assertIsNone(second.poll())
                (base / "release").touch()
                self.assertEqual(first.communicate(timeout=5)[1], "")
                self.assertEqual(first.returncode, 0)
                self.assertEqual(second.communicate(timeout=5)[1], "")
                self.assertEqual(second.returncode, 0)
                self.assertTrue((base / "entered").exists())
                self.assertEqual((directory / ".task-lock").stat().st_ino, inode)
            finally:
                for process in (first, second):
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()

    def test_plan_lock_refuses_unsafe_paths_without_changing_them(self):
        with tempfile.TemporaryDirectory(prefix="opu-controller-") as tmp:
            base = Path(tmp)
            directory = base / "plans" / "test"
            directory.mkdir(parents=True)
            lock = directory / ".task-lock"
            victim = base / "victim"
            victim.write_text("preserve me\n")
            for kind in ("legacy_directory", "symlink", "hardlink", "fifo"):
                with self.subTest(kind=kind):
                    if kind == "legacy_directory":
                        lock.mkdir()  # The old mkdir-before-PID race must fail closed.
                    elif kind == "symlink":
                        lock.symlink_to(victim)
                    elif kind == "hardlink":
                        os.link(victim, lock)
                    else:
                        os.mkfifo(lock)
                    inode = lock.lstat().st_ino
                    result = subprocess.run(command("opu-patch-plan", "lock test"),
                                            env=dict(os.environ, OPU_PLAN_STATE_DIR=tmp),
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 75, result.stderr)
                    self.assertEqual(lock.lstat().st_ino, inode)
                    self.assertEqual(victim.read_text(), "preserve me\n")
                    lock.rmdir() if kind == "legacy_directory" else lock.unlink()

    def test_retry_uses_dispatched_stage_names_and_refuses_unknown_mutation(self):
        with tempfile.TemporaryDirectory(prefix="opu-controller-") as tmp:
            evidence = Path(tmp) / "evidence.json"
            stages = ("rac_rollback_datapatch", "rac_rollback_final_validate", "ojvm_datapatch_upgrade", "oop_validate_clone")
            for stage in stages:
                for outcome, expected in (("binary_state_known", 0), ("binary_state_unknown", 1), ("recovery_required", 1)):
                    with self.subTest(stage=stage, outcome=outcome):
                        evidence.write_text(json.dumps({"stage": stage, "outcome_class": outcome,
                                                        "postcondition": {"status": "failed"}, "exit_code": 1}))
                        result = subprocess.run(command("opu-patch-plan", 'retry_evidence_safe "$1"', evidence), capture_output=True, text=True)
                        self.assertEqual(result.returncode, expected, result.stderr)
            for stage in ("apply", "rollback_binary", "ojvm_binary", "oop_switch", "cluster_rollback_datapatch", "ojvm_datapatch", "oop_validate"):
                evidence.write_text(json.dumps({"stage": stage, "outcome_class": "binary_state_known",
                                                "postcondition": {"status": "failed"}, "exit_code": 1}))
                result = subprocess.run(command("opu-patch-plan", 'retry_evidence_safe "$1"', evidence), capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, stage)

    def test_recovery_windows_require_real_utc_calendar_timestamps(self):
        for start, end, valid in (
            ("2028-02-29T00:00:00Z", "2028-03-01T00:00:00Z", True),
            ("tomorrow", "tomorrow +1 hour", False),
            ("2026-02-29T00:00:00Z", "2026-03-01T00:00:00Z", False),
            ("2026-02-30T00:00:00Z", "2026-03-03T00:00:00Z", False),
            ("2026-09-17T24:00:00Z", "2026-09-19T00:00:00Z", False),
            ("2026-09-17T00:00:00Z", "2026-09-18T00:00:01Z", False),
            ("2026-09-17T00:00:00Z", "2026-09-17T00:00:00Z", False),
        ):
            with self.subTest(start=start, end=end):
                result = subprocess.run(command("opu-database-recovery-prepare", 'verify_window_shape "$1" "$2"', start, end),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, valid, result.stderr)


if __name__ == "__main__":
    unittest.main()
