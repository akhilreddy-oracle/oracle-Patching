#!/usr/bin/env python3
"""Run local checks and bind their fixture-only receipts to exact source bytes.

This record is an audit artifact, not a signed release or production certificate.
No command here can grant live-lab or production approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import time

SOURCE_DIRS = ("bin", "lib", "operations", "contracts", "webapp", "scripts", "tests", "deploy", ".github")
SOURCE_FILES = ("Makefile", ".shellcheckrc", "package.json", "package-lock.json", "playwright.config.mjs", "webapp/requirements-sso.txt")
EXCLUDE = {"var", "__pycache__", "node_modules", ".venv", ".git", "test-results", "playwright-report"}
COMMANDS = {"check": ["make", "-j4", "check"], "browser": ["npm", "run", "test:browser"]}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def source_snapshot(root: Path) -> dict:
    """Hash code/contracts/test definitions, never caches or controller state."""
    root = root.resolve()
    paths = {root / name for name in SOURCE_FILES if (root / name).is_file()}
    for base in SOURCE_DIRS:
        directory = root / base
        if not directory.is_dir() or directory.is_symlink():
            continue
        for current, dirs, names in os.walk(directory, followlinks=False):
            paths.update(Path(current) / name for name in dirs if name not in EXCLUDE and (Path(current) / name).is_symlink())
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDE and not (Path(current) / name).is_symlink())
            for name in names:
                path = Path(current) / name
                if name.startswith(".env") or name.endswith((".log", ".pyc", ".tmp")):
                    continue
                # Installation inventory, local identity mappings and secrets are
                # deployment inputs, not the source release being fixture-tested.
                if path.parent == root / "webapp" and path.suffix == ".json":
                    continue
                if path.is_file() or path.is_symlink():
                    paths.add(path)
    files = []
    for path in sorted(paths):
        st = path.lstat()
        symlink = stat.S_ISLNK(st.st_mode)
        content = os.readlink(path).encode() if symlink else path.read_bytes()
        files.append({"path": path.relative_to(root).as_posix(), "sha256": digest(content), "mode": stat.S_IMODE(st.st_mode), "kind": "symlink" if symlink else "file"})
    return {"sha256": digest(canonical(files)), "files": files}


def _artifact(bundle: Path, name: str) -> bytes:
    if bundle.is_symlink():
        raise ValueError("symbolic validation bundle")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("invalid artifact path")
    target = bundle / path
    if any((bundle / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
        raise ValueError("symbolic artifact path")
    if not target.is_file() or target.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("missing or oversized artifact")
    return target.read_bytes()


def validation_status(root: Path, bundle: Path | None = None) -> dict:
    """Read-only display helper. Unknown or stale evidence never becomes green."""
    root = Path(root).resolve()
    bundle = Path(bundle) if bundle else root / "release-validation"
    result = {
        "fixture_tested": {"status": "unknown", "scopes": [], "reason": "No matching fixture validation record."},
        "live_lab_verified": {"status": "unverified", "reason": "Requires separate live lab evidence for this exact runtime and configuration."},
        "production_approved": {"status": "unverified", "reason": "Requires independent production signoff and the supported release certification process."},
    }
    try:
        record = json.loads(_artifact(bundle, "manifest.json"))
        if not isinstance(record, dict):
            raise ValueError("invalid fixture record")
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        if record.get("schema_version") != "1.0" or record.get("classification") != "fixture-only" or record.get("record_sha256") != digest(canonical(unsigned)):
            raise ValueError("invalid fixture record")
        snapshot = source_snapshot(root)
        if record.get("source_before", {}).get("sha256") != snapshot["sha256"] or record.get("source_after_sha256") != snapshot["sha256"]:
            raise ValueError("Source changed after validation; rerun the checks.")
        scopes = record.get("scopes")
        if not isinstance(scopes, list) or not scopes or len(set(scopes)) != len(scopes) or any(name not in COMMANDS for name in scopes):
            raise ValueError("invalid validation scope")
        receipts = record.get("receipts")
        if not isinstance(receipts, list) or len(receipts) != len(scopes):
            raise ValueError("missing command receipts")
        for scope, receipt in zip(scopes, receipts):
            if receipt.get("scope") != scope or receipt.get("command") != COMMANDS[scope] or receipt.get("log_sha256") != digest(_artifact(bundle, receipt["log_path"])):
                raise ValueError("command evidence failed verification")
        passed = all(type(item.get("exit_code")) is int and item["exit_code"] == 0 for item in receipts)
        result["fixture_tested"] = {"status": "passed" if passed else "failed", "scopes": scopes, "source_sha256": snapshot["sha256"], "reason": "Matching local fixture receipts verified; this is not live or production approval.", "completed_at": record.get("completed_at")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        result["fixture_tested"]["reason"] = str(error) if bundle.exists() else "No fixture validation bundle is available."
    return result


def run_validation(root: Path, bundle: Path, scopes: list[str], timeout: int = 3600) -> int:
    root = root.resolve()
    bundle.mkdir(parents=True, exist_ok=False)
    before = source_snapshot(root)
    receipts = []
    for scope in scopes:
        log_path = bundle / f"{scope}.log"
        started = time.time()
        print(f"Running fixture scope {scope}: {' '.join(COMMANDS[scope])}", flush=True)
        with log_path.open("xb") as log:
            try:
                process = subprocess.Popen(COMMANDS[scope], cwd=root, stdout=log, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    log.write(b"\nValidation command exceeded its timeout; its fixture process group was stopped.\n")
                    code = 124
            except OSError as error:
                log.write(f"Could not launch validation command: {error}\n".encode())
                code = 127
        receipts.append({"scope": scope, "command": COMMANDS[scope], "exit_code": code, "started_at": started, "completed_at": time.time(), "log_path": log_path.name, "log_sha256": digest(log_path.read_bytes())})
        print(f"{scope}: {'passed' if code == 0 else 'failed'} (exit {code}); log {log_path}", flush=True)
    after = source_snapshot(root)
    record = {
        "schema_version": "1.0", "classification": "fixture-only", "scopes": scopes,
        "source_before": before, "source_after_sha256": after["sha256"],
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "receipts": receipts, "completed_at": time.time(),
        "live_lab_verified": False, "production_approved": False,
    }
    record["record_sha256"] = digest(canonical(record))
    (bundle / "manifest.json").write_bytes(json.dumps(record, indent=2).encode() + b"\n")
    status = validation_status(root, bundle)
    (bundle / "status.json").write_bytes(json.dumps(status, indent=2).encode() + b"\n")
    print(json.dumps(status, indent=2), flush=True)
    return 0 if status["fixture_tested"]["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("check", "browser", "all"), default="all")
    parser.add_argument("--output", type=Path, default=Path("release-validation"), help="New bundle directory; existing directories are never overwritten")
    parser.add_argument("--status", action="store_true", help="Read current evidence only; execute no checks")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.status:
        print(json.dumps(validation_status(root, args.output), indent=2))
        return 0
    return run_validation(root, args.output.resolve(), list(COMMANDS) if args.suite == "all" else [args.suite])


if __name__ == "__main__":
    raise SystemExit(main())
