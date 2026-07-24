# Future adapters: honest scoping

This document records the adapters that are deliberately **not** implemented in
this build, why the platform rejects them fail-closed today, and what a real
implementation would require. None of these are stubbed to pretend they work:
`bin/opu-procedure-validate` and `bin/opu-patch-plan` reject them with explicit
errors before any plan can be sealed.

## FPP (Fleet Patching and Provisioning)

Procedures declaring `target.method == "fpp"` are rejected by
`opu-procedure-validate` and again by `opu-patch-plan create`. The schema enum
reserves the method name so a manifest can be authored and versioned, but no
plan can be created from one.

FPP is a server-mediated model: patching is driven by an FPP server through
`rhpctl`, not by a local executor on the target. A real adapter needs:

- **An rhpctl/FPP server**: a running Fleet Patching and Provisioning server
  (a Grid Infrastructure feature) that owns the operation. The utility would
  become a client submitting and auditing `rhpctl move database` /
  `rhpctl move gihome` operations rather than executing sealed local stages.
- **A gold-image store**: FPP patches by provisioning a new working copy from
  a registered gold image and moving targets onto it. The image store and its
  provenance (image import, patch level, digests) would need the same sealed
  evidence treatment this repo applies to patch ZIP artifacts.
- **Client enrollment**: every target cluster must be enrolled as an FPP
  client (`rhpctl add client`), with credentials and network paths to the
  server. There is no enrollment or server inventory model in this build.
- **Evidence redesign**: today's evidence contracts seal per-stage local
  command output. FPP operations run on the server and report through its job
  system, so execution evidence, outcome classes, and lease heartbeats would
  have to be rebuilt around polling a remote job rather than wrapping a local
  process.

Until all of that exists, claiming FPP support would be a stub, so the method
fails closed instead.

## Exadata

Exadata patching is not a Database/Grid OPatch workflow: storage cells,
InfiniBand/RoCE switches, and database hosts are patched with
`patchmgr` and `dbnodeupdate.sh`, which orchestrate firmware, OS image, and
kernel updates and require reboots coordinated across the fabric. A real
adapter needs:

- `patchmgr`/`dbnodeupdate.sh` integration with their own prerequisite,
  rollback, and resume semantics, none of which map onto the current
  OPatch-shaped stage contracts;
- real hardware to validate against — cell and switch patching cannot be
  simulated honestly with filesystem fixtures;
- a distinct evidence model for firmware/image versions per component.

The existing platform binding already protects against accidental misuse: the
artifact's ARU platform ID is sealed at readiness and re-verified per home, so
an Exadata-only patch artifact fails closed on a non-matching platform before
any plan is created.

## Windows

The agent and every executor are Bash programs (`set -euo pipefail`, POSIX
tooling, `oratab`, root/`su` conventions) and are Linux-only by design.
Windows Oracle homes use services (`OracleService<SID>`), `opatch.bat`, and
registry-based configuration; there is no meaningful shared code path with the
Bash executors. A Windows port is a separate agent implementation (PowerShell
or a compiled agent) speaking the same sealed plan/evidence contracts to the
controller — not an adapter inside this codebase. No Windows target can pass
discovery today, so the platform fails closed by construction.
