# Standalone Database Recovery Preparation

`opu-database-recovery-prepare` closes the controlled gap between discovery
and `opu-recovery-evidence-collect`. It creates a new recovery set; the
collector remains the independent read-only validator.

The first supported preparation adapter is a standalone `PRIMARY`, `READ
WRITE`, `NOARCHIVELOG` database using an SPFILE. Oracle requires a
`NOARCHIVELOG` whole-database backup to follow a consistent shutdown and run
while the database is mounted. The utility therefore treats preparation as a
separately approved maintenance-window operation, not as an online check.

## Control lifecycle

```text
awaiting_approval -> approved -> authorized -> running -> completed
```

The requester cannot approve the request. The authorizer must differ from the
requester and approver. The request seals the topology snapshot, policy,
database, Oracle home, owner, SID, derived backup root, and UTC maintenance
window. The backup root is always `BACKUP_PARENT/REQUEST_ID`; it must not
exist, is never overwritten, and an incomplete root is retained for audit.

Example:

```bash
tool=/home/opc/patching-oracle-v51/bin/opu-database-recovery-prepare
state=/u02/patches/39034528/evidence/recovery-preparation-state

sudo -n env OPU_RECOVERY_PREP_STATE_DIR="$state" "$tool" create \
  --request-id sourcedb-orcl-recovery-20260718-001 \
  --requester patch-admin \
  --snapshot /u02/patches/39034528/evidence/topology-sourcedb-current.json \
  --policy /u02/patches/39034528/evidence/readiness-policy-standalone-v1.json \
  --database ORCL \
  --backup-parent /u02/opu-backup/ORCL \
  --window-start 2026-07-18T04:00:00Z \
  --window-end 2026-07-18T06:00:00Z

sudo -n env OPU_RECOVERY_PREP_STATE_DIR="$state" "$tool" approve \
  --request-id sourcedb-orcl-recovery-20260718-001 \
  --actor dba-approver \
  --approval-ticket TEST-ORCL-RECOVERY-001

sudo -n env OPU_RECOVERY_PREP_STATE_DIR="$state" "$tool" authorize \
  --request-id sourcedb-orcl-recovery-20260718-001 \
  --actor patch-operator

sudo -n env OPU_RECOVERY_PREP_STATE_DIR="$state" "$tool" execute \
  --request-id sourcedb-orcl-recovery-20260718-001 \
  --actor patch-operator
```

## Fixed execution sequence

Execution revalidates the sealed documents, snapshot age, open window, live
database identity, SPFILE, owner, listener count, target state, and backup
capacity. It also rejects backup storage that overlaps the Oracle home,
Central Inventory, or database-file directories. It then performs only the
following fixed operations:

1. generate a fixed RMAN command file, run Oracle RMAN `CHECKSYNTAX CMDFILE`
   against it, and record the syntax exit code before any service is stopped;
2. inventory the services exposed by the one discovered target-home listener,
   then stop it if it was running;
3. `SHUTDOWN IMMEDIATE`;
4. archive the quiesced Oracle home, Central Inventory, and `oraInst.loc`;
5. `STARTUP MOUNT` and prove the same DBID in `MOUNTED/NOARCHIVELOG` state;
6. execute the syntax-checked file as one RMAN `CMDFILE` job, recording the
   process exit code, and create a forced compressed level-0 database backup
   with logical-block checking plus explicit current-control-file and SPFILE
   backup sets under the unique root;
7. open the database, restore only the original listener state, register, and
   prove the original listener services and database instance are `READY`;
8. publish a sealed preparation manifest and exact `SHA256SUMS` atomically;
9. collect a fresh post-backup topology snapshot and reconciliation; and
10. run `opu-recovery-evidence-collect`, including exact backup-set and
   `RESTORE DATABASE VALIDATE` checks.

Operator-supplied SQL, RMAN text, archive flags, filenames, and arbitrary
commands are not accepted.

## Filesystem capacity admission

Preparation writes the new recovery set to its sealed backup parent. The
readiness policy can explicitly select filesystem recovery storage:

```json
{
  "recovery": {
    "storage_mode": "filesystem",
    "capacity_basis": "rman_unused_blocks",
    "minimum_filesystem_free_bytes": 10737418240
  }
}
```

These fields are part of the policy digest sealed into the request. Omitted
`capacity_basis` defaults to `allocated`; an omitted filesystem reserve is
zero. The reserve must be a nonnegative JSON integer. Selecting a capacity
basis does not waive backup, validation, approval, or maintenance-window
requirements. The example reserve is 10 GiB; choose a reserve appropriate to
the filesystem and workload before requesting approval.

The default capacity calculation includes every allocated datafile byte.
The opt-in `rman_unused_blocks` basis subtracts only measured free extents
from files whose eligibility can be proved: `COMPATIBLE` is at least 10.2,
there are no guaranteed restore points, the datafile is locally managed,
and its dictionary identity, allocation, and visibility match the complete
current `V$DATAFILE` inventory. A file with missing or ineligible metadata
receives its full allocated budget. Incomplete inventory coverage falls back
to the full database allocation. Contradictory or malformed measurements
block admission. This follows Oracle's documented
[unused-block compression conditions](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/rman-backup-concepts.html)
for the fixed level-0 backup set on a DISK channel.

No binary compression ratio, historical backup size, segment-only estimate,
or sparse-file disk allocation supplies a discount. Oracle home and Central
Inventory archives are budgeted using apparent input sizes and archive
headers. The calculation also includes control-file and SPFILE bytes, adds
20% overhead rounded upward, then adds the sealed filesystem reserve.
Execution measures available filesystem bytes again before creating the
backup root. Admission is a preflight measurement, not a reservation of
filesystem space; other writers can consume free space afterward.

`analyze --request-id ID` performs the live identity, storage location, and
capacity probes using disposable scratch files. It does not update request
state, append request evidence, acquire a mutation lock, or change Oracle
services. Its JSON `capacity` field includes requested and effective basis,
allocated bytes, per-file measurements and fallback reasons, provable unused
bytes, archive budgets, overhead, reserve, required bytes, and available
bytes. Capacity rejection returns `status: "blocked"`, the same measurements,
and exit code 2. Execution repeats the checks under the shared host lock and
seals the measurements into `execution.capacity`, retaining the raw probe
and its digest. The backup remains forced and independently validated against
the exact new pieces; capacity eligibility does not reduce backup coverage.

Listener registration waits for up to 120 seconds by default. Environments
that require a different bounded interval can set
`database.listener_registration_timeout_seconds` in the sealed readiness
policy to a value from 10 through 600 seconds.

## Failure behavior

Before mutation, invalid inputs are blocked without service changes. Once the
listener or database transition begins, every error invokes the fixed service
restoration path. The terminal states distinguish:

- `failed_services_restored`: backup/preparation failed, but the database and
  original listener state were restored;
- `validation_failed`: the prepared artifacts failed independent validation,
  while service health was restored; and
- `recovery_required`: automatic restoration could not prove the database and
  listener healthy.

An `INCOMPLETE` marker remains for every unsuccessful root. A failed root is
never retried or reused. Maintenance-window expiry never prevents restoration
of services already taken down.

The mutable request record persists each execution phase and its update time,
including `validating_rman`, `quiesce`, `archive`, `mount`, `backup`,
`restore_services`, `sealing_evidence`, `validating_recovery`, and
`completed`. Syntax and RMAN execution logs and exit-code files are retained
as request evidence. RMAN scripts contain only RMAN language; SQL*Plus
`WHENEVER SQLERROR` directives are never emitted into them.

Execution holds the shared host mutation lock used by the patch adapters as
well as one atomic recovery lock per database and Oracle home. RMAN inherits
the host lock so a surviving backup process continues excluding another
operation. SQL*Plus `STARTUP`/`STARTUP MOUNT` and listener-start commands close
their copy of descriptor 7; permanent Oracle services cannot retain that
lock after the supervising recovery process exits. The parent retains it
throughout service restoration and evidence validation.

If the worker is terminated, `reconcile` first proves that its recorded PID is
no longer alive and acquires the same shared host lock. A surviving RMAN job
or another patch operation therefore blocks reconciliation. Reconciliation
runs only the fixed service-restoration path and records either
`failed_services_restored` or `recovery_required`; it never resumes RMAN work
from an unknown point.

The resulting `recovery_evidence` path and digest, not the preparation request
alone, are supplied to `opu-patch-plan`.


## Application workflow

Live recovery is available through `POST /api/recovery`. The body contains
`request_id`, `requester`, a configured `host_id`, a discovered `database`, an
existing absolute `backup_parent`, UTC `window_start` and `window_end`, and an
optional complete readiness `policy`. Without an explicit policy, the saved
host policy is used. A successful readiness evaluation is not required to
prepare the missing backup. The application validates the full policy contract,
snapshot freshness, exact standalone database/home/owner/SID, and configured
host binding before submitting remote work. The native adapter repeats its
admission checks before downtime.

From the host's Recovery stage:

1. Create a preparation request and inspect its live capacity analysis.
2. Have a different actor approve it with an approval ticket, then a third actor
   authorize it. The authenticated principal supplies the real actor identity.
3. Execute the approved recovery request during its maintenance window. Backup,
   service restoration, independent backup checks, and RMAN
   `RESTORE DATABASE VALIDATE` belong to this one native preparation operation.
4. Select **Validate for patch planning**. The application requires a successful
   detached wrapper, native completion, and unchanged imported evidence. It then
   independently recollects recovery evidence against the current host snapshot.
5. Evaluate readiness, inspect the patch plan, obtain the separate patch
   approval and authorization, execute the patch, and inspect final database and
   listener validation.

Restore validation checks RMAN's ability to read and validate the required
backup pieces. It is not a restored database on another host and must not be
presented as a completed disaster-recovery rehearsal. The initial live adapter
is standalone PRIMARY, READ WRITE, NOARCHIVELOG with an SPFILE; other topologies
remain subject to their existing admission limits.

Selecting recovery evidence exposes the sealed preparation policy as an
explicit draft choice. It never silently replaces the saved readiness policy.
A failed selection invalidates prior recovery and readiness authority. Each
readiness refresh recollects the selected recovery set against the new snapshot;
request completion alone does not waive freshness or restore-validation gates.

## Application disconnect handling

Backup execution and service reconciliation both run in separate detached
remote wrappers. Their unique run directories and action identities are
persisted before SSH launch. A lost response is an unknown result, never an
instruction to submit another backup. `/api/runs/<run_id>/reconcile` inspects
that same persisted wrapper and does not restart services.

An explicit `/api/recovery/<request_id>/reconcile` may start the native service
restoration operation only after the original wrapper is proven dead and the
sealed authorization actor matches. Missing, malformed, or mismatched PID
records remain unknown. Reconciliation itself has its own durable launch and
cannot be resubmitted after a disconnect. The native executor additionally
requires the shared host lock, preventing restoration while an orphaned RMAN
process still owns it. Recovery-required outcomes remain failures even when
the reconciliation wrapper exited successfully.

`GET /api/recovery` reports capability and the production certification gate.
Capability indicates that the application implementation is available; it is
not host compatibility, a completed backup, live-lab verification, or production
approval. Regression tests use mocked SSH and isolated native fixtures. They
cover the application control sequence, immutable target/input bindings, stale
and malformed policy rejection, detached backup/reconciliation failures, and
selection of validated evidence. No live database mutation is part of these
automated tests.
