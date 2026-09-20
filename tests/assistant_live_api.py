#!/usr/bin/env python3
"""Live inventory chat routing through real auth and isolated native run fixtures.

Discovery and model execution are mocked. No SSH, Oracle command, or real model
is invoked; receipts are built and verified by the production receipt module.
"""
import copy
import hashlib
import json
import random
import threading
import unittest
from unittest.mock import patch

import assistant_api as fixture
from assistant_api import assistant, evidence, pipeline_runner, server
import live_inventory


class LiveAssistantApiTests(fixture.AssistantApiTests):
    def setUp(self):
        super().setUp()
        self.steps["discovery"].side_effect = self.discover
        self.receipt_transform = lambda receipt: receipt
        self.snapshot_transform = lambda snapshot: snapshot

    def snapshot(self, patch_id="88888888"):
        return {"schema_version": "1.0", "collector": {"name": "oracle.topology.discover"},
            "collected_at": live_inventory.utc_now(),
            "oracle_homes": [{"path": "/fixture/current-home", "version": "19.0.0.0.0",
                "opatch_version": "12.2.0.1.51", "patches": [patch_id],
                "patch_inventory_source": "opatch_lsinventory_xml",
                "opatch_inventory_xml_status": "collected", "opatch_inventory_xml_sha256": "a" * 64}],
            "databases": [{"db_unique_name": "ORCL", "oracle_home": "/fixture/current-home",
                "runtime": {"status": "complete", "database_version": "19.0.0.0.0",
                            "sqlpatch_non_success": 0, "open_mode": "READ WRITE"}}]}

    def discover(self, host_id, host, body):
        self.assertIs(body.get("inventory_receipt"), True)
        self.assertEqual(body.get("actor"), body.get("requester"))
        run_id = pipeline_runner.current_run_id()
        self.assertIsNotNone(run_id)
        started = live_inventory.utc_now()
        snapshot = self.snapshot_transform(self.snapshot())
        receipt = live_inventory.build_receipt(host_id=host_id, host=host, run_id=run_id,
            started_at=started, completed_at=live_inventory.utc_now(), node_snapshots=[(host_id, snapshot)])
        return self.receipt_transform(receipt)

    def add_target(self):
        target = {**self.host, "id": "target", "label": "Target fixture"}
        self.hosts_file.write_text(json.dumps({"hosts": [self.host, target]}))
        self.original_hosts = self.hosts_file.read_bytes()

    def seed_messages(self, conversation_id, messages, actor="operator"):
        path = assistant._path(actor, conversation_id)
        data = assistant._read(path, actor)
        data["messages"] = [assistant._message(role, content) for role, content in messages]
        assistant._save(path, data)

    def query(self, content="What is the current patch version on source?", *, actor="operator", conversation_id=None):
        conversation_id = conversation_id or self.create(actor=actor)["id"]
        response = self.request(f"/api/assistant/conversations/{conversation_id}/messages",
                                method="POST", actor=actor, body={"content": content})
        return conversation_id, response

    def conversation(self, conversation_id, actor="operator"):
        response = self.request(f"/api/assistant/conversations/{conversation_id}", actor=actor)
        self.assertEqual(response["status"], 200, response)
        return response["body"]["conversation"]

    def finish_query(self, response):
        self.assertEqual(response["status"], 202, response)
        return self.wait_run(response["body"]["run_id"])

    @staticmethod
    def answer(conversation):
        return "\n".join(message["content"] for message in conversation["messages"]
                         if message["role"] == "assistant")

    def assert_no_mutation(self):
        for function in self.native.values():
            function.assert_not_called()
        self.steps["readiness-chain"].assert_not_called()
        self.steps["recovery-collect"].assert_not_called()

    def model_inventory_proposal(self, host_id="source"):
        self.model.side_effect = [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "check-current-inventory", "type": "function",
                "function": {"name": "check_live_inventory", "arguments": json.dumps({"host_id": host_id})}}]},
            {"role": "assistant", "content": "Review the live inventory action card.", "tool_calls": []},
        ]

    def test_live_model_selects_inventory_for_paraphrases_and_confirmation_returns_exact_receipt(self):
        evidence.write_evidence("source", "snapshot", self.snapshot("11111111"))
        for content in ("Is patch 39034528 installed on source?", "What is the current RU on source?",
                        "Verify the current patches on source"):
            with self.subTest(content=content):
                self.model_inventory_proposal()
                before = self.steps["discovery"].call_count
                conversation_id, response = self.query(content)
                model_record = self.finish_query(response)
                self.assertEqual((model_record.kind, model_record.status), ("assistant", "succeeded"))
                self.assertEqual(self.steps["discovery"].call_count, before)
                offered = self.model.call_args_list[-1].args[1]
                self.assertIn("check_live_inventory", {tool["function"]["name"] for tool in offered})
                prepared = self.conversation(conversation_id)
                self.assertEqual(len(prepared["actions"]), 1)
                action = prepared["actions"][0]
                self.assertEqual((action["tool"], action["state"], action["arguments"]),
                                 ("check_live_inventory", "pending", {"host_id": "source"}))
                self.assertNotIn("run_id", action)
                native = self.finish_query(self.execute(conversation_id, action, actor="operator"))
                self.assertEqual((native.kind, native.key, native.status),
                                 ("pipeline", "host:source:pipeline", "succeeded"))
                self.assertEqual(self.steps["discovery"].call_count, before + 1)
                body = self.steps["discovery"].call_args.args[2]
                self.assertIs(body["inventory_receipt"], True)
                self.assertEqual(body["expected_configuration_sha256"], live_inventory.configuration_digest(self.host))
                completed = self.conversation(conversation_id)
                self.assertEqual(completed["actions"][0]["state"], "completed")
                answer = completed["messages"][-1]["content"]
                for expected in ("88888888", native.run_id, "/fixture/current-home"):
                    self.assertIn(expected, answer)
                self.assertNotIn("11111111", answer)
                self.assertEqual(self.execute(conversation_id, action, actor="operator")["status"], 409)
                self.assertEqual(self.steps["discovery"].call_count, before + 1)
        self.assert_no_mutation()

    def test_live_model_cannot_choose_missing_ambiguous_or_different_user_target(self):
        self.add_target()
        for content in ("Is this patch installed?", "Is this patch installed on source and target?",
                        "Is this patch installed on unknown-host?", "Is this patch installed on target?"):
            with self.subTest(content=content):
                self.model_inventory_proposal("source")
                conversation_id, response = self.query(content)
                self.finish_query(response)
                self.assertEqual(self.conversation(conversation_id)["actions"], [])
                results = [json.loads(message["content"]) for message in self.model.call_args.args[0]
                           if message["role"] == "tool" and message.get("tool_call_id") == "check-current-inventory"]
                self.assertEqual(len(results), 1)
                self.assertIn("select one configured host", results[0]["error"])
        self.assert_no_native_calls()

    def test_live_newer_invalid_target_selection_cannot_revive_old_host_in_either_path(self):
        self.add_target()
        for latest in ("Check patches on unknown-host.", "We are investigating unknown-host.",
                       "Check patches on source and target.", "Check patches on source or unknown-host.",
                       "Check patches on unknown-host. Source was the earlier target."):
            for mode, question in (("automatic", "Show current patch inventory."),
                                   ("model", "Is patch 39034528 installed?")):
                with self.subTest(latest=latest, mode=mode):
                    conversation_id = self.create(actor="operator")["id"]
                    self.seed_messages(conversation_id, [("user", "We are investigating source."),
                        ("assistant", "Understood."), ("user", latest), ("assistant", "source")])
                    self.model.reset_mock()
                    if mode == "model":
                        self.model_inventory_proposal("source")
                    _, response = self.query(question, conversation_id=conversation_id)
                    self.finish_query(response)
                    conversation = self.conversation(conversation_id)
                    self.assertEqual(conversation["actions"], [])
                    if mode == "automatic":
                        self.model.assert_not_called()
                        self.assertIn("Which one configured host", self.answer(conversation))
                    else:
                        result = next(json.loads(message["content"]) for message in self.model.call_args.args[0]
                                      if message.get("tool_call_id") == "check-current-inventory")
                        self.assertIn("select one configured host", result["error"])
        self.assert_no_native_calls()

    def test_live_model_requires_operator_and_rechecks_permission_at_confirmation(self):
        self.model_inventory_proposal()
        conversation_id, response = self.query("Is patch 39034528 installed on source?", actor="viewer")
        self.finish_query(response)
        self.assertEqual(self.conversation(conversation_id, "viewer")["actions"], [])
        self.assertNotIn("check_live_inventory", {tool["function"]["name"] for tool in self.model.call_args.args[1]})
        self.model_inventory_proposal()
        conversation_id, response = self.query("Is patch 39034528 installed on source?")
        self.finish_query(response)
        action = self.conversation(conversation_id)["actions"][0]
        self.roles["operator"] = ["viewer"]
        self.write_principals()
        self.assertEqual(self.execute(conversation_id, action, actor="operator")["status"], 403)
        self.assert_no_native_calls()

    def test_live_model_failure_and_unbound_receipt_never_use_saved_inventory(self):
        evidence.write_evidence("source", "snapshot", self.snapshot("11111111"))
        for failure in ("collection", "wrong_run"):
            with self.subTest(failure=failure):
                self.model_inventory_proposal()
                conversation_id, response = self.query("Is patch 39034528 installed on source?")
                self.finish_query(response)
                action = self.conversation(conversation_id)["actions"][0]
                if failure == "collection":
                    self.steps["discovery"].side_effect = RuntimeError("Fixture collection failed")
                else:
                    self.steps["discovery"].side_effect = self.discover
                    self.receipt_transform = lambda receipt: {**receipt, "run_id": "0" * 12}
                self.finish_query(self.execute(conversation_id, action, actor="operator"))
                completed = self.conversation(conversation_id)
                self.assertEqual(completed["actions"][0]["state"], "failed")
                answer = completed["messages"][-1]["content"]
                self.assertIn("No cached inventory was used", answer)
                self.assertNotIn("11111111", answer)
                self.assertNotIn("88888888", answer)
                if failure == "wrong_run":
                    result = completed["actions"][0]["result"]
                    self.assertEqual(result["inventory_verification"], "rejected")
                    self.assertEqual(result["outcome"], {"status": "unverified"})
                    self.assertEqual(result["run_status"], "succeeded")
                    self.assertNotIn("88888888", json.dumps(completed["actions"][0]))
        self.assert_no_mutation()

    def test_live_generic_application_or_model_version_question_never_auto_reuses_database_target(self):
        for question in ("What version does this tool support?", "Which model version are you?",
                         "What patch versions does this application support?"):
            with self.subTest(question=question):
                conversation_id = self.create(actor="operator")["id"]
                self.seed_messages(conversation_id, [("user", "Check current patches on source")])
                _, response = self.query(question, conversation_id=conversation_id)
                self.finish_query(response)
                self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.assertEqual(self.model.call_count, 3)
        self.assert_no_native_calls()

    def test_live_operator_question_dispatches_exact_native_discovery_and_returns_receipt(self):
        conversation_id, response = self.query()
        record = self.finish_query(response)
        self.assertEqual((record.kind, record.key, record.status),
                         ("pipeline", "host:source:pipeline", "succeeded"))
        self.assertEqual(self.steps["discovery"].call_count, 1)
        args = self.steps["discovery"].call_args.args
        self.assertEqual(args[:2], ("source", self.host))
        self.assertEqual(args[2]["actor"], "operator")
        conversation = self.conversation(conversation_id)
        action = conversation["actions"][0]
        self.assertEqual((action["tool"], action["arguments"], action["origin"], action["state"]),
                         ("refresh_discovery", {"host_id": "source"}, "live_inventory_query", "completed"))
        self.assertEqual((action["confirmed_by"], action["run_id"]), ("operator", record.run_id))
        self.assertTrue(action["confirmed_at"])
        answer = self.answer(conversation)
        for expected in ("88888888", "/fixture/current-home", "ORCL", record.run_id, "12.2.0.1.51"):
            self.assertIn(expected, answer)
        self.assertIn(record.result["nodes"][0]["collected_at"], answer)
        self.assertFalse(conversation["busy"])
        self.model.assert_not_called()
        self.assert_no_mutation()

    def test_live_arbitrary_configured_hosts_report_changing_run_values_without_known_constants(self):
        """Changing the configured estate and collected facts changes the actual answer."""
        rng = random.Random(20260917)
        hosts = [{**self.host, "id": template.format(rng.getrandbits(32)),
                  "label": f"Generated estate {index}",
                  "ssh_alias": f"generated-{rng.getrandbits(32):08x}.invalid"}
                 for index, template in enumerate(("ledger-{:08x}", "warehouse_{:08x}db", "retail.{:08x}"))]
        self.hosts_file.write_text(json.dumps({"hosts": hosts}))
        self.original_hosts = self.hosts_file.read_bytes()
        patch_numbers = iter(rng.sample(range(10000000, 99999999), 21))
        database_versions = iter(rng.sample(range(4, 99), 6))
        opatch_versions = iter(rng.sample(range(30, 99), 6))
        observations = {}
        cache_patches = []
        for host in hosts:
            host_id = host["id"]
            stale_patch = str(next(patch_numbers))
            cache_patches.append(stale_patch)
            stale = self.snapshot(stale_patch)
            stale["collected_at"] = "2000-01-01T00:00:00Z"
            evidence.write_evidence(host_id, "snapshot", stale)
            observations[host_id] = []
            for _generation in range(2):
                snapshot = self.snapshot()
                home, database = snapshot["oracle_homes"][0], snapshot["databases"][0]
                nonce = f"{rng.getrandbits(48):012x}"
                home.update(path=f"/srv/oracle/{host_id}/home_{nonce}",
                    version=f"19.{next(database_versions)}.0.0.0",
                    opatch_version=f"12.2.0.1.{next(opatch_versions)}",
                    patches=[str(next(patch_numbers)) for _ in range(3)],
                    opatch_inventory_xml_sha256=f"{rng.getrandbits(256):064x}")
                database.update(db_unique_name=f"DB_{nonce.upper()}", oracle_home=home["path"])
                database["runtime"]["database_version"] = home["version"]
                observations[host_id].append(snapshot)
        pending = copy.deepcopy(observations)

        def generated_discovery(host_id, host, body):
            self.assertEqual(host, next(row for row in hosts if row["id"] == host_id))
            self.assertIs(body.get("inventory_receipt"), True)
            self.assertEqual(body.get("expected_configuration_sha256"), live_inventory.configuration_digest(host))
            run_id = pipeline_runner.current_run_id()
            started = live_inventory.utc_now()
            snapshot = pending[host_id].pop(0)
            snapshot["collected_at"] = live_inventory.utc_now()
            return live_inventory.build_receipt(host_id=host_id, host=host, run_id=run_id,
                started_at=started, completed_at=live_inventory.utc_now(), node_snapshots=[(host_id, snapshot)])

        self.steps["discovery"].side_effect = generated_discovery
        run_ids = set()
        for host in hosts:
            host_id, conversation_id = host["id"], None
            previous_facts = []
            for generation, snapshot in enumerate(observations[host_id]):
                with self.subTest(host_id=host_id, generation=generation):
                    # Exercise both exact configured identifiers and a natural
                    # database alias, without the source/target lab names.
                    spoken_host = (host_id[:-2] + " database") if host_id.endswith("db") else host_id
                    conversation_id, response = self.query(
                        f"Check the current patch inventory on {spoken_host}", conversation_id=conversation_id)
                    record = self.finish_query(response)
                    self.assertEqual((record.kind, record.key, record.status),
                                     ("pipeline", f"host:{host_id}:pipeline", "succeeded"))
                    self.assertNotIn(record.run_id, run_ids)
                    run_ids.add(record.run_id)
                    native_args = self.steps["discovery"].call_args.args
                    self.assertEqual(native_args[:2], (host_id, host))
                    conversation = self.conversation(conversation_id)
                    action = conversation["actions"][-1]
                    self.assertEqual((action["arguments"]["host_id"], action["run_id"], action["state"]),
                                     (host_id, record.run_id, "completed"))
                    self.assertEqual(record.result["host_id"], host_id)
                    self.assertEqual(record.result["run_id"], record.run_id)
                    self.assertEqual(record.result["configuration_sha256"], live_inventory.configuration_digest(host))
                    home, database = snapshot["oracle_homes"][0], snapshot["databases"][0]
                    current_facts = [home["path"], home["version"], home["opatch_version"],
                                     database["db_unique_name"], *home["patches"]]
                    latest_answer = conversation["messages"][-1]["content"]
                    for fact in [host_id, record.run_id, *current_facts]:
                        self.assertIn(fact, latest_answer)
                    for outdated in previous_facts + cache_patches:
                        self.assertNotIn(outdated, latest_answer)
                    previous_facts = current_facts
        self.assertEqual(self.steps["discovery"].call_count, 6)
        self.assertTrue(all(not values for values in pending.values()))
        self.model.assert_not_called()
        self.assert_no_mutation()

    def test_live_generated_host_failure_never_reuses_previous_success_or_saved_values(self):
        rng = random.Random(20260918)
        host = {**self.host, "id": f"billing-{rng.getrandbits(40):010x}db",
                "ssh_alias": f"new-{rng.getrandbits(32):08x}.invalid"}
        self.hosts_file.write_text(json.dumps({"hosts": [host]}))
        self.original_hosts = self.hosts_file.read_bytes()
        live_patch, stale_patch = [str(value) for value in rng.sample(range(10000000, 99999999), 2)]
        self.snapshot_transform = lambda snapshot: {**snapshot,
            "oracle_homes": [{**snapshot["oracle_homes"][0], "patches": [live_patch]}]}
        conversation_id, response = self.query(f"Check current patches on {host['id']}")
        first_record = self.finish_query(response)
        self.assertIn(live_patch, self.conversation(conversation_id)["messages"][-1]["content"])
        evidence.write_evidence(host["id"], "snapshot", self.snapshot(stale_patch))
        for outcome in (RuntimeError(f"Collection unavailable on {host['ssh_alias']}"), None):
            with self.subTest(outcome=type(outcome).__name__):
                self.steps["discovery"].side_effect = outcome
                self.steps["discovery"].return_value = None
                _, response = self.query(f"Check current patches on {host['id']}", conversation_id=conversation_id)
                record = self.finish_query(response)
                self.assertNotEqual(record.run_id, first_record.run_id)
                self.assertEqual(record.key, f"host:{host['id']}:pipeline")
                latest = self.conversation(conversation_id)
                self.assertEqual(latest["actions"][-1]["run_id"], record.run_id)
                self.assertNotEqual(latest["actions"][-1]["state"], "completed")
                answer = latest["messages"][-1]["content"]
                self.assertIn(host["id"], answer)
                self.assertIn(record.run_id, answer)
                self.assertNotIn(live_patch, answer)
                self.assertNotIn(stale_patch, answer)
                self.assertEqual(self.steps["discovery"].call_args.args[:2], (host["id"], host))
        self.assertEqual(self.steps["discovery"].call_count, 3)
        self.model.assert_not_called()
        self.assert_no_mutation()

    def test_live_viewer_and_requester_receive_operator_instruction_without_native_or_model(self):
        for actor in ("viewer", "requester"):
            with self.subTest(actor=actor):
                conversation_id, response = self.query(actor=actor)
                record = self.finish_query(response)
                self.assertEqual((record.kind, record.status), ("assistant", "succeeded"))
                conversation = self.conversation(conversation_id, actor)
                self.assertIn("operator", self.answer(conversation).lower())
                self.assertEqual(conversation["actions"], [])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_missing_or_ambiguous_host_asks_for_selection_without_dispatch(self):
        self.add_target()
        for content in ("What is the current patch version?", "Check patch versions on source and target"):
            with self.subTest(content=content):
                conversation_id, response = self.query(content)
                self.finish_query(response)
                conversation = self.conversation(conversation_id)
                self.assertEqual(conversation["actions"], [])
                self.assertTrue(self.answer(conversation))
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_bare_host_selection_continues_only_the_pending_inventory_question(self):
        self.add_target()
        conversation_id, response = self.query("What is the current patch version?")
        self.finish_query(response)
        self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.steps["discovery"].assert_not_called()
        _, response = self.query("source", conversation_id=conversation_id)
        record = self.finish_query(response)
        self.assertEqual(record.kind, "pipeline")
        self.assertIn("88888888", self.answer(self.conversation(conversation_id)))
        self.steps["discovery"].assert_called_once()
        self.assertEqual(self.steps["discovery"].call_args.args[0], "source")
        self.model.assert_not_called()

    def test_live_unrelated_reply_cancels_pending_host_selection(self):
        conversation_id, response = self.query("What is the current patch version?")
        self.finish_query(response)
        _, response = self.query("Tell me about the approval workflow instead.", conversation_id=conversation_id)
        self.finish_query(response)
        _, response = self.query("source", conversation_id=conversation_id)
        self.finish_query(response)
        self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.assert_no_native_calls()

    def test_live_inventory_query_works_without_a_configured_language_model(self):
        self.config.return_value = {"enabled": False, "configured": False, "reason": "Fixture model disabled"}
        conversation_id, response = self.query()
        record = self.finish_query(response)
        self.assertEqual((record.kind, record.status), ("pipeline", "succeeded"))
        self.assertIn("88888888", self.answer(self.conversation(conversation_id)))
        self.steps["discovery"].assert_called_once()
        self.model.assert_not_called()

    def test_live_followup_uses_previous_user_host_never_assistant_host_claim(self):
        self.add_target()
        conversation_id = self.create(actor="operator")["id"]
        self.seed_messages(conversation_id, [("user", "We are investigating source."),
            ("assistant", "Target is the database selected for subsequent work.")])
        _, response = self.query("What is the current patch version?", conversation_id=conversation_id)
        self.finish_query(response)
        self.conversation(conversation_id)
        self.assertEqual(self.steps["discovery"].call_args.args[0], "source")
        self.model.assert_not_called()
        self.assert_no_mutation()

    def test_live_previous_user_negation_cannot_select_a_host_for_followup(self):
        self.add_target()
        conversation_id = self.create(actor="operator")["id"]
        self.seed_messages(conversation_id, [("user", "We are investigating source."),
            ("assistant", "Understood."), ("user", "Do not connect to source."),
            ("assistant", "I will not connect to source.")])
        _, response = self.query("What is the current patch version?", conversation_id=conversation_id)
        self.finish_query(response)
        self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_assistant_only_host_mention_is_not_authority_to_choose_a_host(self):
        self.add_target()
        conversation_id = self.create(actor="operator")["id"]
        self.seed_messages(conversation_id, [("user", "Hello"), ("assistant", "Let us inspect source next.")])
        _, response = self.query("What is the current patch version?", conversation_id=conversation_id)
        self.finish_query(response)
        self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_explicit_latest_host_overrides_previous_user_host(self):
        self.add_target()
        conversation_id = self.create(actor="operator")["id"]
        self.seed_messages(conversation_id, [("user", "We are investigating source."), ("assistant", "Understood.")])
        _, response = self.query("Check the current patch version on target", conversation_id=conversation_id)
        self.finish_query(response)
        self.assertIn("88888888", self.answer(self.conversation(conversation_id)))
        self.assertEqual(self.steps["discovery"].call_args.args[0], "target")
        self.model.assert_not_called()

    def test_live_explicit_unknown_host_does_not_fall_back_to_previous_user_target(self):
        conversation_id = self.create(actor="operator")["id"]
        self.seed_messages(conversation_id, [("user", "We are investigating source."), ("assistant", "Understood.")])
        _, response = self.query("What is the current patch version on unknown-host?", conversation_id=conversation_id)
        self.finish_query(response)
        self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_unknown_punctuated_identifier_cannot_select_its_configured_prefix_or_suffix(self):
        configured = self.host["id"]
        unknown_hosts = [configured + suffix for suffix in ("-east", ".prod", "_retired", ":1521", "..extra")]
        unknown_hosts.append("retired-" + configured)
        for host_id in unknown_hosts:
            with self.subTest(host_id=host_id):
                conversation_id = self.create(actor="operator")["id"]
                self.seed_messages(conversation_id, [("user", f"We are investigating {configured}."),
                                                     ("assistant", "Understood.")])
                _, response = self.query(f"Check current patches on {host_id}", conversation_id=conversation_id)
                record = self.finish_query(response)
                self.assertEqual(record.kind, "assistant")
                conversation = self.conversation(conversation_id)
                self.assertEqual(conversation["actions"], [])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_explicit_unknown_target_is_not_replaced_by_an_incidental_known_host_mention(self):
        for content in (
            "What is the current patch version on unknown-host? Source was the earlier target.",
            "Check patch version on unknown-host, source was the earlier target",
        ):
            with self.subTest(content=content):
                conversation_id, response = self.query(content)
                self.finish_query(response)
                self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_saved_requests_and_mutation_requests_do_not_autolaunch_discovery(self):
        for content in ("Show saved patch inventory for source", "Show cached patch version for source",
                        "What is the recorded patch version on source?", "Show historical patch inventory for source",
                        "Check patch version on source without refreshing", "Check patch version on source, no refresh",
                        "Check patches on source and apply patch 39034528", "Execute the patch plan on source"):
            with self.subTest(content=content):
                conversation_id, response = self.query(content)
                self.finish_query(response)
                self.assertEqual(self.conversation(conversation_id)["actions"], [])
        self.assert_no_native_calls()

    def test_live_plan_identifiers_containing_patch_preserve_model_inspection_and_pending_proposals(self):
        generated_plan_id = f"customer-{random.Random(7139).randrange(100000, 999999)}-patch-cycle"
        cases = (
            ("assistant-native-patch", "Inspect assistant-native-patch and propose executing its remaining tasks."),
            ("assistant-native-patch", "Inspect assistant-native-patch again and propose the current remaining tasks."),
            (generated_plan_id, f"Inspect {generated_plan_id} and propose executing its remaining tasks."),
        )
        for plan_id, content in cases:
            with self.subTest(plan_id=plan_id, content=content):
                self.saved_plan["plan_id"] = plan_id
                saved_before = copy.deepcopy(self.saved_plan)
                self.model.reset_mock()
                self.model.side_effect = [
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "inspect-current-plan", "type": "function",
                        "function": {"name": "inspect_plan", "arguments": json.dumps({"plan_id": plan_id})}}]},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "prepare-plan-proposal", "type": "function",
                        "function": {"name": "execute_plan", "arguments": json.dumps({"plan_id": plan_id})}}]},
                    {"role": "assistant", "content": "Inspected the saved plan and prepared its execution for review.",
                     "tool_calls": []},
                ]
                conversation_id, response = self.query(content)
                record = self.finish_query(response)
                self.assertEqual((record.kind, record.status), ("assistant", "succeeded"))
                self.assertEqual(self.model.call_count, 3)
                conversation = self.conversation(conversation_id)
                self.assertIn("prepared its execution for review", self.answer(conversation))
                self.assertEqual(len(conversation["actions"]), 1)
                action = conversation["actions"][0]
                self.assertEqual((action["tool"], action["arguments"], action["state"]),
                                 ("execute_plan", {"plan_id": plan_id}, "pending"))
                self.assertNotIn("run_id", action)
                self.assertNotEqual(action.get("origin"), "live_inventory_query")
                final_wire = self.model.call_args.args[0]
                results = {message["tool_call_id"]: json.loads(message["content"])
                           for message in final_wire if message["role"] == "tool"}
                self.assertEqual(results["inspect-current-plan"]["plan_id"], plan_id)
                self.assertEqual(results["prepare-plan-proposal"]["state"], "pending_human_confirmation")
                self.assertEqual(self.saved_plan, saved_before)
                self.assert_no_native_calls()

    def test_live_negative_or_hypothetical_inventory_mentions_never_dispatch(self):
        requests = (
            "Do not check the patch version on source.",
            "Don't refresh; just show patch inventory on source.",
            "Don’t check the patch version on source.",
            "DON’T check the patch version on source.",
            "Don‘t check the patch version on source.",
            "Show patch version on source, but don’t connect.",
            "Show patch version on source without connecting.",
            "What would happen if I check the patch version on source?",
            "If I wanted to check patch inventory on source, what would run?",
            "Do not run a live check; tell me the patch version for source.",
            "Without executing anything, show patch version on source.",
            "Show patch version on source without making an SSH connection.",
        )
        for content in requests:
            with self.subTest(content=content):
                conversation_id, response = self.query(content)
                self.finish_query(response)
                self.assertEqual(self.conversation(conversation_id)["actions"], [])
                self.steps["discovery"].assert_not_called()
        self.assert_no_native_calls()

    def test_live_answer_ignores_stale_and_later_replaced_saved_inventory(self):
        stale = self.snapshot("11111111")
        stale["collected_at"] = "2000-01-01T00:00:00Z"
        evidence.write_evidence("source", "snapshot", stale)
        conversation_id, response = self.query()
        self.finish_query(response)
        evidence.write_evidence("source", "snapshot", self.snapshot("22222222"))
        answer = self.answer(self.conversation(conversation_id))
        self.assertIn("88888888", answer)
        self.assertNotIn("11111111", answer)
        self.assertNotIn("22222222", answer)
        self.model.assert_not_called()

    def test_live_repeated_get_appends_answer_once_and_never_redispatches(self):
        conversation_id, response = self.query()
        self.finish_query(response)
        first = self.conversation(conversation_id)
        for _ in range(3):
            again = self.conversation(conversation_id)
            self.assertEqual(again["messages"], first["messages"])
            self.assertEqual(again["actions"], first["actions"])
        self.assertEqual(sum(message["role"] == "assistant" for message in first["messages"]), 1)
        self.steps["discovery"].assert_called_once()
        self.model.assert_not_called()

    def test_live_in_progress_query_blocks_duplicate_posts_and_does_not_publish_cached_answer(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow_discovery(host_id, host, body):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Fixture worker was not released")
            return self.discover(host_id, host, body)
        self.steps["discovery"].side_effect = slow_discovery
        conversation_id, response = self.query()
        self.assertEqual(response["status"], 202, response)
        self.assertTrue(started.wait(1))
        pending = self.conversation(conversation_id)
        self.assertTrue(pending["busy"])
        self.assertEqual(self.answer(pending), "")
        _, duplicate = self.query(conversation_id=conversation_id)
        self.assertEqual(duplicate["status"], 409, duplicate)
        self.steps["discovery"].assert_called_once()
        release.set()
        self.finish_query(response)
        self.assertIn("88888888", self.answer(self.conversation(conversation_id)))
        self.model.assert_not_called()

    def test_live_failed_and_missing_receipt_never_fall_back_to_saved_patches(self):
        evidence.write_evidence("source", "snapshot", self.snapshot("11111111"))
        for failure in (RuntimeError("Fixture SSH unavailable"), None):
            with self.subTest(failure=type(failure).__name__):
                if failure:
                    self.steps["discovery"].side_effect = failure
                else:
                    self.steps["discovery"].side_effect = None
                    self.steps["discovery"].return_value = {"status": "succeeded"}
                conversation_id, response = self.query()
                self.finish_query(response)
                conversation = self.conversation(conversation_id)
                self.assertNotEqual(conversation["actions"][0]["state"], "completed")
                answer = self.answer(conversation)
                self.assertTrue(answer)
                self.assertNotIn("11111111", answer)
                self.assertNotIn("88888888", answer)
        self.model.assert_not_called()
        self.assert_no_mutation()

    def test_live_unknown_run_is_reported_once_without_claiming_current_inventory(self):
        evidence.write_evidence("source", "snapshot", self.snapshot("11111111"))
        conversation_id, response = self.query()
        record = self.finish_query(response)
        record.status = "unknown"
        record.result = None
        record._persist()
        first = self.conversation(conversation_id)
        self.assertEqual(first["actions"][0]["state"], "unknown")
        self.assertTrue(self.answer(first))
        self.assertNotIn("11111111", self.answer(first))
        self.assertNotIn("88888888", self.answer(first))
        self.assertEqual(self.conversation(conversation_id)["messages"], first["messages"])
        self.steps["discovery"].assert_called_once()
        self.model.assert_not_called()

    def test_live_unknown_plan_without_host_argument_blocks_a_new_live_check(self):
        conversation_id = self.create(actor="operator")["id"]
        path = assistant._path("operator", conversation_id)
        data = assistant._read(path, "operator")
        data["actions"].append({"id": "a" * 24, "tool": "execute_plan",
            "arguments": {"plan_id": "unresolved-fixture-plan"}, "state": "unknown",
            "run_id": "b" * 12, "confirmed_at": live_inventory.utc_now()})
        assistant._save(path, data)
        _, response = self.query(conversation_id=conversation_id)
        record = self.finish_query(response)
        self.assertEqual(record.kind, "assistant")
        conversation = self.conversation(conversation_id)
        self.assertEqual(len(conversation["actions"]), 1)
        self.assertEqual(conversation["actions"][0]["state"], "unknown")
        self.assertIn("reconcile", conversation["messages"][-1]["content"].lower())
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_reconciled_unknown_run_publishes_terminal_receipt_once(self):
        conversation_id, response = self.query()
        record = self.finish_query(response)
        receipt = copy.deepcopy(record.result)
        record.status, record.result = "unknown", None
        record._persist()
        unknown = self.conversation(conversation_id)
        self.assertEqual(unknown["actions"][0]["state"], "unknown")
        self.assertNotIn("88888888", self.answer(unknown))
        record.status, record.result = "succeeded", receipt
        record._persist()
        completed = self.conversation(conversation_id)
        self.assertEqual(completed["actions"][0]["state"], "completed")
        self.assertEqual(len(completed["messages"]), len(unknown["messages"]) + 1)
        self.assertIn("88888888", completed["messages"][-1]["content"])
        for _ in range(3):
            self.assertEqual(self.conversation(conversation_id)["messages"], completed["messages"])
        self.steps["discovery"].assert_called_once()
        self.model.assert_not_called()

    def test_live_wrong_receipt_association_integrity_or_start_time_cannot_answer(self):
        changes = ({"host_id": "other"}, {"run_id": "a" * 12}, {"configuration_sha256": "b" * 64},
                   {"started_at": "2000-01-01T00:00:00+00:00"}, {"receipt_sha256": "0" * 64})
        for changeset in changes:
            with self.subTest(changes=changeset):
                def tamper(receipt):
                    changed = copy.deepcopy(receipt)
                    changed.update(changeset)
                    if "receipt_sha256" not in changeset:
                        payload = {key: value for key, value in changed.items() if key != "receipt_sha256"}
                        changed["receipt_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                    return changed
                self.receipt_transform = tamper
                conversation_id, response = self.query()
                self.finish_query(response)
                conversation = self.conversation(conversation_id)
                self.assertNotEqual(conversation["actions"][0]["state"], "completed")
                self.assertNotIn("88888888", self.answer(conversation))
                self.assertTrue(self.answer(conversation))
                diagnostics = conversation["actions"][0]["result"]
                self.assertEqual(diagnostics["inventory_verification"], "rejected")
                self.assertEqual(diagnostics["outcome"], {"status": "unverified"})
                self.assertNotIn("88888888", json.dumps(conversation["actions"][0]))
        self.model.assert_not_called()

    def test_live_stale_or_unproven_collector_cannot_report_installed_patch_ids(self):
        for changes in ({"collected_at": "2000-01-01T00:00:00Z"}, {"collector": {"name": "untrusted"}}):
            with self.subTest(changes=changes):
                self.snapshot_transform = lambda snapshot: {**snapshot, **changes}
                conversation_id, response = self.query()
                self.finish_query(response)
                answer = self.answer(self.conversation(conversation_id))
                self.assertNotIn("88888888", answer)
                self.assertIn("unverified", answer.lower())
        self.model.assert_not_called()

    def test_live_native_dispatch_rechecks_roles_after_chat_authorization(self):
        original_submit = server.Handler._submit_assistant_action
        def revoke_then_submit(handler, path, body):
            self.roles["operator"] = ["viewer"]
            self.write_principals()
            return original_submit(handler, path, body)
        with patch.object(server.Handler, "_submit_assistant_action", revoke_then_submit):
            conversation_id, response = self.query()
        self.assertEqual(response["status"], 403, response)
        conversation = self.conversation(conversation_id)
        self.assertFalse(conversation["busy"])
        self.assertFalse(pipeline_runner.RUNS)
        self.model.assert_not_called()
        self.assert_no_native_calls()

    def test_live_native_dispatch_rejects_host_configuration_changed_after_chat_selection(self):
        original_submit = server.Handler._submit_assistant_action
        def change_host_then_submit(handler, path, body):
            changed = {**self.host, "ssh_alias": "different-fixture.invalid"}
            self.hosts_file.write_text(json.dumps({"hosts": [changed]}))
            self.original_hosts = self.hosts_file.read_bytes()
            return original_submit(handler, path, body)
        with patch.object(server.Handler, "_submit_assistant_action", change_host_then_submit):
            conversation_id, response = self.query()
        self.assertEqual(response["status"], 409, response)
        conversation = self.conversation(conversation_id)
        self.assertFalse(conversation["busy"])
        self.assertEqual(conversation["actions"][0]["state"], "failed")
        self.assertFalse(pipeline_runner.RUNS)
        self.model.assert_not_called()
        self.assert_no_native_calls()


def load_tests(loader, _tests, _pattern):
    """Reuse fixture helpers without running its existing API tests a second time."""
    return unittest.TestSuite(LiveAssistantApiTests(name)
        for name in loader.getTestCaseNames(LiveAssistantApiTests) if name.startswith("test_live_"))


if __name__ == "__main__":
    unittest.main()
