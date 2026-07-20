# High-Assurance Acceptance Gates

This utility is not permitted to apply, roll back, or switch an Oracle home
until every gate below is satisfied for the exact target and patch procedure.
A missing, stale, contradictory, or unparsable fact is `unknown`, and `unknown`
blocks mutation.

## Control-plane sequence

The control plane has four separate decisions. They must never be collapsed
into a successful shell-command exit status:

1. Collect one immutable snapshot per active RAC node, then reconcile it.
2. Inspect the exact staged artifact and validate the patch-specific Oracle
   procedure manifest against its digest and authoritative ARU platform ID.
3. Collect an OPatch compatibility result for every selected `node:home`, then
   evaluate it with the snapshots and recovery policy using
   `opu-readiness-evaluate`.
4. A `ready_for_approval` result may create an approval request. It is not an
   authorization to execute. Execution still requires the approved,
   content-addressed plan and an open maintenance window.

`opu-readiness-evaluate` deliberately returns `blocked` for an already applied
patch. That is a correct no-op outcome for an apply workflow, not a reason to
attempt a retry or rollback.

## Evidence hierarchy

The planner ranks evidence in this order and records every source and digest:

1. Oracle structured inventory and SQL views (`opatch lsinventory -xml`,
   central inventory XML, the patch artifact's `etc/config/inventory.xml`,
   `DBA_REGISTRY_SQLPATCH`, `V$` views).
2. Oracle Clusterware commands and their recorded versioned output (`srvctl`,
   `crsctl`, `olsnodes`).
3. Patch README and approved Oracle Support instructions bound by SHA-256 to
   the inspected patch artifact.
4. Operating-system observations, used only to corroborate Oracle evidence.

Human-readable command output cannot override structured Oracle evidence.

## Mandatory pre-execution gates

| Gate | Required result |
| --- | --- |
| Target inventory | All expected nodes have fresh, reconciled snapshots with no critical disagreement. |
| Patch artifact | One approved artifact, deterministic digest, patch ID and metadata extracted, no unsafe/mixed staging, and exactly one authoritative `etc/config/inventory.xml` ARU platform ID/name/source digest. ARU 226 (`Linux x86-64`) and ARU 46 (`Linux x86`) are different platforms. |
| Oracle procedure | README/MOS references, their hashes, exact patch method, numeric target platform ID, minimum OPatch version, prechecks, postchecks, and rollback conditions are bound to the artifact. |
| Compatibility | The artifact, procedure, discovered target home, readiness result, and plan all bind the same platform ID. For every selected `node:home`, both `CheckPatchApplicableOnCurrentPlatform` and `CheckConflictAgainstOHWithDetail` pass with native return code 0 and independently hashed evidence. |
| Grid health | Active nodes, normal upgrade state, cluster active patch level, CRS/ASM/listener/resource checks pass. |
| Database health | Expected role/open mode/instance/PDB state, SQL patch registry, invalid objects, and service state meet policy. |
| Recovery | Backup freshness and recoverability or a supported guaranteed restore point meet the approved policy; FRA/recovery capacity is sufficient. |
| Change policy | Maintenance window is open, separation-of-duties approval is present, concurrency is one rolling node, and rollback authority is recorded. |

## Execution invariants

- The executor runs only a generated, signed task type; it never evaluates an
  operator-provided shell command.
- Each task probes its precondition and postcondition. A timeout or lost lease
  changes the workflow to `paused/unknown`; it never retries blindly.
- Grid, Database, OJVM, Data Guard, and out-of-place patch methods are
  separate adapters. A procedure chooses one supported adapter.
- Node ordering is derived from reconciled topology and is enforced globally.
- Every action emits immutable, content-addressed evidence.
- The selected adapter re-runs both Oracle platform-applicability and conflict
  prerequisites immediately before the first outage and again at the binary
  apply boundary. A failed pre-outage prerequisite has outcome
  `no_mutation`: no database, listener, instance, or Clusterware resource may
  be stopped.

## Post-execution gates

- Rediscover every affected node and reconcile it with the expected target
  inventory.
- Verify binary inventory, Grid active patch level, CRS/resource health,
  database availability, service restoration, `datapatch` outcome, and
  `DBA_REGISTRY_SQLPATCH` status.
- Any failed or unknown postcondition pauses the workflow and exposes only the
  documented recovery/rollback choices for that procedure.

## Required failure matrix

Before execution is enabled for a topology adapter, automated and live-lab
tests must cover: stale snapshot, missing node, mismatched inventory, corrupt
artifact, missing or multiple artifact platform entries, ARU 46/226 mismatch,
wrong procedure digest, insufficient OPatch version, platform-applicability
failure, conflict,
insufficient space, expired backup, unavailable restore path, node loss during
rolling work, failed OPatch, failed restart, failed datapatch, failed
postcheck, lease expiry, and rollback success/failure.
