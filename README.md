# Oracle Patching Utility

This project will provide a controlled, auditable utility for patching Oracle
Database estates. The first release targets Oracle Database 19c on Oracle
Linux x86-64, with initial executable certification planned for Oracle Linux 8
and 9.

This is a greenfield implementation. It does not import or depend on any prior
patching project, codebase, schema, agent, or deployment.

The design is split into two planes:

- A **control plane** manages inventory, patch metadata, policy, approvals,
  scheduling, orchestration, and audit history.
- A **managed host agent** performs only fixed, versioned Oracle operations on
  a target server and reports structured results to the control plane.

Arbitrary remote shell execution is deliberately outside the design.

## Current phase

Architecture and delivery decomposition are complete. The current build has
read-only discovery, evidence reconciliation, artifact/procedure validation,
OPatch compatibility, policy readiness, verified recovery evidence, and typed
execution adapters for standalone Database, RAC Database, and manual rolling
Grid Infrastructure OPatch workflows. Standalone and RAC Database apply plus
their separately approved rollback paths, and manual Grid apply, are
lab-capable and fail closed. Production certification, a separately approved
Grid rollback adapter, Data Guard orchestration, and OPatchAuto execution
remain outside the certified build.

Implemented agent capabilities:

- fixed, versioned operation allowlist; no generic shell operation
- per-task durable evidence and atomic completion publication
- idempotency-key locking and replay of the original completed result
- Oracle Linux host discovery
- Oracle-home observations from `oratab` and the central inventory
- authoritative `opatch lsinventory -xml` inventory where available
- per-node RAC topology reconciliation
- deterministic patch-artifact inspection and procedure binding
- authoritative artifact platform binding from the single
  `etc/config/inventory.xml` platform entry: the ARU platform ID, platform
  name, and source digest are sealed through readiness and the immutable plan
- discovered Oracle-home platform evidence reconciled against that artifact
  binding; for Linux, ARU platform `226` (`Linux x86-64`) is distinct from
  platform `46` (`Linux x86`) and is never inferred from a ZIP filename
- sealed OPatch bootstrap requests with independent approval, backup, version
  verification, and rollback (`bin/opu-opatch-upgrade`)
- per-home OPatch prerequisite compatibility evidence with separate sealed
  results for `CheckPatchApplicableOnCurrentPlatform` and
  `CheckConflictAgainstOHWithDetail`, including each native return code and
  evidence digest
- fail-closed readiness policy evaluation
- content-addressed recovery evidence with checksum, RMAN backup-set, recovery
  coverage, and restore-selection validation
- separately approved standalone `NOARCHIVELOG` recovery preparation with a
  consistent offline RMAN level-0, control-file/SPFILE coverage, Oracle-home
  and Central Inventory archives, automatic service restoration, fresh
  post-backup discovery, and deep recovery validation
- Grid recovery evidence binding Grid home and Central Inventory archives,
  OCR, and one node-local OLR backup for every reconciled node
- evidence-bound immutable plans, separation-of-duties approval, and
  maintenance-window authorization
- anti-replay readiness expiry: every supplied topology snapshot is sealed,
  policy-aged, re-hashed, and rechecked at create, approve, authorize, and
  dispatch
- sealed standalone Database stages for precheck, binary apply, validation,
  datapatch, and final SQL-patch verification
- separately approved, source-apply-bound standalone rollback stages with
  README-derived binary rollback and SQL rollback verification
- serial RAC Database apply stages for service drain, instance stop, local
  OPatch, restart, node validation, one coordinator datapatch, and cluster
  final validation
- separately approved RAC Database rollback child plans that seal every source
  task and controller-custody record, reverse the apply node order, and require
  one coordinator rollback datapatch and final validation
- manual Grid rolling stages for rootcrs prepatch, local OPatch, rootadd_rdbms,
  rootcrs postpatch, node validation, and cluster final validation; OPatchAuto
  planning is explicitly rejected
- explicit mutation outcome classes that distinguish no mutation, known binary
  state, unknown binary state, and a database left down
- executor-side applicability and conflict rechecks immediately before an
  outage and again at the apply boundary; a failed pre-outage prerequisite is
  classified `no_mutation` and cannot shut down the target
- structured, task-bound execution evidence validated by the controller
- fixture-driven failure, injection, provenance, and replay tests

Run the current quality gate with:

```bash
make check
```

- [System architecture](docs/ARCHITECTURE.md)
- [Program charter and accuracy gates](docs/PROGRAM_CHARTER.md)
- [Subprojects and build order](docs/IMPLEMENTATION_PLAN.md)
- [Complete architecture and delivery blueprint](Oracle_Patching_Utility_Blueprint.md)
- [High-assurance acceptance gates](docs/HIGH_ASSURANCE_ACCEPTANCE.md)
- [Readiness pipeline](docs/READINESS_PIPELINE.md)
- [Immutable plan control](docs/IMMUTABLE_PLAN_CONTROL.md)
- [Recovery evidence](docs/RECOVERY_EVIDENCE.md)
- [Standalone Database recovery preparation](docs/RECOVERY_PREPARATION.md)
- [Standalone Database patch execution](docs/STANDALONE_DATABASE_PATCH.md)
- [Standalone Database rollback](docs/STANDALONE_DATABASE_ROLLBACK.md)
- [RAC Database rollback](docs/RAC_DATABASE_ROLLBACK.md)
- [Lab patch artifact onboarding](docs/LAB_ARTIFACT_ONBOARDING.md)
- [Editable draw.io architecture diagrams](Oracle_Patching_Utility_Architecture.drawio)

## Target scope

- Oracle Database 19c Release Updates on Oracle Linux x86-64
- Standalone and RAC Database / Grid Infrastructure adapters
- Data Guard-aware orchestration after primary workflows are proven
- Discovery, inventory, patch repository, prechecks, approvals, execution,
  validation, rollback, reporting, and audit

Exadata, Windows, and Oracle applications are future adapters. A patch-specific
Oracle procedure manifest selects the supported Database or Grid adapter; the
utility does not infer patch instructions from filenames or generic commands.

## Agent commands

```bash
./bin/opu-agent operations
./bin/opu-agent doctor
./bin/opu-agent run \
  --operation host.discover \
  --operation-version 1 \
  --task-id discovery-001 \
  --idempotency-key host-discovery-001
```

Production execution requires Bash 4.4 or newer and the Oracle Linux
capabilities reported by `opu-agent doctor`. An alternate filesystem root is
accepted only in explicit test mode.

## Grid rolling patch utility

The high-assurance manual Grid path uses `opu-patch-plan` with
`opu-grid-node-patch`. It accepts only sealed typed tasks and implements the
fixed rootcrs/OPatch sequence; OPatchAuto plans are rejected. The earlier
root-operated helper is documented in
[Grid Infrastructure Rolling Patch Utility](docs/GRID_ROLLING_UTILITY.md), but
it is not the immutable-plan execution authority and is not exposed through
the read-only agent operation registry.

## Durable patch jobs

`opu-jobctl` remains the initial control-plane prototype for fixed Grid and
Database RAC jobs. The current high-assurance authority is `opu-patch-plan`,
which seals approvals and authorization, emits globally serial typed tasks,
validates structured postconditions, and takes controller custody of evidence.
Do not use `opu-jobctl` alone as mutation authority.

## Database RAC patching

The high-assurance RAC path uses `opu-patch-plan` with
`opu-database-rac-node-patch`. Each node receives six sealed stages, followed
by exactly one coordinator datapatch and final validation. Discovery,
compatibility, recovery evidence, service placement, a surviving instance,
binary inventory, SQL patch history, database health, leases, and the open
maintenance window are checked at the relevant boundaries. A distinct
`opu-database-rac-node-rollback` consumes only a separately approved child plan
derived from a fully succeeded source apply. See
[RAC Database rollback](docs/RAC_DATABASE_ROLLBACK.md).

The earlier `opu-database-rolling-patch` helper is retained for analysis and
compatibility, but its command-line approval flag is not equivalent to the
immutable-plan approval and evidence-custody workflow.
