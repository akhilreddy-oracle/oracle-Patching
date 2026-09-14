#!/usr/bin/env bash
# Webapp TEST_MODE RAC and Grid demo plans drive the real executor binaries
# end-to-end through planctl (create → approve → authorize → dispatch →
# execute_remaining_tasks) with distinct SoD identities.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-webapp-racgrid.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
cd "$ROOT/webapp"
OPU_WEBAPP_TEST_TMP="$TMP" python3 - <<'PY'
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import planctl

tmp = Path(os.environ["OPU_WEBAPP_TEST_TMP"])
planctl.PLAN_STATE_DIR = tmp / "plan-state"
planctl.TESTMODE_DIR = tmp / "testmode"

for adapter in (
    "database_rolling_opatch",
    "database_rac_opatch_rollback",
    "grid_rolling_opatch",
    "grid_rolling_opatch_rollback",
):
    executor = planctl.EXECUTOR_BY_ADAPTER.get(adapter)
    assert executor is not None, f"TEST_MODE executor missing for {adapter}"
    assert executor.is_file(), f"TEST_MODE executor missing on disk: {executor}"


def window() -> tuple[str, str]:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now = datetime.now(timezone.utc)
    return (now - timedelta(minutes=1)).strftime(fmt), (now + timedelta(hours=1)).strftime(fmt)


def drive(plan_id: str, create_fn, worker: str, expected_tasks: int, expected_adapter: str) -> None:
    start, end = window()
    # Distinct SoD identities: requester != approver != operator != worker.
    plan = create_fn(plan_id, "patch-admin", start, end)
    assert plan["plan_id"] == plan_id, plan
    assert plan["procedure"]["adapter"] == expected_adapter, plan["procedure"]
    assert plan["nodes"] == ["node1", "node2"], plan["nodes"]

    sod = planctl.sod_summary(plan_id)
    assert sod["requester"] == "patch-admin" and sod["approver"] is None, sod
    assert sod["barred"]["approve"] == ["patch-admin"], sod
    # A freshly sealed plan is viable: window open, readiness unexpired, all sealed docs intact.
    via = planctl.viability(plan_id)
    assert via["actionable"] is True and via["blockers"] == [] and via["window"]["state"] == "open", via
    assert via["pre_execution"] is True and all(d["status"] == "ok" for d in via["documents"]), via
    # The same plan evaluated after its window closed / readiness expired / a sealed
    # document changed is reported dead, with every reason, instead of exit 66 later.
    import copy, datetime as _dt
    plan_doc = planctl.status(plan_id)
    late = _dt.datetime.fromisoformat(plan_doc["maintenance_window"]["end"].replace("Z", "+00:00")) + _dt.timedelta(hours=1)
    stale = planctl.viability(plan_id, plan_doc, now=late)
    assert stale["actionable"] is False and stale["window"]["state"] == "closed", stale
    assert any("window closed" in b for b in stale["blockers"]), stale["blockers"]
    tampered = copy.deepcopy(plan_doc)
    tampered["source_documents"]["readiness"]["path"] = "/nonexistent/readiness.json"
    tampered["source_documents"]["policy"]["sha256"] = "0" * 64
    dead = planctl.viability(plan_id, tampered)
    statuses = {d["name"]: d["status"] for d in dead["documents"]}
    assert statuses["readiness"] == "missing" and statuses["policy"] == "changed", statuses
    assert dead["actionable"] is False and any("no longer matches" in b for b in dead["blockers"]), dead
    planctl.approve(plan_id, "dba-approver", f"TEST-WEBAPP-{plan_id}")
    sod = planctl.sod_summary(plan_id)
    assert sod["approver"] == "dba-approver", sod
    assert sod["barred"]["authorize"] == ["dba-approver", "patch-admin"], sod
    planctl.authorize(plan_id, "patch-operator")
    sod = planctl.sod_summary(plan_id)
    assert sod["operator"] == "patch-operator" and sod["allowed"]["dispatch"] == ["patch-operator"], sod
    dispatched = planctl.dispatch(plan_id, "patch-operator")
    assert dispatched["state"] == "running", dispatched["state"]

    result = planctl.execute_remaining_tasks(plan_id, worker)
    assert result["plan_state"] == "succeeded", result
    assert result["stopped_reason"] == "succeeded", result
    assert result["executed_count"] == expected_tasks, result
    for task_result in result["task_results"]:
        assert task_result["status"] == "succeeded", task_result

    tasks = planctl.list_tasks(plan_id)
    assert len(tasks) == expected_tasks, [t.get("task_id") for t in tasks]
    assert all(t.get("status") == "succeeded" for t in tasks), tasks
    nodes = [t.get("node") for t in tasks]
    assert "node1" in nodes and "node2" in nodes, nodes


# RAC: 6 rolling stages per node + coordinator datapatch + final validate.
drive("webapp-rac-demo", planctl.create_testmode_demo_rac, "rac-worker", 14, "database_rolling_opatch")
# Grid: 6 rolling stages per node + coordinator cluster final validate.
drive("webapp-grid-demo", planctl.create_testmode_demo_grid, "grid-worker", 13, "grid_rolling_opatch")

print("webapp TEST_MODE RAC and Grid execution test passed")
PY
