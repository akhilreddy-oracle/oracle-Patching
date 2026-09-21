#!/usr/bin/env python3
"""Offline plan transport regressions; shell fixtures use only temporary files."""
import json
import hashlib
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from runtime_fixture import runtime_receipt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import planctl


class PlanTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host = {"id": "tenant.test", "ssh_alias": "fixture", "remote_root": str(self.root / "remote"), "sudo": True}
        self.state = self.root / "local"
        self.enterContext(patch.object(planctl, "PLAN_STATE_DIR", self.state))
        self.enterContext(patch.object(planctl.pipeline_runner, "set_execution_context"))
        self.enterContext(patch.object(planctl.pipeline_runner, "record_event"))
        self.enterContext(patch.object(planctl.pipeline_runner, "controller_poll"))

    def test_complete_identity_selects_matching_host_independently_of_order(self):
        hosts = [{"id": "tenant.prod", "ssh_alias": "production"}, {"id": "tenant.test", "ssh_alias": "testing"}]
        for order in (hosts, hosts[::-1]):
            with self.subTest(order=order), patch.object(planctl, "_load_hosts", return_value={host["id"]: host for host in order}):
                selected = planctl._resolve_node_host("tenant.test")
                self.assertEqual((selected["id"], selected["ssh_alias"], selected["node_name"]),
                                 ("tenant.test", "testing", "tenant.test"))
                with self.assertRaises(planctl.PlanError):
                    planctl._resolve_node_host("tenant")
                with self.assertRaises(planctl.PlanError):
                    planctl._resolve_node_host("tenant.unknown")

    def test_unique_short_alias_remains_supported_without_overriding_unwired_node(self):
        host = {"id": "cluster", "ssh_alias": "coordinator", "nodes": [
            {"name": "member.prod", "ssh_alias": "member"}, {"name": "offline.prod", "ssh_alias": None}]}
        with patch.object(planctl, "_load_hosts", return_value={"cluster": host}):
            self.assertEqual(planctl._resolve_node_host("member")["ssh_alias"], "member")
            self.assertEqual(planctl._resolve_node_host("member.prod")["node_name"], "member.prod")
            with self.assertRaises(planctl.PlanError):
                planctl._resolve_node_host("offline.prod")
        with patch.object(planctl, "_load_hosts", return_value={"short": {"id": "short", "ssh_alias": "short-ssh"}}):
            self.assertEqual(planctl._resolve_node_host("short.example")["ssh_alias"], "short-ssh")

    def test_duplicate_node_names_fail_before_preflight_can_select_an_estate(self):
        hosts = {key: {"id": key, "ssh_alias": key, "nodes": [{"name": "shared"}]} for key in ("one", "two")}
        with patch.object(planctl, "_load_hosts", return_value=hosts):
            with self.assertRaises(planctl.PlanError):
                planctl.preflight_live_plan_nodes({"nodes": ["shared"]})

    def test_qualified_node_names_are_not_mistaken_for_coordinator_placeholders(self):
        for name in ("local", "cluster"):
            with self.subTest(name=name):
                hosts = {key: {"id": key, "ssh_alias": key} for key in (name + ".one", name + ".two")}
                plan = {"nodes": list(hosts), "target": {"coordinator_node": name + ".one"}}
                with patch.object(planctl, "_load_hosts", return_value=hosts):
                    self.assertEqual(planctl._resolve_live_host_for_task(plan, {"node": name + ".two"})["id"], name + ".two")
                    self.assertEqual(planctl._resolve_live_host_for_task(plan, {"node": name})["id"], name + ".one")

    def test_native_single_task_preflight_and_selection_use_one_inventory_snapshot(self):
        first = {"id": "node", "ssh_alias": "reviewed.invalid"}
        changed = {**first, "ssh_alias": "changed.invalid"}
        with patch.object(planctl, "_load_hosts", side_effect=[{"node": first}, {"node": changed}]) as load:
            selected = planctl._resolve_live_host_for_task({"nodes": ["node"]}, {"node": "node"})
        self.assertEqual(selected["ssh_alias"], "reviewed.invalid")
        load.assert_called_once()
        self.assertIsNone(planctl._PINNED_HOSTS.get())

    def test_native_remaining_tasks_pin_routes_even_without_a_chat_confirmation(self):
        host = {"id": "node", "ssh_alias": "initial.invalid", "nodes": [{"name": "node", "ssh_alias": "initial.invalid"}]}
        hosts = {"node": host}
        plan = {"plan_id": "plan", "nodes": ["node"], "state": "running"}
        tasks = [{"task_id": "first", "node": "node"}, {"task_id": "second", "node": "node"}]
        selected = []
        def execute(plan_id, current_plan, task, actor):
            selected.append(planctl._resolve_live_host_for_task(current_plan, task)["ssh_alias"])
            host["nodes"][0]["ssh_alias"] = "changed.invalid"
            if len(selected) == 2:
                plan["state"] = "succeeded"
            return {"task_id": task["task_id"], "status": "succeeded"}
        with patch.object(planctl, "_load_hosts", return_value=hosts) as load, \
             patch.object(planctl, "status", return_value=plan), \
             patch.object(planctl, "next_task", side_effect=tasks), \
             patch.object(planctl, "_fixture_dir_for_plan", return_value=None), \
             patch.object(planctl, "_require_controller_transport"), \
             patch.object(planctl, "_execute_live", side_effect=execute):
            result = planctl.execute_remaining_tasks("plan", "operator")
            self.assertEqual(result["executed_count"], 2)
            self.assertEqual(selected, ["initial.invalid", "initial.invalid"])
            load.assert_called_once()
            self.assertIsNone(planctl._PINNED_HOSTS.get())
            self.assertEqual(planctl._resolve_live_host_for_task(plan, tasks[0])["ssh_alias"], "changed.invalid")

    def test_reconciliation_requires_the_exact_launched_task_definition_and_retry(self):
        host = {"id": "node", "node_name": "node", "ssh_alias": "never-connect", "remote_root": "/fixture"}
        context = {"plan_id": "plan", "task_id": "task", "node": "node", "host_id": "node",
            "ssh_alias": "never-connect", "remote_root": "/fixture",
            "remote_run_dir": "/fixture/var/webapp-runs/plan/task/" + "a" * 32,
            "task_definition_sha256": "b" * 64, "task_retry_count": 1,
            "execution_host_configuration_sha256": planctl.execution_host_binding(host)}
        terminal = {"plan_id": "plan", "task_id": "task", "status": "succeeded",
                    "task_definition_sha256": "b" * 64, "retry_count": 1}
        for difference in ({}, {"retry_count": 2}, {"retry_count": True}, {"task_definition_sha256": "c" * 64},
                           {"plan_id": "other"}, {"task_id": "other"}):
            with self.subTest(difference=difference), \
                 patch.object(planctl, "_resolve_node_host", return_value=host), \
                 patch.object(planctl.remote, "run_remote_shell", return_value=SimpleNamespace(returncode=0, stdout="RC\n0\n")), \
                 patch.object(planctl.remote, "run_remote_raw", side_effect=[
                     SimpleNamespace(returncode=0, stdout='{"task_id":"task","status":"succeeded"}'),
                     SimpleNamespace(returncode=0, stdout="")]), \
                 patch.object(planctl, "_sync_plan_from_host"), \
                 patch.object(planctl, "_run", return_value={**terminal, **difference}), \
                 patch.object(planctl, "status", return_value={"state": "running"}) as status:
                result = planctl.reconcile_detached_run({"context": context})
                self.assertEqual(result["status"], "unknown" if difference else "succeeded")
                if difference:
                    self.assertIn("different definition or retry", result["error"]["message"])
                    status.assert_not_called()
        for missing in ("task_definition_sha256", "task_retry_count"):
            legacy = {key: value for key, value in context.items() if key != missing}
            with self.subTest(missing=missing), \
                 patch.object(planctl, "_resolve_node_host", return_value=host), \
                 patch.object(planctl.remote, "run_remote_shell", return_value=SimpleNamespace(returncode=0, stdout="RC\n0\n")), \
                 patch.object(planctl.remote, "run_remote_raw", side_effect=[
                     SimpleNamespace(returncode=0, stdout='{"task_id":"task","status":"succeeded"}'),
                     SimpleNamespace(returncode=0, stdout="")]), \
                 patch.object(planctl, "_sync_plan_from_host"), \
                 patch.object(planctl, "_run", return_value=terminal):
                result = planctl.reconcile_detached_run({"context": legacy})
                self.assertEqual(result["status"], "unknown")
                self.assertIn("no verified task-attempt binding", result["error"]["message"])

    def test_reconciliation_rejects_changed_effective_host_before_any_remote_access(self):
        host = {"id": "node", "node_name": "node", "ssh_alias": "never-connect", "remote_root": "/fixture",
                "sudo": False, "nodes": [{"name": "node", "ssh_alias": "never-connect"}]}
        context = {"plan_id": "plan", "task_id": "task", "node": "node", "host_id": "node",
                   "ssh_alias": "never-connect", "remote_root": "/fixture",
                   "remote_run_dir": "/fixture/var/webapp-runs/plan/task/" + "a" * 32,
                   "execution_host_configuration_sha256": planctl.execution_host_binding(host)}
        for difference in ({"sudo": True}, {"ssh_alias": "changed"}, {"remote_root": "/other"},
                           {"id": "other"}, {"node_name": "other"}, {"future_transport_option": "changed"},
                           {"nodes": [{"name": "node", "ssh_alias": "changed"}]}):
            with self.subTest(difference=difference), \
                 patch.object(planctl, "_resolve_node_host", return_value={**host, **difference}), \
                 patch.object(planctl.remote, "run_remote_shell") as shell, \
                 patch.object(planctl.remote, "run_remote_raw") as raw, \
                 patch.object(planctl, "_sync_plan_from_host") as sync:
                result = planctl.reconcile_detached_run({"context": context})
                self.assertEqual(result["status"], "unknown")
                self.assertIn("host configuration changed", result["error"]["message"])
                shell.assert_not_called()
                raw.assert_not_called()
                sync.assert_not_called()

    def test_legacy_or_invalid_host_binding_is_never_inferred_from_current_hosts(self):
        base = {"plan_id": "plan", "task_id": "task", "node": "node", "host_id": "node",
                "ssh_alias": "never-connect", "remote_root": "/fixture"}
        for binding in (None, "", "not-a-digest", True, 42, "a" * 63):
            context = dict(base)
            if binding is not None:
                context["execution_host_configuration_sha256"] = binding
            with self.subTest(binding=binding), \
                 patch.object(planctl, "_resolve_node_host") as resolve, \
                 patch.object(planctl.remote, "run_remote_shell") as shell:
                result = planctl.reconcile_detached_run({"context": context})
                self.assertEqual(result["status"], "unknown")
                self.assertIn("no verified execution-host configuration binding", result["error"]["message"])
                resolve.assert_not_called()
                shell.assert_not_called()

    def test_effective_host_defaults_and_mapping_order_do_not_change_binding(self):
        host = {"id": "node", "ssh_alias": "never-connect", "remote_root": "/fixture"}
        with_defaults = {"sudo": False, "node_name": "node", **dict(reversed(list(host.items())))}
        self.assertEqual(planctl.execution_host_binding(host), planctl.execution_host_binding(with_defaults))

    def test_live_completion_cannot_mark_another_valid_retry_terminal(self):
        task = {"task_id": "task", "adapter": "database_single_instance_opatch",
                "task_definition_sha256": "b" * 64, "retry_count": 1}
        terminal = {**task, "plan_id": "plan", "status": "succeeded", "retry_count": 2}
        with patch.object(planctl.production, "require_live_mutation_allowed"), \
             patch.object(planctl, "_resolve_live_host_for_task", return_value=self.host), \
             patch.object(planctl, "_sync_plan_to_host", return_value=("/fixture/plans", runtime_receipt(self.host["ssh_alias"], self.host["remote_root"]))), \
             patch.object(planctl, "_run_detached_remote", return_value=(0, '{"task_id":"task","status":"succeeded"}', "")), \
             patch.object(planctl, "_sync_plan_from_host"), \
             patch.object(planctl, "_run", return_value=terminal), \
             patch.object(planctl, "status") as status:
            with self.assertRaisesRegex(planctl.PlanError, "different definition or retry"):
                planctl._execute_live("plan", {}, task, "operator")
            status.assert_not_called()
        self.assertFalse(any(call.kwargs.get("detached_terminal") is True
                             for call in planctl.pipeline_runner.set_execution_context.call_args_list))

    def test_launch_does_not_fork_when_directory_preparation_fails(self):
        for failure in ("mkdir", "cd"):
            with self.subTest(failure=failure):
                marker = self.root / (failure + "-launched")
                prefix = ("trap 'wait' EXIT; "
                          + ("mkdir() { return 41; }; " if failure == "mkdir" else "mkdir() { return 0; }; cd() { return 42; }; ")
                          + "sleep() { :; }; nohup() { printf attempted >" + shlex.quote(str(marker)) + "; }; ")
                def local_shell(alias, script, **kwargs):
                    return subprocess.run(["bash", "-c", prefix + script], cwd=self.root, capture_output=True, text=True, timeout=5)
                with patch.object(planctl.remote, "run_remote_shell", side_effect=local_shell) as remote_shell:
                    with self.assertRaises(planctl.PlanError):
                        planctl._run_detached_remote(self.host, "plan", "task", ["fixture-executor"])
                    self.assertEqual(remote_shell.call_count, 1)
                self.assertFalse(marker.exists(), "an executor was launched after its directory preflight failed")

    def test_successful_fixture_launch_uses_private_run_directory_and_returns_result(self):
        self.host["sudo"] = False
        expected = hashlib.sha256(json.dumps({**self.host, "node_name": self.host["id"]},
                                  sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        def local_shell(alias, script, **kwargs):
            # The binding must survive a disconnect at the very first SSH call.
            context = planctl.pipeline_runner.set_execution_context.call_args.kwargs
            self.assertEqual(context["execution_host_configuration_sha256"], expected)
            self.assertEqual(context["ssh_alias"], alias)
            self.assertEqual(kwargs["sudo"], False)
            return subprocess.run(["bash", "-c", script], cwd=self.root, capture_output=True, text=True, timeout=5)
        with patch.object(planctl.remote, "run_remote_shell", side_effect=local_shell), \
             patch.object(planctl.remote, "pull_file", side_effect=lambda alias, name, **kw: Path(name).read_bytes()), \
             patch.object(planctl, "LIVE_POLL_INTERVAL_SECONDS", 0.01):
            rc, stdout, stderr = planctl._run_detached_remote(self.host, "plan", "task", [sys.executable, "-c", "print('{\"status\":\"succeeded\"}')"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout), {"status": "succeeded"})
        self.assertEqual(stderr, "")
        directories = list((Path(self.host["remote_root"]) / "var/webapp-runs/plan/task").iterdir())
        self.assertEqual(len(directories), 1)
        self.assertEqual(stat.S_IMODE(directories[0].stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directories[0] / "stdout").stat().st_mode), 0o600)

    def test_archive_transfer_reserves_private_unique_directories_and_cleans_both_directions(self):
        plan = "fixture-" + self.root.name
        local = self.state / "plans" / plan
        local.mkdir(parents=True)
        document = local / "plan.json"
        document.write_text('{"generation":1}')
        local_lock = local / ".task-lock"
        local_lock.write_text("local lock")
        local_lock_inode = local_lock.stat().st_ino
        # Predictable historic paths may already be symlinks on a shared host.
        # A transfer must neither write them nor remove someone else's file.
        victim = self.root / "unrelated-file"
        victim.write_text("unchanged")
        legacy_paths = [Path("/tmp") / f"opu-plan-{plan}{suffix}.tar" for suffix in ("", "-back")]
        for legacy in legacy_paths:
            legacy.symlink_to(victim)
            self.addCleanup(legacy.unlink, missing_ok=True)
        allocations = []
        def local_shell(alias, script, **kwargs):
            self.assertTrue(kwargs["sudo"])
            response = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
            if response.returncode == 0:
                directory = Path(response.stdout.strip())
                allocations.append(directory)
                self.addCleanup(lambda directory=directory: directory.rmdir() if directory.exists() and not list(directory.iterdir()) else None)
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            return response
        def local_checked(alias, argv, **kwargs):
            self.assertTrue(kwargs["sudo"])
            result = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                                    env={**os.environ, "COPYFILE_DISABLE": "1"})
            if result.returncode:
                raise planctl.remote.RemoteError("fixture", "local transport failed", result.stderr)
            return result.stdout
        def local_push(alias, path, payload, **kwargs):
            self.assertTrue(kwargs["sudo"])
            self.assertIn(Path(path).parent, allocations)
            Path(path).write_bytes(payload)
        def local_pull(alias, path, **kwargs):
            self.assertTrue(kwargs["sudo"])
            self.assertIn(Path(path).parent, allocations)
            return Path(path).read_bytes()
        with patch.object(planctl.tools_sync, "ensure_tools", side_effect=runtime_receipt), \
             patch.object(planctl, "_sync_sealed_inputs_to_host"), \
             patch.object(planctl.remote, "run_remote_shell", side_effect=local_shell), \
             patch.object(planctl.remote, "run_remote_checked", side_effect=local_checked), \
             patch.object(planctl.remote, "push_file", side_effect=local_push), \
             patch.object(planctl.remote, "pull_file", side_effect=local_pull):
            remote_root, runtime = planctl._sync_plan_to_host(self.host, plan)
            remote_document = Path(remote_root) / "plans" / plan / "plan.json"
            self.assertEqual(json.loads(remote_document.read_text()), {"generation": 1})
            remote_lock = remote_document.parent / ".task-lock"
            self.assertFalse(remote_lock.exists())
            remote_lock.write_text("remote lock")
            remote_lock_inode = remote_lock.stat().st_ino
            planctl._sync_plan_to_host(self.host, plan)
            self.assertEqual((remote_lock.stat().st_ino, remote_lock.read_text()), (remote_lock_inode, "remote lock"))
            remote_document.write_text('{"generation":2}')
            planctl._sync_plan_from_host(self.host, plan, remote_root)
        self.assertEqual(json.loads(document.read_text()), {"generation": 2})
        self.assertEqual(len(set(allocations)), 3)
        self.assertTrue(all(not directory.exists() for directory in allocations))
        self.assertEqual(victim.read_text(), "unchanged")
        self.assertTrue(all(legacy.is_symlink() for legacy in legacy_paths))
        self.assertEqual((local_lock.stat().st_ino, local_lock.read_text()), (local_lock_inode, "local lock"))

    def test_failed_private_allocation_cannot_write_or_read_an_archive(self):
        for output in ("/tmp/opu-plan-transfer../unsafe", "/tmp/opu-plan-transfer.ABCDEFGHIJKL\nextra", ""):
            with self.subTest(output=output), \
                 patch.object(planctl.remote, "run_remote_shell", return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")), \
                 patch.object(planctl.remote, "run_remote_checked") as checked, \
                 patch.object(planctl.remote, "pull_file") as pull:
                with self.assertRaises(planctl.PlanError):
                    planctl._sync_plan_from_host(self.host, "plan", "/fixture")
                checked.assert_not_called()
                pull.assert_not_called()

    def test_dataguard_source_documents_are_collected_and_mirrored_with_exact_bytes(self):
        plan_id = "dg-fixture"
        plan_directory = self.state / "plans" / plan_id
        plan_directory.mkdir(parents=True)
        documents = {
            "dataguard_order": b'{"strategy": "standby_first", "members": ["STANDBY", "PRIMARY"]}\n',
            "dataguard_evaluation": b'{\n  "status": "ready_for_standby_first",\n  "target": {"db_unique_name": "DB_PRIMARY"}\n}\n',
        }
        bindings = {}
        for kind, payload in documents.items():
            path = self.root / (kind + " evidence.json")
            path.write_bytes(payload)
            bindings[kind] = {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}
        plan = {"plan_id": plan_id, "source_documents": bindings}
        (plan_directory / "plan.json").write_text(json.dumps(plan))
        self.assertEqual(set(planctl._sealed_input_paths(plan_id)), {item["path"] for item in bindings.values()})

        transfer = self.root / "private-transfer"
        transfer.mkdir(mode=0o700)
        remote_filesystem = self.root / "remote-filesystem"
        uploaded_documents = []
        def local_push(alias, path, payload, **kwargs):
            self.assertEqual(alias, self.host["ssh_alias"])
            self.assertTrue(kwargs["sudo"])
            destination = Path(path)
            if destination.parent != transfer:
                uploaded_documents.append(path)
                destination = remote_filesystem / destination.relative_to("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        def local_checked(alias, argv, **kwargs):
            self.assertEqual(alias, self.host["ssh_alias"])
            self.assertTrue(kwargs["sudo"])
            response = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                                      env={**os.environ, "COPYFILE_DISABLE": "1"})
            if response.returncode:
                raise planctl.remote.RemoteError("fixture", "local archive transfer failed", response.stderr)
            return response.stdout
        with patch.object(planctl.tools_sync, "ensure_tools", side_effect=runtime_receipt), \
             patch.object(planctl, "_temporary_remote_archive", return_value=str(transfer)), \
             patch.object(planctl.remote, "push_file", side_effect=local_push), \
             patch.object(planctl.remote, "run_remote_checked", side_effect=local_checked):
            remote_root, runtime = planctl._sync_plan_to_host(self.host, plan_id)
        transferred_plan = json.loads((Path(remote_root) / "plans" / plan_id / "plan.json").read_text())
        self.assertEqual(transferred_plan["source_documents"], bindings)
        self.assertCountEqual(uploaded_documents, [item["path"] for item in bindings.values()])
        for kind, binding in transferred_plan["source_documents"].items():
            mirrored = remote_filesystem / Path(binding["path"]).relative_to("/")
            self.assertEqual(mirrored.read_bytes(), documents[kind])
            self.assertEqual(hashlib.sha256(mirrored.read_bytes()).hexdigest(), binding["sha256"])
        self.assertFalse(transfer.exists())

    def test_archive_cleanup_runs_even_when_transfer_fails(self):
        directory = "/tmp/opu-plan-transfer.ABCDEFGHIJKL"
        with patch.object(planctl, "_temporary_remote_archive", return_value=directory), \
             patch.object(planctl.remote, "run_remote_checked") as checked, \
             patch.object(planctl.remote, "pull_file", side_effect=planctl.remote.RemoteError("fixture", "lost contact")):
            with self.assertRaises(planctl.PlanError):
                planctl._sync_plan_from_host(self.host, "plan", "/fixture")
        self.assertEqual([call.args[1] for call in checked.call_args_list][-2:],
                         [["rm", "-f", "--", directory + "/plan.tar"], ["rmdir", "--", directory]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
