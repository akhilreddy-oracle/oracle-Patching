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

Execution holds one atomic lock per database and Oracle home. If the worker is
terminated, `reconcile` first proves that its recorded PID is no longer alive,
then runs only the fixed service-restoration path and records either
`failed_services_restored` or `recovery_required`; it never resumes RMAN work
from an unknown point.

The resulting `recovery_evidence` path and digest, not the preparation request
alone, are supplied to `opu-patch-plan`.
