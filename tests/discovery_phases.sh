#!/usr/bin/env bash
# Unit coverage for webapp discovery phase mapping over live snapshot shapes.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT/webapp"
python3 - <<'PY'
import tempfile
from unittest.mock import patch
from pathlib import Path

import discovery_phases

DIGEST = "a" * 64


def home(path, *, plat="collected", opatch="12.2.0.1.51", patches=None):
    platform = {
        "status": plat,
        "id": "226" if plat == "collected" else None,
        "name": "Linux x86-64" if plat == "collected" else None,
        "source": "opatch_lsinventory_xml" if plat == "collected" else None,
        "source_sha256": DIGEST if plat == "collected" else None,
    }
    return {
        "path": path,
        "owner": "oracle",
        "version": "19.0.0.0.0",
        "opatch_version": opatch,
        "platform": platform,
        "patch_inventory_source": "opatch_lsinventory_xml" if plat == "collected" else "unavailable",
        "opatch_inventory_xml_status": "collected" if plat == "collected" else "unavailable",
        "patches": patches if patches is not None else ["39034528"],
    }


def rac_snapshot():
    return {
        "schema_version": "1.0",
        "collector": {"name": "oracle.topology.discover", "version": "1"},
        "collected_at": "2026-07-25T12:00:00Z",
        "host": {"name": "node1.example", "os": "Oracle Linux Server 8.10", "kernel": "5.15"},
        "cluster": {
            "status": "detected",
            "grid_home": "/u01/grid",
            "runtime": {"status": "healthy", "upgrade_state": "NORMAL", "active_version": "19"},
            "nodes": [{"name": "node1", "status": "Active"}, {"name": "node2", "status": "Active"}],
        },
        "oracle_homes": [
            {**home("/u01/grid", patches=["1", "2"]), "owner": "grid"},
            home("/u01/db"),
        ],
        "databases": [
            {
                "db_unique_name": "ORCL",
                "oracle_home": "/u01/db",
                "runtime": {"status": "complete", "database_role": "PRIMARY"},
            }
        ],
        "warnings": [],
    }


def si_snapshot():
    return {
        "schema_version": "1.0",
        "collector": {"name": "oracle.topology.discover", "version": "1"},
        "collected_at": "2026-07-25T12:00:00Z",
        "host": {"name": "standalone.example", "os": "Oracle Linux Server 8.10", "kernel": "5.15"},
        "cluster": {
            "status": "unavailable",
            "grid_home": None,
            "runtime": {"status": "unavailable"},
            "nodes": [],
        },
        "oracle_homes": [home("/u01/db")],
        "databases": [
            {
                "db_unique_name": "ORCL",
                "oracle_home": "/u01/db",
                "runtime": {"status": "complete"},
            }
        ],
        "warnings": ["Clusterware home was not detected from PATH or active processes."],
    }


def by_id(phases):
    return {p["id"]: p for p in phases}


# RAC: all phases pass
rac = by_id(discovery_phases.derive_discovery_phases(rac_snapshot()))
assert rac["host_identity"]["status"] == "pass", rac["host_identity"]
assert rac["oracle_homes"]["status"] == "pass", rac["oracle_homes"]
assert rac["cluster"]["status"] == "pass", rac["cluster"]
assert rac["databases"]["status"] == "pass", rac["databases"]
assert rac["patch_inventory"]["status"] == "pass", rac["patch_inventory"]
assert discovery_phases.discovery_phases_rollup(list(rac.values())) == "pass"

# SI: cluster is N/A, not fail
si = by_id(discovery_phases.derive_discovery_phases(si_snapshot()))
assert si["cluster"]["status"] == "not_applicable", si["cluster"]
assert si["host_identity"]["status"] == "pass", si["host_identity"]
assert si["patch_inventory"]["status"] == "pass", si["patch_inventory"]
assert discovery_phases.discovery_phases_rollup(list(si.values())) == "pass"

# Missing platform blocks inventory (reconcile prerequisite)
broken = rac_snapshot()
broken["oracle_homes"][0]["platform"] = {
    "status": "failed",
    "id": None,
    "name": None,
    "source": None,
    "source_sha256": None,
}
inv = by_id(discovery_phases.derive_discovery_phases(broken))["patch_inventory"]
assert inv["status"] == "fail", inv
assert discovery_phases.discovery_phases_rollup(list(by_id(discovery_phases.derive_discovery_phases(broken)).values())) == "fail"

# Grid-only: no databases → N/A
grid_only = rac_snapshot()
grid_only["databases"] = []
dbs = by_id(discovery_phases.derive_discovery_phases(grid_only))["databases"]
assert dbs["status"] == "not_applicable", dbs

# Empty snapshot → unavailable placeholders
empty = discovery_phases.derive_discovery_phases(None)
assert len(empty) == 5
assert all(p["status"] == "unavailable" for p in empty), empty

# Unknown values and malformed nested payloads must not become green phases.
for field in ("owner", "opatch_version"):
    document = si_snapshot()
    document["oracle_homes"][0][field] = "unknown"
    assert by_id(discovery_phases.derive_discovery_phases(document))["oracle_homes"]["status"] != "pass"
for value in ("bad-digest", [], None):
    document = si_snapshot()
    document["oracle_homes"][0]["platform"]["source_sha256"] = value
    assert by_id(discovery_phases.derive_discovery_phases(document))["patch_inventory"]["status"] == "fail"
for document in ({"host": [1]}, {"oracle_homes": [None]}, {"cluster": [1]}, {"databases": [None]}):
    phases = discovery_phases.derive_discovery_phases(document)
    assert len(phases) == 5 and any(p["status"] == "fail" for p in phases), phases

# pipeline_state consumes isolated persisted fixtures, never a developer's
# mutable host evidence or real inventory.
import evidence
import pipeline_steps
with tempfile.TemporaryDirectory() as temporary, patch.object(evidence, "VAR_DIR", Path(temporary)):
    for host_id, snapshot in (("rac-fixture", rac_snapshot()), ("standalone-fixture", si_snapshot())):
        evidence.write_evidence(host_id, "snapshot", snapshot)
        state = pipeline_steps.pipeline_state(host_id)
        discovery = next(step for step in state if step["step"] == "discovery")
        assert discovery["done"] is True
        assert len(discovery["phases"]) == 5
        assert discovery["status"] == discovery["phases_status"] == "pass"

print("discovery_phases_ok")
PY
