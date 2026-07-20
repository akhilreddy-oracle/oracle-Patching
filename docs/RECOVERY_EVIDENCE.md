# Recovery Evidence

`opu-recovery-evidence-collect` converts a selected, already-created recovery
set into immutable execution evidence. It never creates, deletes, restores, or
crosschecks a backup. For a controlled standalone `NOARCHIVELOG` creation
workflow, use `opu-database-recovery-prepare` first; see
[Standalone Database Recovery Preparation](RECOVERY_PREPARATION.md).

For a standalone database it requires:

- a current topology snapshot that uniquely maps the database to its Oracle
  home, owner, and running SID;
- a non-symbolic-link backup root containing `SHA256SUMS`;
- exactly one Oracle-home tar archive in that checksum manifest; and
- one or more available RMAN backup sets whose pieces are stored below the
  selected root.

The collector reruns every checksum, lists the Oracle-home archive, resolves
the RMAN backup-set primary keys from Oracle structured views, and executes
`VALIDATE BACKUPSET` for every selected key plus `RESTORE DATABASE VALIDATE`.
Before connecting to the target it checks the complete generated command file
with RMAN `CHECKSYNTAX CMDFILE`; it then executes that file as one RMAN
`CMDFILE` job. Both process exit codes and both log digests are sealed into the
result. The owner-readable runtime copy is created with mode `0400` outside the
backup root and removed on success or failure, while the audit copy remains in
the evidence directory. RMAN command files never contain SQL*Plus
`WHENEVER SQLERROR` directives.
It requires a full or level-0 base for every current datafile, control-file and
SPFILE coverage, and proves that every RMAN-validated piece is under the
approved root and present in `SHA256SUMS`. Oracle errors, missing validation
completion messages, unchecksummed or outside-root pieces, symbolic links, or
ambiguous target facts fail closed.

Recovery roots produced by `opu-database-recovery-prepare` additionally carry
a sealed `PREPARATION.json`. The collector verifies its canonical record,
request/target binding, Oracle-home archive, Central Inventory archive, and
retained `oraInst.loc`, while preserving compatibility with legacy roots that
contain exactly one Oracle-home archive.

Example:

```bash
sudo -n /home/opc/patching-oracle/bin/opu-recovery-evidence-collect \
  --snapshot /u02/patches/39034528/evidence/topology-current.json \
  --database ORCL \
  --backup-root /u02/opu-backup/ORCL/20260716T205505Z \
  --output /u02/patches/39034528/evidence/recovery-ORCL.json
```

The result contains the exact file digests, selected RMAN keys, verification
log digests, RMAN syntax and execution exit-code evidence, source-snapshot
digest, and a canonical `record_sha256`. It also
records the selected recovery set's observation time, the oldest full/level-0
datafile backup completion time, the sealed age in seconds, restore-selected
piece handles, and the selected datafile backup-set keys. The plan controller
recomputes this exact age at creation, approval, authorization, and dispatch;
an unrelated newer backup cannot make an older selected root appear current.
Database execution plans must bind both the recovery-evidence file digest and
its internal record digest. A successful discovery backup-age check by itself
is not sufficient authorization to mutate an Oracle home.

## Grid Infrastructure recovery evidence

`opu-grid-recovery-evidence-collect` validates an already prepared Grid
Infrastructure recovery bundle. It does not create OCR or OLR backups and it
does not restore Clusterware. A separate controlled preparation procedure must
create the bundle before this read-only collector can seal it.

The bundle is accepted only when it contains:

- a healthy topology snapshot with every reconciled cluster node `Active` and
  Clusterware upgrade state `NORMAL`;
- the exact Grid home, Central Inventory, and `oraInst.loc` target identity;
- nonempty, readable archives of the Grid home and Central Inventory;
- an OCR automatic or manual backup suitable for `ocrconfig -restore` (an
  `ocrconfig -export` file is not interchangeable with an OCR backup);
- exactly one node-local OLR backup for every reconciled cluster node; and
- one exact checksum manifest containing only those recovery artifacts.

The collector reruns every checksum, rejects paths that resolve outside the
sealed backup root, rejects unsafe archive member paths, compares the retained
`oraInst.loc` with the live file, and reads the OCR and every OLR backup with
`ocrdump`. It also captures live `crsctl check crs`, active version/upgrade
state, `ocrcheck`, and voting-disk evidence. The result binds the prepared
bundle's canonical record, every validation log, and a second canonical
`record_sha256` under the `oracle.grid.recovery.evidence` collector identity.

Example:

```bash
sudo -n /home/opc/patching-oracle/bin/opu-grid-recovery-evidence-collect \
  --snapshot /evidence/topology-grid-current.json \
  --bundle /recovery/grid/run-001/bundle.json \
  --output /evidence/recovery-grid-run-001.json
```

Grid plans must bind both the resulting file digest and internal record digest.
The per-node OLR set must exactly match the plan's reconciled node set.
