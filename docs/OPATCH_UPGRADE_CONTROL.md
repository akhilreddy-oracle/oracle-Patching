# Controlled OPatch upgrade

`bin/opu-opatch-upgrade` is the bootstrap worker used when the target patch
requires a newer OPatch than the discovered Oracle home contains. It is not an
Oracle RU executor and cannot apply a database patch.

The worker accepts only a ZIP with a caller-supplied SHA-256, plus an already
extracted staging directory containing `OPatch/opatch`. `create` records the
ZIP digest, the complete staged OPatch tree digest, the Oracle-home owner,
current and required OPatch versions, and a dedicated backup location. The
record is integrity-sealed. `approve` requires an actor other than the
requester. Every later action verifies the seal again.

`apply` runs only as root, refuses to proceed while an executable from the
target Oracle home is running, moves the original `OPatch` directory to the
recorded backup location, installs the staged copy as the discovered home
owner, and verifies the required version. A failed replacement or failed
verification attempts to restore the original directory before returning
failure. Failed restoration remains an unresolved replacement that requires
operator recovery; it is never reported as restored.

When an Oracle-home JDK is present, the worker invokes OPatch with
`-jdk $ORACLE_HOME/jdk`. This avoids relying on a bundled JRE that may not run
on a newer operating system.

`rollback` also requires a stopped Oracle home and verifies the captured
directory against its sealed digest before restoring it. It is only available
after a successful apply.

For a standalone database, `quiesce` dynamically requires exactly one PMON
owned by the target Oracle-home owner whose executable resolves to that home's
`bin/oracle`, and at most one listener from that home. It saves the SID and
listener in a sealed `quiescing` record before `shutdown immediate`, then stops
the listener and verifies that no target home service remains. The resulting
logs are sealed into the `quiesced` request.

`resume` restores those recorded services after an apply, rollback or
interrupted quiesce. Its sealed `resuming` record survives a disconnect; retry
checks the database and listener first and starts only a missing service.
Ambiguous service topology fails closed. Completion after an interrupted
quiesce means services were restored; an OPatch upgrade is established only
by the separate successful `apply` record.

Each request has an exclusive lock; service and binary changes also take the
shared native host mutation lock. State changes are sealed before atomic
replacement of the request record. `applying` and `rolling_back` are written
before directory replacement starts, and an interrupted replacement cannot
resume services until its result is reconciled by an operator. Startup
commands close inherited lock descriptors so persistent database/listener
processes cannot retain a completed worker's locks.

For the staged standalone lab artifacts, the sequence is:

```bash
sudo -n /home/opc/patching-oracle/bin/opu-opatch-upgrade create \
  --request-id sourcedb-opatch-20260716-001 \
  --requester patch-admin \
  --oracle-home /u01/app/oracle/product/19c/dbhome_1 \
  --stage-dir /u02/patches/39034528/opatch \
  --artifact /u02/patches/39034528/p6880880_190000_LINUX.zip \
  --artifact-sha256 d46f44eea6854fd775f4f5772da92e183dafd06a31713ceb160e919163cfd1b5 \
  --required-version 12.2.0.1.51 \
  --backup-root /u02/opu-backup/ORCL/opatch
```

At this point `analyze` is safe and read-only. Do not call `approve` or
`apply` until the patch plan has passed its recovery, compatibility, and
maintenance-window gates.
