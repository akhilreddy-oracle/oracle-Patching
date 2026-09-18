#!/usr/bin/env python3
"""Stable release-independent paths and fixture isolation admission checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp"))
import runtime_paths
import planctl
import recoveryctl


class RuntimePathTests(unittest.TestCase):
    def test_default_paths_preserve_existing_sealed_state(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime_paths.state_dir(), ROOT / "webapp/var")
            self.assertEqual(runtime_paths.hosts_file(), ROOT / "webapp/hosts.json")

    def test_generations_share_stable_state_without_environment_retargeting(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            for digest in ("a" * 64, "b" * 64):
                code = base / ".opu-runtimes" / digest
                (code / "webapp").mkdir(parents=True)
                with self.subTest(digest=digest), patch.object(runtime_paths, "ROOT", code / "webapp"), \
                        patch.dict(os.environ, {"OPU_DEPLOYMENT_ROOT": "/untrusted"}, clear=True):
                    self.assertEqual(runtime_paths.deployment_root(), base)
                    self.assertEqual(runtime_paths.state_dir(), base / "webapp/var")
                    self.assertEqual(runtime_paths.hosts_file(), base / "webapp/hosts.json")
                self.assertFalse((code / "webapp/var").exists())

    def test_malformed_or_aliased_generation_cannot_choose_state_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            real = base / ".opu-runtimes" / ("a" * 64)
            real.mkdir(parents=True)
            alias = real.parent / ("b" * 64)
            alias.symlink_to(real, target_is_directory=True)
            invalid = [Path("relative"), base / ".." / "outside", real.parent,
                       real.parent / ("A" * 64), real.parent / ("a" * 63), alias,
                       real / "nested", real / ".opu-runtimes" / ("b" * 64)]
            for code in invalid:
                with self.subTest(code=code), self.assertRaises(ValueError):
                    runtime_paths.deployment_root(code)

    def test_generation_pull_commands_use_existing_deployment_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            code = base / ".opu-runtimes" / ("a" * 64)
            files = ["bin/opu-agent-work-pull", "bin/opu-agent-work-run", "lib/opu/python.sh"]
            files += ["webapp/" + name + ".py" for name in
                      ("agent_worker", "agent_queue", "agent_enroll", "runtime_paths", "adapters", "durable", "diagnostics")]
            for relative in files:
                target = code / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            env = {key: value for key, value in os.environ.items() if not key.startswith("OPU_")}
            env.update(OPU_PLAN_STATE_DIR=str(base / "var/webapp-plans"), PYTHONDONTWRITEBYTECODE="1")
            for command in ("opu-agent-work-pull", "opu-agent-work-run"):
                result = subprocess.run([str(code / "bin" / command), "--node", "node1", "--agent-id", "worker"],
                                        env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "idle")
                self.assertTrue((base / "webapp/var/agent-queue/jobs").is_dir())
                self.assertFalse((code / "webapp/var").exists())

    def test_rejects_relative_empty_and_symlink_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to(Path(tmp).resolve(), target_is_directory=True)
            for value in ("", "var/state", str(link)):
                with self.subTest(value=value), patch.dict(os.environ, {"OPU_WEBAPP_STATE_DIR": value}):
                    with self.assertRaises(ValueError):
                        runtime_paths.state_dir()

    def test_all_imported_stores_use_persistent_state_and_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            code = '''import json,auth,company_auth,evidence,planctl,recoveryctl,pipeline_runner,fleet_metadata,agent_queue,agent_enroll,notifications,itsm,production,extjobctl,server
print(json.dumps([str(x) for x in [auth.TOKEN_FILE,auth.PRINCIPALS_FILE,company_auth.DEFAULT_CONFIG,company_auth.STATE_DIR,evidence.VAR_DIR,planctl.PLAN_STATE_DIR,planctl.TESTMODE_DIR,recoveryctl.LIVE_DIR,pipeline_runner.RUNS_DIR,fleet_metadata.STORE_FILE,agent_queue.DEFAULT_QUEUE_DIR,agent_enroll.DEFAULT_REGISTRY_FILE,notifications.VAR_DIR,itsm.DEFAULT_TICKETS_FILE,production.DEFAULT_CERT_FILE,extjobctl.REFERENCE_DIR,server.HOSTS_FILE,planctl.HOSTS_FILE,recoveryctl.HOSTS_FILE]]))'''
            env = {**os.environ, "PYTHONPATH": str(ROOT / "webapp"), "OPU_WEBAPP_STATE_DIR": str(root / "state"), "OPU_WEBAPP_HOSTS_FILE": str(root / "inventory.json")}
            result = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True, capture_output=True, check=True)
            paths = json.loads(result.stdout)
            for path in paths[:-3]:
                self.assertTrue(Path(path).is_relative_to(root / "state"), path)
            self.assertEqual(paths[-3:], [str(root / "inventory.json")] * 3)

    def test_disabled_fixture_executor_never_reaches_subprocess(self):
        with patch.dict(os.environ, {"OPU_WEBAPP_ALLOW_FIXTURES": "0"}), \
             patch.object(planctl.subprocess, "run") as run, \
             patch.object(planctl.testmode_fixtures, "env_for") as fixture:
            with self.assertRaises(ValueError):
                planctl._execute_testmode("example", {"adapter": "database_single_instance_opatch"}, "operator", Path("/tmp/fixture"))
            run.assert_not_called()
            fixture.assert_not_called()

    def test_disabled_recovery_execution_never_reaches_subprocess(self):
        with patch.dict(os.environ, {"OPU_WEBAPP_ALLOW_FIXTURES": "0"}), patch.object(recoveryctl.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                recoveryctl._run("demo", ["execute", "--request-id", "demo"])
            run.assert_not_called()

    def test_disabled_fixture_cannot_be_published_to_an_external_worker(self):
        import agent_queue
        with patch.dict(os.environ, {"OPU_WEBAPP_ALLOW_FIXTURES": "0"}), \
             patch.object(planctl, "status", return_value={}), \
             patch.object(planctl, "_fixture_dir_for_plan", return_value=Path("/tmp/fixture")), \
             patch.object(agent_queue, "publish_plan_tasks") as publish:
            with self.assertRaises(ValueError):
                planctl.publish_agent_queue("demo")
            publish.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
