"""Bounded inventory receipts captured from one live discovery operation.

This module never opens saved evidence. A receipt contains only the payloads
returned to its own discovery worker, plus the worker's exact run association.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

from diagnostics import redacted


class InventoryError(ValueError):
    pass


LIMITS = {"nodes": 16, "homes_per_node": 8, "databases_per_node": 16, "patches_per_home": 100}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_RUN = re.compile(r"[a-f0-9]{12}")
_RUNTIME = ("status", "database_version", "instance", "database_role", "open_mode",
            "instance_state", "cdb", "invalid_objects", "sqlpatch_non_success")
# The collector emits timestamps to the second. A small explicit tolerance
# avoids rejecting that rounding while still rejecting previous observations.
_CLOCK_TOLERANCE = timedelta(seconds=5)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def configuration_digest(host):
    return hashlib.sha256(_canonical(host).encode()).hexdigest()


def _datetime(value):
    try:
        if type(value) in (int, float):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, datetime):
            stamp = value
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (AttributeError, ValueError, TypeError, OverflowError, OSError):
        raise InventoryError("Live inventory has an invalid collection time") from None


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _scalar(value):
    return isinstance(value, (str, int, float, bool)) and not (
        isinstance(value, float) and not math.isfinite(value))


def _node(name, payload, started, completed):
    issues = []
    observed = _text(payload.get("collected_at"), 64)
    try:
        stamp = _datetime(observed)
        dated = started - _CLOCK_TOLERANCE <= stamp <= completed + _CLOCK_TOLERANCE
    except InventoryError:
        dated = False
    if not dated:
        issues.append("Collection time does not verify an observation made during this run")
    collector = payload.get("collector")
    collector_valid = (isinstance(collector, dict) and collector.get("name") == "oracle.topology.discover"
                       and payload.get("schema_version") == "1.0")
    if not collector_valid:
        issues.append("Topology collector provenance is missing or unsupported")
    verified = dated and collector_valid
    raw_homes, raw_databases = payload.get("oracle_homes"), payload.get("databases")
    if not isinstance(raw_homes, list) or not raw_homes:
        issues.append("Oracle home inventory is missing, empty or invalid")
        raw_homes = []
    if not isinstance(raw_databases, list):
        issues.append("Database observations are missing or invalid")
        raw_databases = []
    homes, databases = [], []
    for home in raw_homes[:LIMITS["homes_per_node"]]:
        if not isinstance(home, dict):
            issues.append("Invalid Oracle home entry omitted")
            continue
        patches = home.get("patches")
        digest = home.get("opatch_inventory_xml_sha256")
        provenance = (home.get("opatch_inventory_xml_status") == "collected"
                      and home.get("patch_inventory_source") == "opatch_lsinventory_xml"
                      and isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None)
        valid_patches = (isinstance(patches, list) and all(
            isinstance(patch, str) and re.fullmatch(r"[0-9]{1,20}", patch) for patch in patches))
        valid_path = isinstance(home.get("path"), str) and home["path"].startswith("/")
        accepted = verified and provenance and valid_patches and valid_path
        reason = None if accepted else "Current XML inventory and its checksum could not be verified"
        if not accepted:
            issues.append("An Oracle home's installed binary patch inventory is unverified")
        truncated = bool(accepted and len(patches) > LIMITS["patches_per_home"])
        if truncated:
            issues.append("An Oracle home's binary patch list exceeds the receipt limit")
        homes.append({"path": _text(home.get("path"), 1024),
            "version": _text(home.get("version"), 64) if verified else None,
            "opatch_version": _text(home.get("opatch_version"), 64) if verified else None,
            "binary_inventory_status": "collected" if accepted else "unknown",
            "patch_inventory_source": _text(home.get("patch_inventory_source"), 64),
            "opatch_inventory_xml_status": _text(home.get("opatch_inventory_xml_status"), 64),
            "opatch_inventory_xml_sha256": digest if provenance else None,
            "patches": patches[:LIMITS["patches_per_home"]] if accepted else None,
            "total_patches": len(patches) if accepted else None,
            "patches_truncated": truncated, "reason": reason})
    for database in raw_databases[:LIMITS["databases_per_node"]]:
        if not isinstance(database, dict):
            issues.append("Invalid database entry omitted")
            continue
        raw_runtime = database.get("runtime")
        runtime = {key: _text(raw_runtime[key]) if isinstance(raw_runtime[key], str) else raw_runtime[key]
                   for key in _RUNTIME if isinstance(raw_runtime, dict) and key in raw_runtime
                   and _scalar(raw_runtime[key])} if verified else {}
        count = runtime.get("sqlpatch_non_success")
        databases.append({"db_unique_name": _text(database.get("db_unique_name"), 128),
            "oracle_home": _text(database.get("oracle_home"), 1024), "runtime": runtime,
            "sql_patch_evidence": {"scope": "aggregate_only", "per_patch_status": "not_collected",
                "non_success_count": count if type(count) is int and count >= 0
                and runtime.get("status") == "complete" else None}})
    if len(raw_homes) > LIMITS["homes_per_node"]:
        issues.append("Oracle homes exceed the receipt limit")
    if len(raw_databases) > LIMITS["databases_per_node"]:
        issues.append("Databases exceed the receipt limit")
    return {"node": name, "collected_at": observed,
            "collection_status": "verified" if verified else "unknown",
            "oracle_homes": homes, "databases": databases,
            "coverage": {"total_homes": len(raw_homes), "total_databases": len(raw_databases),
                "homes_truncated": len(raw_homes) > LIMITS["homes_per_node"],
                "databases_truncated": len(raw_databases) > LIMITS["databases_per_node"],
                "issues": list(dict.fromkeys(issues))[:12]}}


def build_receipt(*, host_id, host, run_id, started_at, completed_at, node_snapshots):
    """Capture all nodes from this worker; incomplete transport never gets a receipt."""
    if (not isinstance(host_id, str) or not _IDENTIFIER.fullmatch(host_id)
            or host.get("id") != host_id or not isinstance(run_id, str) or not _RUN.fullmatch(run_id)):
        raise InventoryError("Live inventory needs its exact configured host and native run")
    started, completed = _datetime(started_at), _datetime(completed_at)
    if completed < started:
        raise InventoryError("Live inventory completion precedes its start")
    configured = host.get("nodes")
    expected = ([str(node.get("name") or "").split(".", 1)[0] for node in configured]
                if isinstance(configured, list) and configured else [host_id])
    captured = [name for name, _payload in node_snapshots]
    if (not expected or len(set(expected)) != len(expected) or captured != expected
            or any(not _IDENTIFIER.fullmatch(name) for name in expected)
            or any(not isinstance(payload, dict) for _name, payload in node_snapshots)):
        raise InventoryError("Live inventory did not capture every exact configured node")
    nodes = [_node(name, payload, started, completed)
             for name, payload in node_snapshots[:LIMITS["nodes"]]]
    incomplete = len(node_snapshots) > LIMITS["nodes"] or any(node["coverage"]["issues"] for node in nodes)
    result = redacted({"schema_version": "1.0", "source": "live_discovery",
        "host_id": host_id, "run_id": run_id, "configuration_sha256": configuration_digest(host),
        "started_at": started.isoformat(), "completed_at": completed.isoformat(),
        "status": "partial" if incomplete else "complete", "nodes": nodes,
        "coverage": {"all_nodes_collected": True, "configured_nodes": len(expected),
            "returned_nodes": len(nodes), "nodes_truncated": len(node_snapshots) > LIMITS["nodes"],
            "limits": dict(LIMITS)},
        "interpretation": "Collected by this live discovery run. Binary inventory is per Oracle home. "
            "Base database version is not an RU version. SQL failure counts do not establish per-patch SQL application status."})
    result["receipt_sha256"] = hashlib.sha256(_canonical(result).encode()).hexdigest()
    return result


def verify_receipt(receipt, *, host_id, run_id, configuration_sha256=None, not_before=None):
    """Require an intact receipt associated with the expected native operation."""
    if not isinstance(receipt, dict):
        raise InventoryError("The live discovery run returned no inventory receipt")
    if (receipt.get("schema_version") != "1.0" or receipt.get("source") != "live_discovery"
            or receipt.get("host_id") != host_id or receipt.get("run_id") != run_id
            or receipt.get("status") not in {"complete", "partial"}
            or not isinstance(receipt.get("configuration_sha256"), str)
            or not _DIGEST.fullmatch(receipt["configuration_sha256"])):
        raise InventoryError("The live inventory receipt does not match this operation")
    digest = receipt.get("receipt_sha256")
    try:
        actual = hashlib.sha256(_canonical({key: value for key, value in receipt.items()
                                          if key != "receipt_sha256"}).encode()).hexdigest()
    except (ValueError, TypeError, OverflowError):
        raise InventoryError("The live inventory receipt is malformed") from None
    if digest != actual:
        raise InventoryError("The live inventory receipt failed its integrity check")
    if configuration_sha256 is not None and receipt["configuration_sha256"] != configuration_sha256:
        raise InventoryError("The live inventory receipt belongs to a different host configuration")
    started, completed = _datetime(receipt.get("started_at")), _datetime(receipt.get("completed_at"))
    if completed < started or (not_before is not None and started < _datetime(not_before)):
        raise InventoryError("The live inventory receipt predates this operation")
    coverage, nodes = receipt.get("coverage"), receipt.get("nodes")
    if (not isinstance(coverage, dict) or coverage.get("all_nodes_collected") is not True
            or not isinstance(nodes, list) or not nodes or len(nodes) > LIMITS["nodes"]
            or coverage.get("returned_nodes") != len(nodes)):
        raise InventoryError("The live inventory receipt has incomplete node coverage")
    return receipt


def format_receipt(receipt):
    """Render facts directly; the language model cannot invent or erase patch IDs."""
    verify_receipt(receipt, host_id=receipt.get("host_id"), run_id=receipt.get("run_id"))
    lines = [f"Live discovery {'completed' if receipt['status'] == 'complete' else 'returned partial inventory'} "
             f"for {receipt['host_id']}.",
             f"Run: {receipt['run_id']}. Collected between {receipt['started_at']} and {receipt['completed_at']}."]
    for node in receipt["nodes"]:
        lines.append(f"\nNode: {node['node']} — collection time: {node.get('collected_at') or 'unavailable'}.")
        if node["collection_status"] != "verified":
            lines.append("Current inventory is unverified; this node cannot answer the live patch question.")
        else:
            for home in node["oracle_homes"]:
                lines.append(f"Oracle home: {home.get('path') or 'unavailable'}.")
                lines.append(f"Oracle base version: {home.get('version') or 'unavailable'}; "
                             f"OPatch version: {home.get('opatch_version') or 'unavailable'}.")
                if home["binary_inventory_status"] == "collected":
                    patches = ", ".join(home["patches"]) if home["patches"] else "none recorded in the verified XML inventory"
                    lines.append(f"Installed binary patch IDs: {patches}."
                                 + (" This list is truncated." if home["patches_truncated"] else ""))
                else:
                    lines.append("Installed binary patch IDs: unavailable — XML inventory provenance was not verified.")
            for database in node["databases"]:
                lines.append(f"Database: {database.get('db_unique_name') or 'unavailable'}; "
                             f"Oracle home: {database.get('oracle_home') or 'unavailable'}.")
        lines.extend(f"Collection issue: {issue}." for issue in node["coverage"]["issues"])
    if receipt["coverage"]["nodes_truncated"]:
        lines.append("Additional configured nodes were collected but exceed this receipt's display limit.")
    limitation = ("Binary patch IDs are per Oracle home; the base database version does not identify an RU. "
                  "Per-patch SQL application status was not collected by this discovery tool.")
    text = "\n".join(lines)
    if len(text) > 14000:
        # Preserve whole lines and the run provenance when the chat transcript
        # budget is smaller than the complete structured receipt.
        text = text[:14000].rsplit("\n", 1)[0]
        text += "\nDisplay truncated. Additional inventory remains in this run's structured receipt."
    return text + "\n" + limitation
