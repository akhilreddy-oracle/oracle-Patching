#!/usr/bin/env python3
"""Assessment harness checks against a deterministic loopback HTTP simulator.

These tests do not install or evaluate a real model or exercise Oracle tools.
"""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_acceptance as assessment


def response_for(case):
    if "expected_tool" in case:
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "synthetic-response",
            "type": "function", "function": {"name": case["expected_tool"],
            "arguments": json.dumps(case["expected_arguments"])}}]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": json.dumps(case["expected_text"])}
        finish = "stop"
    return {"choices": [{"message": message, "finish_reason": finish}]}


class ModelAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opu-model-assessment-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "assistant-config.json"
        self.requests = []
        self.replies = [response_for(case) for case in assessment.scenarios()]
        self.after_request = None
        self.http_status = 200
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                fixture.requests.append({"path": self.path,
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                    "authorization": self.headers.get("Authorization")})
                index = len(fixture.requests) - 1
                payload = json.dumps(fixture.replies[index]).encode()
                if fixture.after_request:
                    fixture.after_request()
                self.send_response(fixture.http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.settings = {"enabled": True, "model": "deterministic-assessment-simulator",
                         "base_url": f"http://127.0.0.1:{self.server.server_port}/v1", "timeout_seconds": 5}
        self.save()
        self.enterContext(patch.dict(os.environ, {"OPU_ASSISTANT_CONFIG": str(self.config)}))
        # Fail immediately if this diagnostic begins invoking native workflows.
        for module, name in ((assessment.capabilities, "read"), (assessment.capabilities, "binding"),
                             (assessment.assistant, "send"), (assessment.assistant, "action")):
            self.enterContext(patch.object(module, name, side_effect=AssertionError("Native tool invoked")))

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def save(self):
        self.config.write_text(json.dumps(self.settings)); self.config.chmod(0o600)

    def run_assessment(self, filename="receipt.json"):
        output = self.root / filename
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = assessment.main(["--output", str(output), "--deterministic-simulator"])
        return code, json.loads(output.read_text()), stdout.getvalue() + stderr.getvalue()

    def test_actual_transport_and_exact_typed_proposals_are_read_only_and_honestly_classified(self):
        initial = set(self.root.iterdir())
        before = self.config.read_bytes()
        code, receipt, output = self.run_assessment()
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["classification"], "model-protocol-smoke-assessment")
        self.assertEqual(receipt["endpoint_kind"], "deterministic-simulator")
        self.assertFalse(receipt["inference_implementation_verified"])
        self.assertEqual(receipt["native_actions_executed"], 0)
        self.assertEqual(len(receipt["scenarios"]), len(assessment.scenarios()))
        self.assertTrue(all(row["outcome"] == "pass" and row["duration_seconds"] >= 0 for row in receipt["scenarios"]))
        self.assertEqual(len(self.requests), len(assessment.scenarios()))
        self.assertTrue(all(request["path"] == "/v1/chat/completions" for request in self.requests))
        self.assertTrue(all(request["body"]["model"] == self.settings["model"] for request in self.requests))
        self.assertTrue(all(request["body"]["temperature"] == 0 for request in self.requests))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()) - initial, {self.root / "receipt.json"})
        self.assertEqual((self.root / "receipt.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt["record_sha256"], assessment.digest({k: v for k, v in receipt.items() if k != "record_sha256"}))
        self.assertEqual(receipt["configuration_sha256"], assessment.digest(receipt["configuration"]))
        self.assertEqual(receipt["source"]["sha256"], assessment.source_snapshot()["sha256"])
        self.assertIn("no native actions", output)

    def test_missing_disabled_unsafe_or_nonloopback_config_never_connects(self):
        configs = [None, {"enabled": False}, {"enabled": True, "model": "model:cloud"},
            {"enabled": True, "model": "synthetic", "base_url": "https://10.0.0.2/v1", "allow_private_endpoint": True},
            {"enabled": True, "model": "synthetic", "base_url": "https://internal.example/v1", "allow_private_endpoint": True},
            {"enabled": True, "model": "synthetic", "base_url": "http://localhost:11434/v1", "allow_private_endpoint": True},
            {"enabled": True, "model": "synthetic", "base_url": "http://localhost:11434/v1", "allow_insecure_private": True}]
        for index, config in enumerate(configs):
            with self.subTest(config=config):
                if config is None:
                    self.config.unlink()
                else:
                    self.settings = config; self.save()
                code, receipt, _ = self.run_assessment(f"blocked-{index}.json")
                self.assertEqual(code, 1)
                self.assertEqual(receipt["status"], "blocked")
                self.assertEqual(receipt["scenarios"], [])
        self.assertEqual(self.requests, [])

    def test_configuration_change_stops_before_a_second_request_or_new_endpoint(self):
        def change():
            self.settings.update(base_url="https://10.0.0.2/v1", allow_private_endpoint=True)
            self.save()
        self.after_request = change
        code, receipt, _ = self.run_assessment()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(receipt["scenarios"][-1]["outcome"], "error")
        self.assertFalse(receipt["bindings_unchanged"])

    def test_wrong_patch_database_plan_host_or_extra_actor_cannot_pass(self):
        for index, changes in enumerate(({"patch_id": "99999999"}, {"database": "OTHERDB"},
                {"plan_id": "other-plan"}, {"host_id": "synthetic-other-host"}, {"actor": "root"})):
            with self.subTest(changes=changes):
                self.requests.clear()
                self.replies = [response_for(case) for case in assessment.scenarios()]
                self.replies[1]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps({**assessment.PLAN, **changes})
                code, receipt, _ = self.run_assessment(f"wrong-scope-{index}.json")
                self.assertEqual(code, 1)
                self.assertEqual(receipt["scenarios"][1]["outcome"], "fail")

    def test_proposal_claim_without_a_function_call_still_fails_with_policy_on_the_wire(self):
        # Reproduce the real local model's failure: correct inputs and confident
        # preparation prose are not a controller-created proposal. Exercise the
        # production HTTP encoder/parser, not only evaluate() in isolation.
        for index in (1, 5):
            self.replies[index] = {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "status": "prepared for review", **assessment.PLAN})}}]}
        code, receipt, _ = self.run_assessment()
        self.assertEqual(code, 1)
        for index in (1, 5):
            self.assertEqual(receipt["scenarios"][index]["reason"], "expected_one_typed_call")
            request = self.requests[index]["body"]
            system = request["messages"][0]
            self.assertEqual(system, {"role": "system",
                "content": assessment.assistant.SYSTEM + assessment.PROTOCOL})
            self.assertEqual(sum(row["role"] == "system" for row in request["messages"]), 1)
            policy = " ".join(system["content"].split())
            self.assertRegex(policy, r"issue the corresponding function call")
            self.assertRegex(policy, r"Never claim a proposal is prepared or created until the tool returns")
            self.assertIn("proposal_id", policy)
            proposal = next(tool["function"] for tool in request["tools"]
                            if tool["function"]["name"] == "create_patch_plan")
            self.assertEqual(set(proposal["parameters"]["required"]), set(assessment.PLAN))
            self.assertFalse(proposal["parameters"]["additionalProperties"])

    def test_literal_refusal_instructions_do_not_relax_exact_status_and_reason(self):
        replacements = (
            {"status": "missing_evidence", "reason": "No saved evidence available."},
            {"status": "blocked", "reason": "An unresolved run prevents execution."},
            {"status": "blocked", "reason": "Cannot waive checks or assume identity."},
        )
        for index, text in enumerate(replacements, 2):
            self.replies[index]["choices"][0]["message"]["content"] = json.dumps(text)
        code, receipt, _ = self.run_assessment()
        self.assertEqual(code, 1)
        for index in (2, 3, 4):
            self.assertEqual(receipt["scenarios"][index]["reason"], "incorrect_uncertainty_or_refusal")
            text = self.requests[index]["body"]["messages"][0]["content"]
            required = assessment.scenarios()[index]["expected_text"]
            self.assertIn(json.dumps(required, separators=(",", ":")), text)

    def test_known_host_inspection_cannot_substitute_an_estate_listing(self):
        function = self.replies[0]["choices"][0]["message"]["tool_calls"][0]["function"]
        function.update(name="list_estate", arguments="{}")
        code, receipt, _ = self.run_assessment()
        self.assertEqual(code, 1)
        self.assertEqual(receipt["scenarios"][0]["reason"], "wrong_tool_or_synthetic_scope")
        self.assertEqual(assessment.scenarios()[0]["expected_arguments"], {"host_id": assessment.HOST})

    def test_missing_evidence_and_unknown_run_must_not_return_a_proposal(self):
        for index in (2, 3, 4):
            with self.subTest(case=index):
                self.requests.clear()
                self.replies = [response_for(case) for case in assessment.scenarios()]
                self.replies[index] = response_for(assessment.scenarios()[1])
                code, receipt, _ = self.run_assessment(f"unsafe-proposal-{index}.json")
                self.assertEqual(code, 1)
                self.assertEqual(receipt["scenarios"][index]["reason"], "unexpected_call_when_blocked_or_missing_evidence")

    def test_malformed_or_unavailable_model_response_stops_without_fallback(self):
        for index, response in enumerate(({"choices": []}, {"sensitive": "must-never-enter-receipt"})):
            self.requests.clear(); self.replies[0] = response
            code, receipt, output = self.run_assessment(f"malformed-{index}.json")
            self.assertEqual(code, 1)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(receipt["scenarios"][0]["outcome"], "error")
            self.assertNotIn("must-never-enter-receipt", json.dumps(receipt) + output)
        self.requests.clear(); self.http_status = 404
        code, receipt, _ = self.run_assessment("unavailable-model.json")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)

    def test_provider_credential_and_model_text_are_never_written_to_receipt_or_logs(self):
        self.settings["api_key_env"] = "SYNTHETIC_MODEL_CREDENTIAL"
        self.save()
        self.replies[2]["choices"][0]["message"]["content"] = "secret-response-do-not-log"
        with patch.dict(os.environ, {"SYNTHETIC_MODEL_CREDENTIAL": "secret-credential-do-not-log"}):
            _, receipt, output = self.run_assessment()
        self.assertTrue(receipt["configuration"]["credential_configured"])
        self.assertEqual(self.requests[0]["authorization"], "Bearer secret-credential-do-not-log")
        rendered = json.dumps(receipt) + output
        for secret in ("SYNTHETIC_MODEL_CREDENTIAL", "secret-credential-do-not-log", "secret-response-do-not-log", str(self.config)):
            self.assertNotIn(secret, rendered)

    def test_source_change_cannot_create_a_passed_receipt(self):
        snapshot = assessment.source_snapshot()
        with patch.object(assessment, "source_snapshot", side_effect=[snapshot, {"sha256": "changed"}]):
            code, receipt, _ = self.run_assessment()
        self.assertEqual(code, 1)
        self.assertFalse(receipt["bindings_unchanged"])

    def test_exclusive_output_rejects_existing_files_symlinks_and_unsafe_parent_before_network(self):
        existing = self.root / "existing.json"
        existing.write_text("keep")
        link = self.root / "link.json"; link.symlink_to(existing)
        unsafe = self.root / "unsafe"; unsafe.mkdir(); unsafe.chmod(0o777)
        for path in (existing, link, unsafe / "receipt.json"):
            with self.subTest(path=path), redirect_stderr(io.StringIO()):
                self.assertEqual(assessment.main(["--output", str(path)]), 2)
        self.assertEqual(existing.read_text(), "keep")
        self.assertEqual(self.requests, [])

    def test_public_config_snapshot_guard_remains_optional_and_rejects_changes_before_transport(self):
        config = assessment.local_llm.load_config()
        self.assertEqual(config["endpoint"]["host"], "127.0.0.1")
        self.settings["model"] = "changed-model"; self.save()
        with self.assertRaises(assessment.local_llm.LLMError):
            assessment.local_llm.complete(assessment.scenarios()[0]["messages"],
                assessment.capabilities.definitions({"read"}), expected_config=config)
        self.assertEqual(self.requests, [])
        response = assessment.local_llm.complete(assessment.scenarios()[0]["messages"],
            assessment.capabilities.definitions({"read"}))
        self.assertEqual(response["tool_calls"][0]["function"]["name"], "inspect_host")
        self.assertEqual(self.requests[0]["body"]["model"], "changed-model")


if __name__ == "__main__":
    unittest.main()
