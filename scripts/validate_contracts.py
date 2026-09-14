#!/usr/bin/env python3
"""Validate schemas, bundled examples, and generated runtime fixture output.

The runtime gate executes real collectors/evaluators and a constrained fixture
executor, then checks required evidence, shapes, and timestamps against their
contracts. Negative probes ensure absent fields are actually rejected.
"""
from __future__ import annotations

import json
from datetime import datetime
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"

PLACEHOLDER = re.compile(r"^REPLACE_WITH_.*")
DUMMY_SHA256 = "a" * 64
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def valid_datetime(value) -> bool:
    """Require offset-aware timestamps without optional jsonschema extras."""
    if not isinstance(value, str):
        return True  # The schema's type keyword handles non-string values.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value):
        return False
    parsed = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return parsed.tzinfo is not None


def dummy_for(key: str) -> str:
    if "sha256" in key:
        return DUMMY_SHA256
    if key in ("patch_id", "platform_id"):
        return "12345678"
    if key == "required_opatch_version":
        return "12.2.0.1.51"
    return f"example-{key}"


def fill_placeholders(node, parent_key: str = ""):
    if isinstance(node, dict):
        return {k: fill_placeholders(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [fill_placeholders(v, parent_key) for v in node]
    if isinstance(node, str) and PLACEHOLDER.match(node):
        return dummy_for(parent_key)
    return node


def check_schema_files() -> list[str]:
    errors = []
    schema_files = sorted(CONTRACTS.rglob("*.schema.json"))
    if not schema_files:
        errors.append("no *.schema.json files found under contracts/")
        return errors
    for path in schema_files:
        rel = path.relative_to(ROOT)
        try:
            schema = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: invalid JSON ({exc})")
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            errors.append(f"{rel}: invalid JSON Schema ({exc.message})")
    return errors


def check_procedure_examples() -> list[str]:
    errors = []
    schema_path = CONTRACTS / "procedure" / "oracle-patch-procedure-v1.schema.json"
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    example_dir = CONTRACTS / "procedure" / "examples"
    examples = sorted(example_dir.glob("*.json"))
    if not examples:
        errors.append(f"no example/template files found under {example_dir.relative_to(ROOT)}")
        return errors
    for path in examples:
        rel = path.relative_to(ROOT)
        instance = fill_placeholders(json.loads(path.read_text()))
        problems = sorted(validator.iter_errors(instance), key=lambda e: e.path)
        for problem in problems:
            loc = "/".join(str(p) for p in problem.path) or "<root>"
            errors.append(f"{rel}: {loc}: {problem.message}")
    return errors


def validate_payload(schema_relative: str, instance: dict, label: str) -> list[str]:
    """Validate actual runtime output, including date/time and required fields."""
    schema = json.loads((CONTRACTS / schema_relative).read_text())
    validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    errors = []
    for problem in sorted(validator.iter_errors(instance), key=lambda error: str(list(error.path))):
        location = "/".join(str(part) for part in problem.path) or "<root>"
        errors.append(f"{label}: {location}: {problem.message}")
    return errors


def main() -> int:
    errors = check_schema_files() + check_procedure_examples()
    if not errors:
        sys.path.insert(0, str(ROOT / "tests"))
        from runtime_contracts import check_runtime_contracts
        errors.extend(check_runtime_contracts(validate_payload))
    if errors:
        print(f"contract validation failed ({len(errors)} problem(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    schema_count = len(list(CONTRACTS.rglob("*.schema.json")))
    example_count = len(list((CONTRACTS / "procedure" / "examples").glob("*.json")))
    print(f"contract validation passed: {schema_count} schemas, {example_count} procedure examples, generated runtime payloads and negative boundary probes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
