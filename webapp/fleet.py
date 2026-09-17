"""Read-only fleet summaries from dated evidence, never execution authority."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import time

import evidence
import fleet_metadata


def epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


def _read(host, name):
    try:
        value = evidence.read_evidence(host, name)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _number(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _bound_readiness(host_id, readiness, snapshot, procedure_input):
    refs = readiness.get("snapshot_evidence")
    if not isinstance(refs, list) or not refs:
        return False
    root = evidence.evidence_dir(host_id)
    matches_current = False
    for ref in refs:
        try:
            path = Path(ref["path"])
            if path.is_symlink() or path.resolve().parent != root.resolve() or not path.name.startswith("snapshot"):
                return False
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                return False
            matches_current = matches_current or json.loads(raw) == snapshot
        except (ValueError, KeyError, TypeError, OSError):
            return False
    binding = readiness.get("evidence")
    if not matches_current or not isinstance(binding, dict):
        return False
    required = {"policy_sha256": "policy", "reconciliation_sha256": "reconciliation",
                "artifact_manifest_sha256": "artifact", "procedure_validation_sha256": "procedure",
                "compatibility_sha256": "compatibility_reconciliation"}
    optional = {"recovery_sha256": "recovery"}
    try:
        for field, name in {**required, **{k: v for k, v in optional.items() if k in binding}}.items():
            path = evidence.evidence_path(host_id, name)
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != binding.get(field):
                return False
        validated = _read(host_id, "procedure")
        source = validated.get("evidence")
        if (validated.get("status") != "ready_for_planning" or validated.get("procedure") != procedure_input
                or not isinstance(source, dict) or source.get("procedure_sha256") != hashlib.sha256(
                    evidence.evidence_path(host_id, "procedure_input").read_bytes()).hexdigest()
                or readiness.get("patch_id") != procedure_input.get("patch_id")):
            return False
        if "dataguard_sha256" in binding:
            dg = readiness.get("dataguard_evaluation")
            if (not isinstance(dg, dict) or dg.get("sha256") != binding["dataguard_sha256"]
                    or not isinstance(dg.get("path"), str)):
                return False
            path = Path(dg["path"])
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != dg["sha256"]:
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def build(hosts, *, now=None, can_manage_metadata=False):
    now = time.time() if now is None else now
    rows = []
    metadata_error = None
    try:
        metadata = fleet_metadata.snapshot(hosts)
    except fleet_metadata.MetadataError as exc:
        metadata, metadata_error = {}, exc.message
    for host in hosts.values():
        host_id = host["id"]
        snapshot, readiness, policy, procedure = (_read(host_id, x) for x in ("snapshot", "readiness", "policy", "procedure_input"))
        stamp = epoch(snapshot.get("collected_at"))
        limit = policy.get("maximum_snapshot_age_seconds", 1800)
        limit = limit if _number(limit) and limit > 0 else 1800
        fresh = stamp is not None and 0 <= now - stamp <= limit
        freshness = "fresh" if fresh else "stale" if stamp is not None and stamp <= now else "unknown"
        homes = {h.get("path"): h for h in _objects(snapshot.get("oracle_homes")) if isinstance(h.get("path"), str)}
        databases = _objects(snapshot.get("databases")) or [{}]
        bound = _bound_readiness(host_id, readiness, snapshot, procedure)
        valid_until = epoch(readiness.get("valid_until"))
        for database in databases:
            home = homes.get(database.get("oracle_home"), {}) if isinstance(database.get("oracle_home"), str) else {}
            runtime = database.get("runtime") if isinstance(database.get("runtime"), dict) else {}
            configured = metadata.get(host_id, {})
            desired = configured.get("desired_patch_baseline")
            patches = home.get("patches")
            inventory = isinstance(patches, list) and all(isinstance(x, str) and x.isdigit() for x in patches)
            inventory = (inventory and home.get("opatch_inventory_xml_status") == "collected"
                         and home.get("patch_inventory_source") == "opatch_lsinventory_xml"
                         and isinstance(home.get("opatch_inventory_xml_sha256"), str)
                         and len(home["opatch_inventory_xml_sha256"]) == 64
                         and all(c in "0123456789abcdef" for c in home["opatch_inventory_xml_sha256"]))
            baseline_status = "unknown"
            multi_node = isinstance(host.get("nodes"), list) and len(host["nodes"]) > 1
            baseline_reason = ("All-node baseline compliance is unavailable: this view contains the primary node's inventory."
                               if multi_node else None)
            if fresh and inventory and desired and not multi_node:
                baseline_status = "compliant" if desired in patches else "behind"
            name = database.get("db_unique_name") if isinstance(database.get("db_unique_name"), str) else None
            target = procedure.get("target")
            target_matches = name and isinstance(target, dict) and target.get("database_unique_name") == name
            readiness_status = "unknown"
            if fresh and bound and target_matches and valid_until and valid_until > now:
                readiness_status = readiness.get("status") if readiness.get("status") in ("ready_for_approval", "blocked") else "unknown"
            gates = _objects(readiness.get("gates"))
            blockers = sum(g.get("status") in ("blocker", "blocked", "fail", "failed") for g in gates) if target_matches and bound else None
            backup_status = "unknown"
            age = runtime.get("backup_age_minutes")
            recovery_policy = policy.get("recovery") if isinstance(policy.get("recovery"), dict) else {}
            maximum_age = recovery_policy.get("max_backup_age_minutes")
            if fresh and runtime.get("status") == "complete":
                if "latest_backup_completed_at" in runtime and not runtime.get("latest_backup_completed_at"):
                    backup_status = "missing"
                elif _number(age) and _number(maximum_age):
                    backup_status = "fresh" if age + (now - stamp) / 60 <= maximum_age else "stale"
            backup_time = runtime.get("latest_backup_completed_at")
            rows.append({"host_id": host_id, "host_label": host.get("label", host_id), "database": name,
                "oracle_home": database.get("oracle_home"), "environment": configured.get("environment"),
                "metadata_version": configured.get("metadata_version"),
                "configuration_missing": [field for field in ("environment", "desired_patch_baseline") if not configured.get(field)],
                "oracle_version": runtime.get("database_version") or home.get("version"),
                "patch_baseline": ", ".join(patches) if inventory else None, "desired_patch_baseline": desired,
                "baseline_status": baseline_status, "backup_status": backup_status,
                "baseline_reason": baseline_reason,
                "backup_completed_at": backup_time if epoch(backup_time) else None,
                "readiness": readiness_status, "evidence_status": freshness, "evidence_at": snapshot.get("collected_at"),
                "blockers": blockers, "backup_restore_validated": None,
                "attention": "blocked" if blockers or readiness_status == "blocked" else "behind" if baseline_status == "behind" else "refresh" if not fresh else "backup" if backup_status in {"missing", "stale"} else "review" if readiness_status == "unknown" else "ready"})
    order = {"blocked": 0, "behind": 1, "backup": 2, "refresh": 3, "review": 4, "ready": 5}
    rows.sort(key=lambda row: (order[row["attention"]], row["host_id"], row["database"] or ""))
    return {"generated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(), "databases": rows,
            "can_manage_metadata": bool(can_manage_metadata and not metadata_error), "metadata_error": metadata_error,
            "notice": "Inventory and backup freshness are observations; restore validation and patch approval require their own evidence."}
