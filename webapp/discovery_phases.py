"""Derive readiness-relevant discovery phases from a live topology snapshot.

Phases are a presentation view over ``oracle.topology.discover`` evidence —
they do not invent probes. One SSH run of ``opu-topology-discover`` still
produces the sealed snapshot; this module maps real fields onto the stages
the patching pipeline actually consumes (reconcile, compatibility, readiness).

Status meanings (fail-closed where downstream gates need the field):
  pass            — usable evidence present
  partial         — some evidence, incomplete for a hard gate
  fail            — required evidence missing or collector failed
  not_applicable  — expected absence for this topology (e.g. SI without CRS)
  unavailable     — collector reported unavailable when the phase needed more
"""
from __future__ import annotations

from typing import Any
import re


def _known_text(value):
    return isinstance(value, str) and value.strip().lower() not in {"", "unknown", "unavailable", "n/a"}

# Order matches operator-facing discovery → what later steps consume.
PHASE_DEFS: tuple[dict[str, str], ...] = (
    {
        "id": "host_identity",
        "letter": "A",
        "label": "Host identity & OS",
        "required": "always",
        "why": "Proves SSH reached the host and records hostname/OS/kernel for estate identity.",
        "unblocks": "Estate cards; snapshot freshness binding",
    },
    {
        "id": "oracle_homes",
        "letter": "B",
        "label": "Oracle homes & OPatch",
        "required": "always",
        "why": "Locates Grid/DB homes, owners, versions, and OPatch tooling used for apply/prereqs.",
        "unblocks": "Target-home selection; compatibility collector owner/path",
    },
    {
        "id": "cluster",
        "letter": "C",
        "label": "Cluster / RAC / CRS",
        "required": "rac_or_grid",
        "why": "CRS membership, Grid home, and runtime state for rolling/RAC/Grid patching.",
        "unblocks": "Multi-node reconcile; Grid readiness gates",
    },
    {
        "id": "databases",
        "letter": "D",
        "label": "Databases & instances",
        "required": "database_family",
        "why": "DB unique names, home mapping, and runtime health used by DB-family readiness.",
        "unblocks": "DB target selection; recovery/runtime gates",
    },
    {
        "id": "patch_inventory",
        "letter": "E",
        "label": "Patch inventory & platform",
        "required": "always",
        "why": "OPatch XML platform ID/name/digest and installed patches — required before reconcile.",
        "unblocks": "Reconcile; OPatch compatibility; readiness platform binding",
    },
)


def _phase(
    definition: dict[str, str],
    *,
    status: str,
    summary: str,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "id": definition["id"],
        "letter": definition["letter"],
        "label": definition["label"],
        "required": definition["required"],
        "why": definition["why"],
        "unblocks": definition["unblocks"],
        "status": status,
        "summary": summary,
        "detail": detail,
    }


def _host_identity(snapshot: dict) -> dict[str, Any]:
    definition = PHASE_DEFS[0]
    host = snapshot.get("host") or {}
    name = (host.get("name") or "").strip()
    os_name = (host.get("os") or "").strip()
    kernel = (host.get("kernel") or "").strip()
    if not name:
        return _phase(definition, status="fail", summary="Host name missing from snapshot.")
    bits = [name]
    if os_name:
        bits.append(os_name)
    if kernel:
        bits.append(f"kernel {kernel}")
    if os_name and kernel:
        status = "pass"
    else:
        status = "partial"
    return _phase(
        definition,
        status=status,
        summary="; ".join(bits),
        detail=f"collected_at={snapshot.get('collected_at') or 'unknown'}",
    )


def _oracle_homes(snapshot: dict) -> dict[str, Any]:
    definition = PHASE_DEFS[1]
    homes = snapshot.get("oracle_homes") or []
    if not isinstance(homes, list) or not homes:
        return _phase(
            definition,
            status="fail",
            summary="No Oracle homes discovered.",
            detail="Reconcile and compatibility cannot select a target home.",
        )
    missing_opatch = []
    missing_owner = []
    for home in homes:
        path = home.get("path") or "?"
        if not _known_text(home.get("owner")):
            missing_owner.append(path)
        if not _known_text(home.get("opatch_version")):
            missing_opatch.append(path)
    if missing_owner:
        return _phase(
            definition,
            status="fail",
            summary=f"{len(homes)} home(s); owner missing on {len(missing_owner)}.",
            detail=", ".join(missing_owner[:4]),
        )
    with_version = sum(1 for h in homes if _known_text(h.get("version")))
    if missing_opatch:
        return _phase(
            definition,
            status="partial",
            summary=f"{len(homes)} home(s); OPatch version missing on {len(missing_opatch)}.",
            detail=", ".join(missing_opatch[:4]),
        )
    return _phase(
        definition,
        status="pass",
        summary=f"{len(homes)} home(s); OPatch present on all; version on {with_version}/{len(homes)}.",
        detail="; ".join(f"{h.get('path')} ({h.get('opatch_version')})" for h in homes[:4]),
    )


def _cluster(snapshot: dict) -> dict[str, Any]:
    definition = PHASE_DEFS[2]
    cluster = snapshot.get("cluster") or {}
    status_raw = str(cluster.get("status") or "").lower()
    runtime = cluster.get("runtime") or {}
    runtime_status = str(runtime.get("status") or "").lower()
    nodes = cluster.get("nodes") or []
    grid_home = cluster.get("grid_home")
    active = [n for n in nodes if str(n.get("status") or "").lower() == "active"]

    if status_raw in ("unavailable", "not_applicable") and not nodes and not grid_home:
        return _phase(
            definition,
            status="not_applicable",
            summary="No Clusterware detected — expected for single-instance hosts.",
            detail="Required for RAC/Grid procedures; optional for single_instance. Not a database-down signal.",
        )

    if status_raw != "detected":
        return _phase(
            definition,
            status="fail",
            summary=f"Cluster status is {cluster.get('status') or 'missing'}.",
            detail="RAC/Grid patching needs cluster.status=detected.",
        )

    if not nodes:
        return _phase(
            definition,
            status="partial",
            summary="Clusterware detected but no CRS nodes listed.",
            detail=f"grid_home={grid_home or '—'}",
        )

    if runtime_status == "healthy":
        return _phase(
            definition,
            status="pass",
            summary=(
                f"Detected; {len(nodes)} node(s), {len(active)} Active; "
                f"runtime healthy; grid_home={grid_home or '—'}"
            ),
            detail=(
                f"upgrade_state={runtime.get('upgrade_state') or '—'}; "
                f"active_version={runtime.get('active_version') or '—'}"
            ),
        )

    if runtime_status in ("unavailable", "unknown", ""):
        return _phase(
            definition,
            status="partial",
            summary=(
                f"Detected; {len(nodes)} node(s); runtime {runtime.get('status') or 'missing'}."
            ),
            detail="Grid readiness prefers runtime.status=healthy and upgrade_state=NORMAL.",
        )

    return _phase(
        definition,
        status="fail",
        summary=f"Cluster detected but runtime status is {runtime.get('status')}.",
        detail=f"grid_home={grid_home or '—'}",
    )


def _databases(snapshot: dict) -> dict[str, Any]:
    definition = PHASE_DEFS[3]
    databases = snapshot.get("databases") or []
    cluster = snapshot.get("cluster") or {}
    cluster_detected = str(cluster.get("status") or "").lower() == "detected"

    if not isinstance(databases, list) or not databases:
        if cluster_detected:
            return _phase(
                definition,
                status="not_applicable",
                summary="No databases registered — OK for Grid-only patching on this host.",
                detail="Required for database-family procedures.",
            )
        return _phase(
            definition,
            status="fail",
            summary="No databases discovered on a non-cluster host.",
            detail="Database-family readiness needs db_unique_name → oracle_home mapping.",
        )

    complete = 0
    unavailable = 0
    missing_home = 0
    for db in databases:
        if not (db.get("oracle_home") or "").strip():
            missing_home += 1
        runtime = db.get("runtime") or {}
        rt = str(runtime.get("status") or "").lower()
        if rt == "complete":
            complete += 1
        elif rt == "unavailable":
            unavailable += 1

    if missing_home:
        return _phase(
            definition,
            status="fail",
            summary=f"{len(databases)} database(s); {missing_home} missing oracle_home.",
        )
    if complete == len(databases):
        return _phase(
            definition,
            status="pass",
            summary=f"{len(databases)} database(s); runtime complete on all.",
            detail="; ".join(
                f"{d.get('db_unique_name')}@{d.get('oracle_home')}" for d in databases[:4]
            ),
        )
    if complete or unavailable:
        return _phase(
            definition,
            status="partial",
            summary=(
                f"{len(databases)} database(s); runtime complete={complete}, "
                f"unavailable={unavailable}."
            ),
            detail="Readiness recovery/DB gates need runtime.status=complete.",
        )
    return _phase(
        definition,
        status="partial",
        summary=f"{len(databases)} database(s) registered; runtime evidence incomplete.",
    )


def _patch_inventory(snapshot: dict) -> dict[str, Any]:
    definition = PHASE_DEFS[4]
    homes = snapshot.get("oracle_homes") or []
    if not isinstance(homes, list) or not homes:
        return _phase(
            definition,
            status="fail",
            summary="No homes — cannot evaluate patch inventory / platform evidence.",
        )

    collected = 0
    failed = 0
    unavailable = 0
    patch_total = 0
    for home in homes:
        platform = home.get("platform") or {}
        plat_status = str(platform.get("status") or "").lower()
        if plat_status == "collected":
            # Reconcile requires numeric id, name, XML source, and sha256.
            if (
                re.fullmatch(r"[0-9]+", str(platform.get("id") or ""))
                and _known_text(platform.get("name"))
                and platform.get("source") == "opatch_lsinventory_xml"
                and re.fullmatch(r"[a-f0-9]{64}", str(platform.get("source_sha256") or ""))
            ):
                collected += 1
            else:
                failed += 1
        elif plat_status == "failed":
            failed += 1
        else:
            unavailable += 1
        patch_total += len(home.get("patches") or [])

    if collected == len(homes):
        return _phase(
            definition,
            status="pass",
            summary=(
                f"Platform XML collected for {collected}/{len(homes)} home(s); "
                f"{patch_total} installed patch id(s)."
            ),
            detail="Required before reconcile, compatibility, and readiness platform binding.",
        )
    if failed:
        return _phase(
            definition,
            status="fail",
            summary=(
                f"Platform evidence incomplete: collected={collected}, "
                f"failed={failed}, unavailable={unavailable} of {len(homes)}."
            ),
            detail="Reconcile blocks homes without platform.status=collected from OPatch XML.",
        )
    if collected:
        return _phase(
            definition,
            status="partial",
            summary=(
                f"Platform XML on {collected}/{len(homes)} home(s); "
                f"{unavailable} unavailable."
            ),
            detail="All target homes need collected platform evidence before reconcile.",
        )
    return _phase(
        definition,
        status="unavailable",
        summary=f"No OPatch XML platform evidence on {len(homes)} home(s).",
        detail="Run discovery as a user that can execute opatch lsinventory -xml.",
    )


_EVALUATORS = (
    _host_identity,
    _oracle_homes,
    _cluster,
    _databases,
    _patch_inventory,
)


def derive_discovery_phases(snapshot: dict | None) -> list[dict[str, Any]]:
    """Return phase records for a topology snapshot, or not-run placeholders."""
    if not isinstance(snapshot, dict):
        return [
            _phase(
                definition,
                status="unavailable",
                summary="No live discovery snapshot yet.",
                detail="Run discovery over SSH to collect this phase.",
            )
            for definition in PHASE_DEFS
        ]
    phases = []
    for definition, evaluate in zip(PHASE_DEFS, _EVALUATORS):
        try:
            phases.append(evaluate(snapshot))
        except (AttributeError, TypeError, ValueError):
            phases.append(_phase(definition, status="fail", summary="Snapshot fields are malformed.",
                                 detail="Refresh discovery to collect valid evidence for this phase."))
    return phases


def discovery_phases_rollup(phases: list[dict[str, Any]]) -> str:
    """Roll up phase statuses for a single badge on the discovery step."""
    if not phases:
        return "unavailable"
    statuses = {p.get("status") for p in phases}
    if "fail" in statuses:
        return "fail"
    if "unavailable" in statuses:
        return "partial"
    if "partial" in statuses:
        return "partial"
    # not_applicable + pass is success for the topology shape we discovered.
    if statuses <= {"pass", "not_applicable"}:
        return "pass"
    return "partial"
