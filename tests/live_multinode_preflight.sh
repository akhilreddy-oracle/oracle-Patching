#!/usr/bin/env bash
# Unit-style checks for multi-node live execute preflight (no SSH).
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT/webapp"
python3 - <<'PY'
from pathlib import Path
import json
import os
import tempfile

import planctl

# Point hosts file at a temp multi-node registry.
hosts = {
  "hosts": [
    {
      "id": "lab-rac",
      "ssh_alias": "node1-ssh",
      "remote_root": "/opt/opu",
      "sudo": False,
      "nodes": [
        {"name": "node1", "ssh_alias": "node1-ssh"},
        {"name": "node2", "ssh_alias": "node2-ssh"},
      ],
    }
  ]
}
tmpdir = Path(tempfile.mkdtemp())
hosts_path = tmpdir / "hosts.json"
hosts_path.write_text(json.dumps(hosts))
planctl.HOSTS_FILE = hosts_path

plan = {"nodes": ["node1", "node2"], "target": {"coordinator_node": "node1"}}
planctl.preflight_live_plan_nodes(plan)

task = {"task_id": "001-rac-precheck-node2", "node": "node2", "adapter": "database_rolling_opatch"}
host = planctl._resolve_live_host_for_task(plan, task)
assert host["ssh_alias"] == "node2-ssh", host

# Missing ssh_alias must fail closed.
hosts["hosts"][0]["nodes"][1]["ssh_alias"] = None
hosts_path.write_text(json.dumps(hosts))
try:
    planctl.preflight_live_plan_nodes(plan)
    raise SystemExit("preflight accepted a node without ssh_alias")
except planctl.PlanError:
    pass

# RAC/Grid adapters must be wired for live execute.
assert "database_rolling_opatch" in planctl.LIVE_EXECUTOR_BY_ADAPTER
assert "grid_rolling_opatch" in planctl.LIVE_EXECUTOR_BY_ADAPTER
assert "grid_rolling_opatch_rollback" in planctl.LIVE_EXECUTOR_BY_ADAPTER
print("live multi-node preflight test passed")
PY
