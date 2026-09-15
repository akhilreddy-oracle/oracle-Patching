#!/usr/bin/env python3
"""Conservative disk admission for the fixed DISK level-0 recovery job.

RMAN unused-block compression is distinct from binary compression. Only
documented, measured unused blocks may reduce the database input budget.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def policy_options(policy):
    recovery = policy.get("recovery", {})
    if not isinstance(recovery, dict):
        raise ValueError("recovery must be an object")
    basis = recovery.get("capacity_basis", "allocated")
    if basis not in ("allocated", "rman_unused_blocks"):
        raise ValueError("recovery.capacity_basis must be allocated or rman_unused_blocks")
    if recovery.get("storage_mode", "fra") not in ("fra", "filesystem"):
        raise ValueError("recovery.storage_mode must be fra or filesystem")
    reserve = integer(recovery.get("minimum_filesystem_free_bytes", 0),
                      "recovery.minimum_filesystem_free_bytes")
    return basis, reserve


def natural(text, name):
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"invalid {name} in live capacity probe")
    return int(text)


def archive_measurement(root):
    """Count apparent input bytes; du/allocated blocks undercount sparse files."""
    pending = [Path(root)]
    apparent, tar_bytes, entries = 0, 0, 0
    while pending:
        path = pending.pop()
        info = path.lstat()
        entries += 1
        # Two headers per entry conservatively include long-name metadata.
        tar_bytes += 1024
        if stat.S_ISREG(info.st_mode):
            apparent += info.st_size
            tar_bytes += ((info.st_size + 511) // 512) * 512
        elif stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISLNK(info.st_mode):
            raise ValueError(f"unsupported Oracle archive entry: {path}")
    return {"apparent_bytes": apparent, "tar_budget_bytes": tar_bytes + 10240,
            "entries": entries}


def database_measurement(raw, allocated, dbid, basis):
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines or any(not line.startswith(("META|", "FILE|")) for line in lines):
        raise ValueError("live capacity probe contains missing or unexpected records")
    headers = [line.split("|") for line in lines if line.startswith("META|")]
    if len(headers) != 1 or len(headers[0]) != 7:
        raise ValueError("live capacity probe requires one complete database summary")
    _, compatible, grp, actual_dbid, expected_count, total, control = headers[0]
    if actual_dbid != dbid or natural(total, "allocated bytes") != allocated:
        raise ValueError("database identity or allocation changed during capacity preflight")
    count = natural(expected_count, "datafile count")
    if count == 0 or allocated == 0:
        raise ValueError("live database has no datafile allocation")
    grp_count = natural(grp, "guaranteed restore point count")
    control_bytes = natural(control, "control file bytes")
    version = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)*", compatible)
    compatible_ok = bool(version and tuple(map(int, version.groups())) >= (10, 2))
    global_reasons = []
    if basis == "allocated":
        global_reasons.append("allocated_policy")
    if not compatible_ok:
        global_reasons.append("compatible_below_10_2_or_unproven")
    if grp_count:
        global_reasons.append("guaranteed_restore_points_present")
    files, seen = [], set()
    for line in lines:
        if not line.startswith("FILE|"):
            continue
        fields = line.split("|")
        if len(fields) != 11:
            raise ValueError("invalid datafile capacity record")
        (_, file_id, file_bytes, dict_bytes, extent, unused, dict_status,
         online_status, tablespace_status, path, dict_id) = fields
        number = natural(file_id, "datafile number")
        size = natural(file_bytes, "datafile bytes")
        if number == 0 or size == 0 or number in seen:
            raise ValueError("duplicate or invalid datafile in capacity probe")
        seen.add(number)
        reasons = list(global_reasons)
        if dict_bytes != str(size) or dict_id != file_id:
            reasons.append("dictionary_file_coverage_unproven")
        if extent != "LOCAL":
            reasons.append("locally_managed_file_unproven")
        if (dict_status != "AVAILABLE" or online_status not in ("SYSTEM", "ONLINE")
                or tablespace_status not in ("ONLINE", "READ ONLY")):
            reasons.append("datafile_dictionary_visibility_unproven")
        free = natural(unused, "free extent bytes") if unused else 0
        if free > size:
            raise ValueError("free extents exceed the datafile allocation")
        if not unused:
            reasons.append("free_extents_unproven")
        measured_disk_bytes = None
        try:
            info = os.stat(path)
            if stat.S_ISREG(info.st_mode):
                measured_disk_bytes = info.st_blocks * 512
        except OSError:
            pass  # Physical allocation never supplies the admission discount.
        files.append({"file_id": number, "path": path, "allocated_bytes": size,
                      "filesystem_allocated_bytes": measured_disk_bytes,
                      "measured_free_bytes": free, "extent_management": extent or None,
                      "dictionary_file_id": dict_id or None, "dictionary_bytes": dict_bytes or None,
                      "dictionary_status": dict_status or None, "online_status": online_status or None,
                      "tablespace_status": tablespace_status or None,
                      "eligible": not reasons, "fallback_reasons": reasons})
    covered = len(files) == count and sum(item["allocated_bytes"] for item in files) == allocated
    if not covered:
        global_reasons.append("complete_datafile_coverage_unproven")
        for item in files:
            item["eligible"] = False
            item["fallback_reasons"].append("complete_datafile_coverage_unproven")
    provable_unused = sum(item["measured_free_bytes"] for item in files if item["eligible"])
    for item in files:
        item["budget_bytes"] = item["allocated_bytes"] - (item["measured_free_bytes"] if item["eligible"] else 0)
    return {"allocated_database_bytes": allocated, "database_budget_bytes": allocated - provable_unused,
            "provable_unused_bytes": provable_unused, "compatible": compatible,
            "guaranteed_restore_point_count": grp_count, "datafile_count": count,
            "complete_datafile_coverage": covered, "fallback_reasons": global_reasons,
            "effective_basis": "rman_unused_blocks" if provable_unused else "allocated",
            "controlfile_bytes": control_bytes, "datafiles": files}


def calculate(policy, raw, allocated, dbid, home, inventory, backup_parent, spfile):
    basis, reserve = policy_options(policy)
    result = database_measurement(raw, allocated, dbid, basis)
    home_measure = archive_measurement(home)
    inventory_measure = archive_measurement(inventory)
    spfile_info = os.stat(spfile)
    if not stat.S_ISREG(spfile_info.st_mode):
        raise ValueError("SPFILE must resolve to a regular filesystem file")
    payload = (result["database_budget_bytes"] + result["controlfile_bytes"] + spfile_info.st_size
               + home_measure["tar_budget_bytes"] + inventory_measure["tar_budget_bytes"])
    overhead = (payload + 4) // 5
    required = payload + overhead + reserve
    filesystem = os.statvfs(backup_parent)
    available = filesystem.f_bavail * filesystem.f_frsize
    result.update({"capacity_basis": basis, "measured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "probe_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                   "oracle_home": home_measure, "central_inventory": inventory_measure,
                   "spfile_bytes": spfile_info.st_size, "backup_payload_budget_bytes": payload,
                   "overhead_percent": 20, "overhead_bytes": overhead,
                   "minimum_filesystem_free_bytes": reserve, "required_bytes": required,
                   "available_bytes": available, "admitted": available >= required,
                   "binary_compression_discount_bytes": 0})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("policy", "measure"))
    parser.add_argument("--policy", required=True)
    for field in ("probe", "database-bytes", "dbid", "home", "inventory", "backup-parent", "spfile"):
        parser.add_argument("--" + field)
    args = parser.parse_args()
    try:
        policy = json.loads(Path(args.policy).read_text())
        policy_options(policy)
        if args.command == "measure":
            result = calculate(policy, Path(args.probe).read_text(),
                               natural(args.database_bytes, "allocated bytes"), args.dbid,
                               args.home, args.inventory, args.backup_parent, args.spfile)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"opu-agent: recovery capacity cannot be proven: {exc}", file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    sys.exit(main())
