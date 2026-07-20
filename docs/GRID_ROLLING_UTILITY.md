# Grid Infrastructure Rolling Patch Utility

`bin/opu-grid-rolling-patch` is a root-operated, fixed-workflow utility for a
one-off Oracle Grid Infrastructure patch that Oracle certifies for rolling,
local OPatch installation. It is intentionally separate from `opu-agent`,
which remains the read-only control-plane foundation.

## Guardrails

- `analyze` checks CRS health, inventory, and OPatch conflicts without mutation.
- `apply` and `rollback` need explicit `--approve` and persist completed node
  steps so an identical command can resume after a failure.
- Each run writes a run manifest, completed-step ledger, main log, and
  `last.failure` record under its evidence directory.
- Every node must be an active Clusterware member, pass local and remote CRS
  health checks, and retain at least 4096 MB free in both the Grid home and
  staging filesystem (override deliberately with `--min-free-mb`).
- Before a patch that is not already installed is analyzed or applied, the
  utility verifies the requested patch ID from patch XML—not its directory
  name.
- The supplied node file is the exact rolling order; SSH is BatchMode only.
- By default it connects as `root`. Where root SSH is disabled, specify an
  existing automation account with passwordless sudo using `--remote-user`.
- The patch directory must exist at the same absolute path on every node.
- No services are drained or relocated and no database or PDB is started. That
  must be covered by the approved topology-specific change runbook.

## Use

```bash
bin/opu-grid-rolling-patch --grid-home /u01/app/19.0.0/grid \
  --patch-dir /stage/38723830 --mode analyze \
  --remote-user opc --min-free-mb 4096

bin/opu-grid-rolling-patch --grid-home /u01/app/19.0.0/grid \
  --patch-dir /stage/38723830 --mode apply --approve
```

To roll back, replace `--patch-dir` with `--patch-id 38723830` and set
`--mode rollback --approve`. Logs and resume markers reside under
`/var/tmp/opu-grid-rolling-<mode>-<patch-id>/` by default.
