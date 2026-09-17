"""Typed assistant capabilities. Model output is data, never executable code."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import stat

import evidence
import planctl
import recoveryctl
from diagnostics import redacted


class ToolError(ValueError):
    pass


# Every argument is required; actors, policies and credentials are deliberately
# absent. All writes are proposals dispatched through the normal HTTP commands.
SPECS = {
    "list_estate": ("List configured hosts and saved database observations. Use inspect_host for dated installed binary patch IDs and inventory provenance.", (), "read"),
    "list_plans": ("List saved patch plans and their states.", (), "read"),
    "list_backups": ("List saved local recovery summaries for a configured host. No SSH; current native state remains unverified until explicitly inspected.", ("host_id",), "read"),
    "inspect_host": ("Inspect saved installed binary patch inventory, Oracle/OPatch versions, observation dates and freshness, readiness, requirements and backup summaries. SQL counters are aggregate evidence, not per-patch confirmation. Does not refresh SSH.", ("host_id",), "read"),
    "inspect_plan": ("Inspect a saved plan and its tasks.", ("plan_id",), "read"),
    "inspect_backup": ("Inspect a saved local recovery summary without SSH. Use an analyze_backup proposal for explicit native inspection.", ("request_id",), "read"),
    "refresh_discovery": ("Propose live SSH discovery. May synchronize collector tools and replace saved discovery evidence.", ("host_id",), "execute"),
    "refresh_readiness": ("Propose refreshing live evidence and readiness using saved requirements/policy. Stops at blockers; may invalidate older evidence-bound plans.", ("host_id",), "execute"),
    "create_patch_plan": ("Propose a patch plan from verified saved requirements and readiness. Does not approve or apply it.", ("host_id", "plan_id", "patch_id", "database", "window_start", "window_end"), "create"),
    "create_backup": ("Propose creating a live backup preparation request, requiring separate review and authorization before execution.", ("host_id", "request_id", "database", "backup_parent", "window_start", "window_end"), "create"),
    "analyze_backup": ("Propose native analysis of a backup preparation request; may inspect the remote host.", ("request_id",), "read"),
    "execute_backup": ("Propose execution of an independently approved and authorized backup request. May stop the database for the native cold-backup phase.", ("request_id",), "execute"),
    "select_backup": ("Propose validating and selecting a completed live backup against this host's current evidence.", ("host_id", "request_id"), "execute"),
    "dispatch_plan": ("Propose dispatching an already authorized patch plan within its sealed maintenance window.", ("plan_id",), "dispatch"),
    "execute_plan": ("Propose executing remaining tasks of an already running/authorized plan. May stop database/listener and change Oracle binaries. Stops on failure, blocker or unknown outcome.", ("plan_id",), "execute"),
}
READ_TOOLS = {"list_estate", "list_plans", "list_backups", "inspect_host", "inspect_plan", "inspect_backup"}


def definitions(allowed):
    return [{"type": "function", "function": {"name": name, "description": spec[0],
        "parameters": {"type": "object", "properties": {key: {"type": "string"} for key in spec[1]},
                       "required": list(spec[1]), "additionalProperties": False}}}
        for name, spec in SPECS.items() if spec[2] in allowed]


def validate(name, arguments, hosts):
    if name not in SPECS or not isinstance(arguments, dict) or set(arguments) != set(SPECS[name][1]):
        raise ToolError("Unsupported tool or unexpected arguments")
    for key, value in arguments.items():
        if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ToolError(f"Invalid {key}")
        if key in {"host_id", "plan_id", "request_id", "database", "patch_id"} and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ToolError(f"Invalid {key}")
    if "host_id" in arguments and arguments["host_id"] not in hosts:
        raise ToolError("Unknown configured host")
    if "window_start" in arguments:
        try:
            start, end = [datetime.fromisoformat(arguments[key].replace("Z", "+00:00")) for key in ("window_start", "window_end")]
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError()
        except ValueError:
            raise ToolError("Maintenance window needs ordered ISO timestamps with an explicit timezone") from None
    if "backup_parent" in arguments and not re.fullmatch(r"/[A-Za-z0-9_./-]+", arguments["backup_parent"]):
        raise ToolError("Backup parent must be an absolute server path")
    return dict(arguments)


def _fields(value, names):
    return {key: value[key] for key in names if key in value}


_RUNTIME_FIELDS = ("status", "database_version", "instance", "database_role", "open_mode",
                   "instance_state", "cdb", "invalid_objects", "sqlpatch_non_success",
                   "pdb_not_read_write", "latest_backup_completed_at", "backup_age_minutes")
_INVENTORY_LIMITS = {"nodes": 4, "homes_per_node": 6, "databases_per_node": 8, "patches_per_home": 80}


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _runtime(value):
    if not isinstance(value, dict):
        return {}
    return {key: _text(item) if isinstance(item, str) else item for key in _RUNTIME_FIELDS
            if key in value and (item := value[key]) is not None and isinstance(item, (str, int, float, bool))
            and not (isinstance(item, float) and not math.isfinite(item))}


def _saved_document(host_id, name):
    try:
        value = evidence.read_evidence(host_id, name)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _snapshot_inventory(snapshot, evidence_name, maximum_age, now):
    collected_at = _text(snapshot.get("collected_at"), 64)
    age, freshness = None, "unknown"
    try:
        stamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            elapsed = (now - stamp).total_seconds()
            age = math.floor(elapsed)
            if elapsed >= 0:
                freshness = "fresh" if elapsed <= maximum_age else "stale"
    except (AttributeError, ValueError, TypeError, OverflowError):
        pass
    homes, databases, omissions = [], [], []
    raw_homes, raw_databases = snapshot.get("oracle_homes"), snapshot.get("databases")
    if not isinstance(raw_homes, list):
        raw_homes = []
        omissions.append("Oracle home inventory is missing or invalid")
    if not isinstance(raw_databases, list):
        raw_databases = []
        omissions.append("Database observations are missing or invalid")
    for home in raw_homes[:_INVENTORY_LIMITS["homes_per_node"]]:
        if not isinstance(home, dict):
            omissions.append("Invalid Oracle home entry omitted")
            continue
        patches = home.get("patches")
        inventory_valid = (home.get("opatch_inventory_xml_status") == "collected"
                           and home.get("patch_inventory_source") == "opatch_lsinventory_xml"
                           and isinstance(patches, list)
                           and all(isinstance(p, str) and re.fullmatch(r"[0-9]{1,20}", p) for p in patches))
        homes.append({"path": _text(home.get("path")), "version": _text(home.get("version"), 64),
            "opatch_version": _text(home.get("opatch_version"), 64),
            "binary_inventory_status": "collected" if inventory_valid else "unknown",
            "patch_inventory_source": _text(home.get("patch_inventory_source"), 64),
            "opatch_inventory_xml_status": _text(home.get("opatch_inventory_xml_status"), 64),
            "opatch_inventory_xml_sha256": _text(home.get("opatch_inventory_xml_sha256"), 64),
            "patches": patches[:_INVENTORY_LIMITS["patches_per_home"]] if inventory_valid else None,
            "total_patches": len(patches) if inventory_valid else None,
            "patches_truncated": bool(inventory_valid and len(patches) > _INVENTORY_LIMITS["patches_per_home"])})
    for database in raw_databases[:_INVENTORY_LIMITS["databases_per_node"]]:
        if not isinstance(database, dict):
            omissions.append("Invalid database entry omitted")
            continue
        runtime = _runtime(database.get("runtime"))
        count = runtime.get("sqlpatch_non_success")
        databases.append({"db_unique_name": _text(database.get("db_unique_name"), 128),
            "oracle_home": _text(database.get("oracle_home")), "runtime": runtime,
            "sql_patch_evidence": {"scope": "aggregate_only", "per_patch_status": "not_collected",
                "non_success_count": count if type(count) is int and count >= 0 and runtime.get("status") == "complete" else None}})
    return {"evidence_name": evidence_name, "collected_at": collected_at, "age_seconds": age,
            "freshness": freshness, "oracle_homes": homes, "databases": databases,
            "coverage": {"total_homes": len(raw_homes), "total_databases": len(raw_databases),
                "homes_truncated": len(raw_homes) > _INVENTORY_LIMITS["homes_per_node"],
                "databases_truncated": len(raw_databases) > _INVENTORY_LIMITS["databases_per_node"],
                "omissions": omissions[:8]}}


def saved_inventory(host_id):
    """Bounded observations only; neither current state nor patching authority."""
    policy = _saved_document(host_id, "policy") or {}
    maximum_age = policy.get("maximum_snapshot_age_seconds", 1800)
    try:
        valid_age = type(maximum_age) in (int, float) and math.isfinite(maximum_age) and maximum_age > 0
    except OverflowError:
        valid_age = False
    if not valid_age:
        maximum_age = 1800
    now = datetime.now(timezone.utc)
    index = _saved_document(host_id, "snapshot_nodes") or {}
    entries = index.get("nodes")
    indexed = isinstance(entries, list) and bool(entries)
    entries = entries if indexed else [{"evidence": "snapshot"}]
    nodes, omissions, seen = [], [], set()
    for entry in entries[:_INVENTORY_LIMITS["nodes"]]:
        if not isinstance(entry, dict):
            omissions.append("Invalid node index entry omitted")
            continue
        name = entry.get("evidence")
        if not name and isinstance(entry.get("name"), str):
            try:
                name = evidence.node_snapshot_evidence_name(entry["name"])
            except ValueError:
                pass
        if not isinstance(name, str) or not re.fullmatch(r"snapshot(?:_[A-Za-z0-9][A-Za-z0-9._:-]{0,118})?", name):
            omissions.append("Invalid node evidence reference omitted")
            continue
        if name in seen:
            omissions.append("Duplicate node evidence reference omitted")
            continue
        seen.add(name)
        snapshot = _saved_document(host_id, name)
        if snapshot is None:
            omissions.append(f"{name}: saved snapshot unavailable")
            continue
        nodes.append({"node": _text(entry.get("name"), 128),
                      **_snapshot_inventory(snapshot, name, maximum_age, now)})
    return {"source": "saved_discovery_snapshots", "live_state_verified": False,
            "maximum_snapshot_age_seconds": maximum_age,
            "interpretation": "Binary inventory is per Oracle home. Base database version is not an RU version. SQL non-success counts do not establish any specific patch applied successfully.",
            "nodes": nodes, "coverage": {"scope": "indexed_nodes" if indexed else "primary_snapshot_only",
                "completeness": "not_asserted", "indexed_nodes": len(entries) if indexed else None,
                "returned_nodes": len(nodes), "nodes_truncated": len(entries) > _INVENTORY_LIMITS["nodes"],
                "omissions": omissions, "limits": _INVENTORY_LIMITS}}


def _named_hosts(content, hosts):
    """Match only configured IDs, including natural spacing such as target db."""
    matches = []
    content = content[:16000] if isinstance(content, str) else ""
    normalized_ids = {re.sub(r"[^A-Za-z0-9]", "", key).lower() for key in hosts if isinstance(key, str)}
    for host_id in hosts:
        if not isinstance(host_id, str) or not evidence._ID_RE.fullmatch(host_id):
            continue
        normalized = re.sub(r"[^A-Za-z0-9]", "", host_id).lower()
        variants = [normalized]
        if len(normalized) > 2 and normalized.endswith("db"):
            expanded = normalized[:-2] + "database"
            if expanded not in normalized_ids:
                variants.append(expanded)
        positions = []
        for variant in variants:
            pattern = r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9]*".join(re.escape(c) for c in variant) + r"(?![A-Za-z0-9])"
            match = re.search(pattern, content, re.I | re.ASCII)
            if match:
                positions.append(match.start())
        if positions:
            matches.append((min(positions), host_id))
    return [host_id for _, host_id in sorted(matches)]


def grounding(content, hosts):
    """Preload read-only saved evidence; never infer a target or dispatch a tool."""
    matched = _named_hosts(content, hosts)
    result = {"source": "saved_evidence", "live_state_verified": False, "inspected_hosts": [],
              "matched_hosts_truncated": len(matched) > 3, "context_truncated": False,
              "estate": {"hosts": [], "total_hosts": len(hosts), "hosts_truncated": len(hosts) > 100}}
    maximum = 7500

    def size():
        return len(json.dumps(result, allow_nan=False))

    for host_id in matched[:3]:
        try:
            inspected = read("inspect_host", {"host_id": host_id}, hosts)
            # Inventory comes first; requirements and blockers remain available
            # through the regular inspect_host tool when the question needs them.
            row = _fields(inspected, ("host_id", "source", "host_link", "inventory"))
            if not row.get("inventory", {}).get("nodes"):
                row["status"] = "unavailable"
            row["additional_evidence"] = "Use inspect_host for saved requirements, readiness and backup selection."
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            row = {"host_id": host_id, "status": "unavailable", "host_link": f"#/hosts/{host_id}/discover",
                   "reason": "Saved host evidence could not be read; other hosts remain available."}
        result["inspected_hosts"].append(row)
        if size() > maximum - 800:
            # Keep valid JSON and the exact named host, never a cut-off evidence
            # string that might hide an omitted node or collection failure.
            result["inspected_hosts"].pop()
            result["matched_hosts_truncated"] = True
            result["context_truncated"] = True
            row = {"host_id": host_id, "status": "omitted_for_context_limit",
                   "next_action": "Call inspect_host for this configured host's saved evidence."}
            if size() + len(json.dumps(row)) < maximum - 800:
                result["inspected_hosts"].append(row)
            break
    for host_id, host in list(hosts.items())[:100]:
        try:
            single = read("list_estate", {}, {host_id: host})
            original = single["hosts"][0]
            row = _fields(original, ("host_id", "label", "collected_at", "host_link", "inventory_available", "databases_truncated", "snapshot_status"))
            row["label"] = _text(row.get("label"), 128)
            row["collected_at"] = _text(row.get("collected_at"), 64)
            row["databases"] = [_fields(db, ("db_unique_name", "oracle_home")) for db in original["databases"][:8]]
            row["databases_truncated"] = original["databases_truncated"] or len(original["databases"]) > 8
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, IndexError):
            row = {"host_id": _text(host_id, 128), "status": "unavailable"}
        result["estate"]["hosts"].append(redacted(row))
        if size() > maximum:
            result["estate"]["hosts"].pop()
            result["estate"]["hosts_truncated"] = True
            result["context_truncated"] = True
            break
    return redacted(result)


def _cache_json(path):
    """Read bounded local data without following symlinks or invoking a tool."""
    try:
        if any(entry.is_symlink() for entry in (path, *path.parents)):
            raise ValueError()
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022 or info.st_size > 4 * 1024 * 1024):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError()
            raw = source.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value, hashlib.sha256(raw).hexdigest(), datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
    except (OSError, ValueError, TypeError):
        raise ToolError('Saved backup record is unavailable or unsafe; inspect the native recovery page') from None


def _saved_analysis(native, metadata):
    approval = native.get('approval')
    approved = approval.get('analysis') if isinstance(approval, dict) else None
    analysis = approved if isinstance(approved, dict) else metadata.get('analysis')
    if not isinstance(analysis, dict):
        return None
    summary = _fields(analysis, ('status', 'reason', 'analyzed_at', 'request_id'))
    capacity = analysis.get('capacity')
    if isinstance(capacity, dict):
        summary['capacity'] = _fields(capacity, ('admitted', 'required_bytes', 'available_bytes', 'complete_datafile_coverage'))
    summary['source'] = 'saved_approval_analysis' if isinstance(approved, dict) else 'saved_analysis'
    return summary


def cached_backup(request_id):
    """Return (whitelisted saved summary, exact local record digests).

    Cached metadata is not current native authority. Even a saved completed
    state must pass the existing native confirmation/selection commands.
    """
    live = recoveryctl._live_path(request_id)
    digests = {}
    if live.exists():
        base = live
        metadata, digest, updated = _cache_json(base / 'metadata.json')
        if metadata.get('request_id') != request_id or metadata.get('mode') != 'live':
            raise ToolError('Saved recovery request identity is invalid')
        digests['metadata.json'] = digest
        native = metadata.get('last_status') or {}
        if not isinstance(native, dict) or native and native.get('request_id') != request_id:
            raise ToolError('Saved native recovery identity is invalid')
        if any(value.get('target') is not None and not isinstance(value['target'], dict) for value in (metadata, native)):
            raise ToolError('Saved recovery target is invalid')
        for name in ('snapshot.json', 'policy.json'):
            path = base / name
            if path.exists() or path.is_symlink():
                _, digests[name], _ = _cache_json(path)
        summary = {**_fields(native, ('request_id', 'state', 'target', 'maintenance_window')),
                   'request_id': request_id, 'mode': 'live', 'host_id': metadata.get('host_id'),
                   'target': metadata.get('target') or native.get('target'), 'state': native.get('state', 'unknown')}
    else:
        base = recoveryctl._fixture_path(request_id)
        native, digest, updated = _cache_json(base / 'state' / request_id / 'request.json')
        if native.get('request_id') != request_id:
            raise ToolError('Saved recovery request identity is invalid')
        digests['request.json'] = digest
        metadata = {}
        if (base / 'webapp-metadata.json').exists() or (base / 'webapp-metadata.json').is_symlink():
            metadata, digests['webapp-metadata.json'], _ = _cache_json(base / 'webapp-metadata.json')
        summary = {**_fields(native, ('request_id', 'state', 'target', 'maintenance_window')),
                   'mode': 'test_mode', 'host_id': metadata.get('host_id'), 'state': native.get('state', 'unknown')}
    host_id = summary['host_id']
    if (host_id is None and summary['mode'] == 'live') or (host_id is not None and (
            not isinstance(host_id, str) or not evidence._ID_RE.fullmatch(host_id))):
        raise ToolError('Saved recovery host attribution is invalid')
    if summary.get('target') is not None and not isinstance(summary['target'], dict):
        raise ToolError('Saved recovery target is invalid')
    summary['target'] = _fields(summary.get('target') or {}, ('database_unique_name', 'oracle_home', 'host_id', 'oracle_sid', 'oracle_owner'))
    summary['analysis'] = _saved_analysis(native, metadata)
    summary.update(source='saved_local_record', cache_file_updated_at=updated, live_state_verified=False,
                   next_action='Use an analyze_backup proposal or the native recovery page for explicit inspection')
    return summary, digests


def cached_backups(host_id):
    summaries, seen = [], set()
    for root in (recoveryctl.LIVE_DIR, recoveryctl.RECOVERY_DIR):
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in seen or not entry.is_dir() or entry.is_symlink() or not recoveryctl._ID_RE.fullmatch(entry.name):
                continue
            seen.add(entry.name)
            try:
                summary, _ = cached_backup(entry.name)
            except (ToolError, recoveryctl.RecoveryError):
                continue  # Missing attribution cannot be assigned to this host.
            if summary.get('host_id') == host_id:
                summaries.append(summary)
                if len(summaries) == 100:
                    return summaries
    return summaries


def read(name, args, hosts):
    validate(name, args, hosts)
    if name == "list_estate":
        result = []
        for host_id, host in list(hosts.items())[:100]:
            snap = _saved_document(host_id, "snapshot") or {}
            databases = snap.get("databases") if isinstance(snap.get("databases"), list) else []
            result.append({"host_id": host_id, "label": host.get("label"), "collected_at": snap.get("collected_at"),
                "snapshot_status": "available" if snap else "unavailable",
                "host_link": f"#/hosts/{host_id}/discover", "inventory_available": bool(snap.get("oracle_homes")),
                "databases": [{"db_unique_name": _text(db.get("db_unique_name"), 128),
                    "oracle_home": _text(db.get("oracle_home")), "runtime": _runtime(db.get("runtime"))}
                    for db in databases[:30] if isinstance(db, dict)],
                "databases_truncated": len(databases) > 30})
        return redacted({"hosts": result, "source": "saved_evidence", "total_hosts": len(hosts),
                         "hosts_truncated": len(hosts) > 100})
    if name == "list_plans":
        plans = planctl.list_plans()
        return redacted({"plans": [_fields(p, ("plan_id", "state", "intent", "host_id", "requester", "patch_id", "patch", "target", "maintenance_window")) for p in plans[:100]], "total_plans": len(plans)})
    if name == "list_backups":
        requests = cached_backups(args["host_id"])
        return redacted({"host_id": args["host_id"], "source": "saved_local_record", "returned_requests": len(requests),
                        "coverage": "Readable local records only; unreadable or unattributed records are omitted. This is not a complete native inventory.",
                        "limit": 100, "limit_reached": len(requests) == 100, "requests": requests})
    if name == "inspect_host":
        host_id = args["host_id"]
        # Only structured evidence; raw logs, README bodies, host credentials and
        # server configuration never enter the model context.
        return redacted({"host_id": host_id, "source": "saved_evidence", "host_link": f"#/hosts/{host_id}/discover",
            "inventory": saved_inventory(host_id), **{key: evidence.read_evidence(host_id, key)
            for key in ("procedure_input", "readiness", "recovery_selection")}})
    if name == "inspect_plan":
        plan = planctl.status(args["plan_id"])
        unresolved = plan.get('unresolved_run')
        viability = planctl.viability(args['plan_id'], plan=plan)
        return redacted({**_fields(plan, ("plan_id", "state", "intent", "requester", "patch_id", "patch", "target", "maintenance_window")),
            "unresolved_run": _fields(unresolved, ('run_id', 'status', 'error')) if isinstance(unresolved, dict) else None,
            "viability": _fields(viability, ('window', 'blockers', 'readiness_expired', 'readiness_valid_until')),
            "tasks": [_fields(t, ("task_id", "stage", "node", "status")) for t in planctl.list_tasks(args["plan_id"])],
            "approval_link": f"#/plans/{args['plan_id']}"})
    if name == "inspect_backup":
        request, _ = cached_backup(args["request_id"])
        return redacted(request)
    raise ToolError("This tool requires a confirmed proposal")


def binding(name, args, hosts):
    """Bind confirmation to exact saved target/config/evidence, without sending it to the model."""
    validate(name, args, hosts)
    state = {}
    if "host_id" in args:
        host_id = args["host_id"]
        state["host"] = hosts[host_id]
        state["evidence"] = {key: evidence.read_evidence(host_id, key) for key in (
            "snapshot", "snapshot_nodes", "artifact", "procedure_input", "procedure", "policy", "readiness", "recovery", "recovery_selection", "compatibility_reconciliation", "reconciliation")}
        if name == "create_patch_plan":
            procedure = state["evidence"]["procedure_input"] or {}
            if procedure.get("patch_id") != args["patch_id"] or (procedure.get("target") or {}).get("database_unique_name") != args["database"]:
                raise ToolError("Requested patch/database does not match the saved procedure. Review requirements in the host wizard first.")
    if "plan_id" in args:
        if name == "create_patch_plan":
            if (planctl.PLAN_STATE_DIR / "plans" / args["plan_id"]).exists():
                raise ToolError("Plan ID already exists")
        else:
            state["plan"] = planctl.status(args["plan_id"])
            state["tasks"] = planctl.list_tasks(args["plan_id"])
            host_id = planctl._host_id_for_plan(state["plan"])
            state["host"] = hosts.get(host_id) if host_id else None
            # Native commands verify sealed documents again at execution.
    if "request_id" in args:
        if name == "create_backup":
            if recoveryctl._live_path(args["request_id"]).exists() or recoveryctl._fixture_path(args["request_id"]).exists():
                raise ToolError("Backup request ID already exists")
        else:
            backup, state["backup_records"] = cached_backup(args["request_id"])
            state["backup_host"] = hosts.get(backup.get("host_id"))
            if name == "select_backup" and backup.get("host_id") != args["host_id"]:
                raise ToolError("Selected backup belongs to a different configured host")
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def route(name, args):
    """Exact allowlisted route/body for the existing authenticated command path."""
    if name in {"refresh_discovery", "refresh_readiness", "select_backup"}:
        step = {"refresh_discovery": "discovery", "refresh_readiness": "readiness-chain", "select_backup": "recovery-collect"}[name]
        return f"/api/hosts/{args['host_id']}/pipeline/{step}", ({"request_id": args["request_id"]} if name == "select_backup" else {})
    if name == "create_patch_plan":
        return "/api/plans", {key: value for key, value in args.items() if key not in {"patch_id", "database"}}
    if name == "create_backup":
        return "/api/recovery", dict(args)
    if name in {"analyze_backup", "execute_backup"}:
        return f"/api/recovery/{args['request_id']}/{'analyze' if name == 'analyze_backup' else 'execute'}", {}
    if name in {"dispatch_plan", "execute_plan"}:
        return f"/api/plans/{args['plan_id']}/{'dispatch' if name == 'dispatch_plan' else 'execute-remaining'}", {}
    raise ToolError("Tool has no executable route")


def expected_run(name, args):
    """The existing dispatcher must return a new run in this exact scope."""
    if name in {"refresh_discovery", "refresh_readiness", "select_backup"}:
        return "pipeline", f"host:{args['host_id']}:pipeline"
    if name in {"create_patch_plan", "dispatch_plan", "execute_plan"}:
        action = {"create_patch_plan": "create", "dispatch_plan": "dispatch", "execute_plan": "execute"}[name]
        return "plan", f"plan:{args['plan_id']}:{action}"
    if name in {"create_backup", "analyze_backup", "execute_backup"}:
        action = {"create_backup": "create", "analyze_backup": "analyze", "execute_backup": "execute"}[name]
        return "recovery", f"recovery:{args['request_id']}:{action}"
    raise ToolError("Tool has no native execution scope")
