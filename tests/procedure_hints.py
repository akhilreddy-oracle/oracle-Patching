"""Synthetic README hint and custody regressions; no SSH or Oracle media."""
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import procedure_hints
import remote

SENTENCE = "You must use the OPatch utility version 12.2.0.1.49 or later to apply this patch."
HOST = {"ssh_alias": "fixture-host", "sudo": True}


def manifest(payload, readme="README.html"):
    return {"artifact": {
        "status": "ready_for_catalog", "path": "/stage/12345678", "sha256": "a" * 64,
        "readme_files": [{"path": readme, "sha256": hashlib.sha256(payload).hexdigest()}],
    }}


class ProcedureHints(unittest.TestCase):
    def hints(self, payload=SENTENCE.encode(), *, document=None, selection="", host=None):
        with patch.object(procedure_hints.evidence, "read_evidence", return_value=document or manifest(payload)) as read, \
                patch.object(procedure_hints.remote, "pull_file", return_value=payload) as pull:
            result = procedure_hints.get_hints("host1", HOST if host is None else host, selection)
            read.assert_called_once_with("host1", "artifact")
            return result, pull

    def test_explicit_requirement_is_bound_to_inspected_bytes_and_bounded_read(self):
        result, pull = self.hints()
        self.assertEqual(result, {
            "artifact_sha256": "a" * 64, "readme_identifier": "README.html",
            "readme_sha256": hashlib.sha256(SENTENCE.encode()).hexdigest(),
            "required_opatch_version": "12.2.0.1.49", "evidence": SENTENCE, "warnings": [],
        })
        pull.assert_called_once_with("fixture-host", "/stage/12345678/README.html", timeout=30,
                                     sudo=True, max_bytes=2 * 1024 * 1024)
        self.assertFalse(any("rollback" in key for key in result))

    def test_markup_splits_entities_and_line_wrapping_preserve_requirement(self):
        payload = ("<html><style>OPatch 99.0.0</style><script>" + SENTENCE.replace("49", "99")
                   + "</script><h3>OPatch Utility</h3><p>You must use the OP<b>at</b>ch utility\n"
                   "version <code>12.2.0.1.<span>49</span></code> or&nbsp;later to apply this patch.</p></html>").encode()
        result, _ = self.hints(payload)
        self.assertEqual(result["required_opatch_version"], "12.2.0.1.49")
        self.assertEqual(result["evidence"], SENTENCE)

    def test_declared_windows_1252_and_utf8_bom(self):
        for payload in (("<meta http-equiv='Content-Type' content='text/html; charset=windows-1252'>"
                         "<p>Registered \xae product.</p><p>" + SENTENCE + "</p>").encode("cp1252"),
                        b"\xef\xbb\xbf<p>" + SENTENCE.encode() + b"</p>"):
            with self.subTest(payload=payload[:20]):
                result, _ = self.hints(payload)
                self.assertEqual(result["required_opatch_version"], "12.2.0.1.49")

    def test_supported_explicit_minimum_forms(self):
        for sentence in (
            "The minimum required OPatch version is 12.2.0.1.49.",
            "Minimum OPatch version: 12.2.0.1.49.",
            "OPatch version 12.2.0.1.49 or higher is required to apply this patch.",
            "Use OPatch version 12.2.0.1.49 or later to install this patch.",
        ):
            with self.subTest(sentence=sentence):
                result, _ = self.hints(sentence.encode())
                self.assertEqual(result["required_opatch_version"], "12.2.0.1.49")

    def test_missing_unrelated_example_conditional_and_rollback_text_are_not_requirements(self):
        for sentence in (
            "OPatch version: 12.2.0.1.49", "Installed OPatch version is 12.2.0.1.49.",
            "Oracle Database version 19.0.0.0.0. Download the latest OPatch.",
            "For example, " + SENTENCE[0].lower() + SENTENCE[1:],
            "If you use an older database, " + SENTENCE[0].lower() + SENTENCE[1:],
            "You must use the OPatch utility version 12.2.0.1.49 or later to rollback this patch.",
            "<pre>" + SENTENCE + "</pre>", "<samp>" + SENTENCE + "</samp>",
            "<script>" + SENTENCE + "</script>", "<style>" + SENTENCE + "</style>",
            "You must use OPatch version 12.2.0.1.49.9 or later to apply this patch.",
            "You must use OPatch version 12.2.0.1.49 or 12.2.0.1.50 to apply this patch.",
        ):
            with self.subTest(sentence=sentence):
                result, _ = self.hints(sentence.encode())
                self.assertIsNone(result["required_opatch_version"])
                self.assertIsNone(result["evidence"])
                self.assertTrue(result["warnings"])

    def test_distinct_requirements_are_ambiguous_but_repeated_requirement_is_not(self):
        for second, expected in ((SENTENCE.replace("49", "50"), None), (SENTENCE, "12.2.0.1.49")):
            result, _ = self.hints(f"<p>{SENTENCE}</p><p>{second}</p>".encode())
            self.assertEqual(result["required_opatch_version"], expected)
            self.assertEqual(bool(result["warnings"]), expected is None)

    def test_conditional_requirement_cannot_be_hidden_by_a_recognized_sentence(self):
        conditional = "For older database homes, you must use OPatch version 12.2.0.1.50 or later to apply this patch."
        result, _ = self.hints(f"<p>{SENTENCE}</p><p>{conditional}</p>".encode())
        self.assertIsNone(result["required_opatch_version"])
        self.assertIsNone(result["evidence"])
        self.assertTrue(result["warnings"])

    def test_example_sections_are_ignored_until_the_next_peer_heading(self):
        example = SENTENCE.replace("49", "99")
        result, _ = self.hints((f"<h2>Example</h2><p>{example}</p><h3>Sample output</h3>"
                                f"<p>{example}</p><h3>Example requirements</h3><p>{example}</p>").encode())
        self.assertIsNone(result["required_opatch_version"])
        result, _ = self.hints((f"<h2>Example</h2><p>{example}</p><h2>Requirements</h2>"
                                f"<p>{SENTENCE}</p>").encode())
        self.assertEqual(result["required_opatch_version"], "12.2.0.1.49")

    def test_sha_mismatch_rejects_before_parsing_and_does_not_expose_contents(self):
        payload = b"private content that changed since inspection"
        with patch.object(procedure_hints.evidence, "read_evidence", return_value=manifest(SENTENCE.encode())), \
                patch.object(procedure_hints.remote, "pull_file", return_value=payload), \
                patch.object(procedure_hints, "_extract_requirement") as parse:
            with self.assertRaises(remote.RemoteError) as caught:
                procedure_hints.get_hints("host1", HOST)
        self.assertEqual(caught.exception.error, "readme_changed")
        self.assertIn("Run artifact inspection again", caught.exception.message)
        self.assertNotIn(payload.decode(), caught.exception.message)
        parse.assert_not_called()

    def test_invalid_artifact_evidence_prevents_ssh(self):
        invalid = [None, {}, {"artifact": []}]
        for key, value in (("status", "blocked"), ("sha256", "invalid"), ("path", "relative"),
                           ("path", "/stage/../outside"), ("path", "/stage//patch"),
                           ("path", "/"), ("path", "/stage/patch\n"), ("path", "/stage/./patch")):
            document = manifest(SENTENCE.encode())
            document["artifact"][key] = value
            invalid.append(document)
        for document in invalid:
            with self.subTest(document=document), \
                    patch.object(procedure_hints.evidence, "read_evidence", return_value=document), \
                    patch.object(procedure_hints.remote, "pull_file") as pull:
                with self.assertRaises(remote.RemoteError):
                    procedure_hints.get_hints("host1", HOST)
                pull.assert_not_called()

    def test_invalid_or_unlisted_readme_selection_prevents_ssh(self):
        for selection in ("/etc/passwd", "../README.html", "docs/../README.html", "./README.html",
                          "docs//README.html", "README.html/", "README.html\n", "README\x00.html",
                          "docs\\README.html", "uninspected.html", None, 12):
            with self.subTest(selection=selection), \
                    patch.object(procedure_hints.evidence, "read_evidence", return_value=manifest(SENTENCE.encode())), \
                    patch.object(procedure_hints.remote, "pull_file") as pull:
                with self.assertRaises(remote.RemoteError):
                    procedure_hints.get_hints("host1", HOST, selection)
                pull.assert_not_called()

    def test_ambiguous_nested_missing_or_bad_digest_readme_evidence_prevents_ssh(self):
        good = {"path": "README.html", "sha256": "b" * 64}
        cases = [[], None, [good, {**good, "path": "docs/README.html"}], [good, good],
                 [{**good, "path": "docs/README.html"}], [{**good, "path": "../README.html"}],
                 [{**good, "sha256": "invalid"}], [{**good, "path": "something.html"}]]
        for references in cases:
            document = manifest(SENTENCE.encode())
            document["artifact"]["readme_files"] = references
            with self.subTest(references=references), \
                    patch.object(procedure_hints.evidence, "read_evidence", return_value=document), \
                    patch.object(procedure_hints.remote, "pull_file") as pull:
                with self.assertRaises(remote.RemoteError):
                    procedure_hints.get_hints("host1", HOST)
                pull.assert_not_called()

    def test_explicit_selection_can_read_exact_inspected_nested_readme(self):
        document = manifest(SENTENCE.encode(), "docs/README.html")
        document["artifact"]["readme_files"].append({"path": "README.html", "sha256": "b" * 64})
        result, pull = self.hints(document=document, selection="docs/README.html", host={"ssh_alias": "fixture-host"})
        self.assertEqual(result["readme_identifier"], "docs/README.html")
        pull.assert_called_once_with("fixture-host", "/stage/12345678/docs/README.html", timeout=30,
                                     sudo=False, max_bytes=2 * 1024 * 1024)

    def test_oversize_or_unsupported_encoding_cannot_supply_hints(self):
        for payload, code in ((b"x" * (procedure_hints.MAX_README_BYTES + 1), "readme_too_large"),
                              (b"<meta charset='utf-16'>" + SENTENCE.encode(), "invalid_readme_encoding"),
                              (b"\xff" + SENTENCE.encode(), "invalid_readme_encoding")):
            with self.subTest(code=code):
                with self.assertRaises(remote.RemoteError) as caught:
                    self.hints(payload)
                self.assertEqual(caught.exception.error, code)


if __name__ == "__main__":
    unittest.main()
