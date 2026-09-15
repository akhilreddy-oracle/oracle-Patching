#!/usr/bin/env python3
"""Capacity admission tests without Oracle, SSH, or assumed compression."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("recovery_capacity", Path(__file__).resolve().parents[1] / "lib/opu/recovery_capacity.py")
CAPACITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPACITY)


class CapacityTests(unittest.TestCase):
    def probe(self, compatible="19.0.0", grp=0, first_extent="LOCAL", second="", count=1):
        return (f"META|{compatible}|{grp}|12345|{count}|{1024 * count}|512\n"
                f"FILE|1|1024|1024|{first_extent}|768|AVAILABLE|ONLINE|ONLINE|/unavailable/one.dbf|1\n" + second)

    def measure(self, raw=None, basis="rman_unused_blocks", allocated=1024):
        return CAPACITY.database_measurement(raw or self.probe(), allocated, "12345", basis)

    def test_default_is_full_allocation(self):
        self.assertEqual(CAPACITY.policy_options({}), ("allocated", 0))
        result = self.measure(basis="allocated")
        self.assertEqual(result["database_budget_bytes"], 1024)
        self.assertEqual(result["provable_unused_bytes"], 0)

    def test_measured_unused_blocks_require_opt_in(self):
        result = self.measure()
        self.assertEqual(result["database_budget_bytes"], 256)
        self.assertEqual(result["provable_unused_bytes"], 768)
        self.assertTrue(result["complete_datafile_coverage"])

    def test_global_prerequisites_fall_back_to_allocated(self):
        for raw in (self.probe(compatible="10.1.0"), self.probe(compatible="unknown"), self.probe(grp=1)):
            with self.subTest(raw=raw):
                self.assertEqual(self.measure(raw)["database_budget_bytes"], 1024)
        self.assertEqual(self.measure(self.probe(compatible="10.2.0"))["database_budget_bytes"], 256)

    def test_only_eligible_files_are_discounted(self):
        second = "FILE|2|1024|1024|DICTIONARY|768|AVAILABLE|ONLINE|ONLINE|/unavailable/two.dbf|2\n"
        result = self.measure(self.probe(second=second, count=2), allocated=2048)
        self.assertEqual(result["database_budget_bytes"], 1280)
        self.assertFalse(result["datafiles"][1]["eligible"])

    def test_uncovered_datafile_budget_is_not_lost(self):
        result = self.measure(self.probe(count=2), allocated=2048)
        self.assertFalse(result["complete_datafile_coverage"])
        self.assertEqual(result["database_budget_bytes"], 2048)

    def test_dictionary_and_free_space_unknown_fall_back(self):
        for original, replacement in (("|LOCAL|768|", "|LOCAL||"),
                                      ("|1|1024|1024|", "|1|1024||"),
                                      ("|AVAILABLE|", "|OFFLINE|")):
            with self.subTest(replacement=replacement):
                self.assertEqual(self.measure(self.probe().replace(original, replacement))["database_budget_bytes"], 1024)

    def test_duplicate_impossible_or_changed_measurements_block(self):
        for raw in (self.probe() + self.probe().splitlines()[1],
                    self.probe().replace("|LOCAL|768|", "|LOCAL|2048|"),
                    self.probe().replace("|12345|", "|999|"),
                    self.probe().replace("META|", "ORA-00942|")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.measure(raw)

    def test_capacity_policy_rejects_coercion_and_unknown_values(self):
        for value in (-1, 1.5, True, "10737418240", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CAPACITY.policy_options({"recovery": {"minimum_filesystem_free_bytes": value}})
        for value in ("compressed", None, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CAPACITY.policy_options({"recovery": {"capacity_basis": value}})

    def test_archive_budget_uses_apparent_sparse_file_size(self):
        with tempfile.TemporaryDirectory() as directory:
            sparse = Path(directory) / "sparse"
            with sparse.open("wb") as output:
                output.truncate(16 * 1024 * 1024)
            result = CAPACITY.archive_measurement(directory)
            self.assertEqual(result["apparent_bytes"], 16 * 1024 * 1024)
            self.assertGreater(result["tar_budget_bytes"], result["apparent_bytes"])

    def test_overhead_and_reserve_are_additive_without_compression_discount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "home").mkdir()
            (root / "inventory").mkdir()
            (root / "spfile").write_bytes(b"spfile")
            result = CAPACITY.calculate({"recovery": {"capacity_basis": "rman_unused_blocks", "minimum_filesystem_free_bytes": 10**18}},
                                        self.probe(), 1024, "12345", root / "home", root / "inventory", root, root / "spfile")
            payload = result["backup_payload_budget_bytes"]
            self.assertEqual(result["required_bytes"], payload + (payload + 4) // 5 + 10**18)
            self.assertFalse(result["admitted"])
            self.assertEqual(result["binary_compression_discount_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
