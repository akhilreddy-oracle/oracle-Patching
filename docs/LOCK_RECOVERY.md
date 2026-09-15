# Managed standalone lock recovery

`opu-database-lock-recover` handles one bounded interruption: binary apply is
sealed as succeeded, its next validate task remains unclaimed, and its detached
launch exited 75 with the exact host-lock rejection. It does not apply a patch,
run datapatch, claim or complete a task, revise an approval, or change plan state.

The root-only Linux interface is:

```text
opu-database-lock-recover inspect --plan-id ID --task-id ID --run-id 32_HEX_REMOTE_LAUNCH_ID --actor AUTHORIZER
opu-database-lock-recover recover --plan-id ID --task-id ID --run-id 32_HEX_REMOTE_LAUNCH_ID --actor AUTHORIZER
```

`OPU_PLAN_STATE_DIR` must equal the deployed tool root's `var/webapp-plans`.
The launch is resolved under that same root's `var/webapp-runs/PLAN/TASK/RUN`.
No lock path, PID, Oracle command, SQL, home, SID, listener, or test-mode override
is accepted by the CLI. Native plan status and task-status validate existing
seals and the completed apply evidence custody. The operation requires the
sealed authorizing actor and an open original maintenance window.

The existing controller tar sync preserves file ownership, so the mirrored
`authorization.json` and `approval.json` may be owned by the mirrored plan's
controller UID. Only those two documents receive this ownership allowance;
they must remain regular, single-link, non-symlink files without group/world
write permission. Their hashes alone do not grant authority. The exact
authorization file and record digests must match the protected native apply
`claim.json`; the actor, plan, attempt and successful evidence must match the
root-owned native apply `evidence.json` and the controller's verified custody.
Approval remains bound by its exact digests inside that anchored authorization.
Native records, launch proof, host locks and audit files retain root ownership
requirements. Recomputing seals on modified controller files cannot replace
the native execution's authority.

`/etc/oratab` is a crosscheck of the already bound home and SID, never a source
of execution scope. Its parent must be root-protected; the fixed file must be
regular, single-link and non-symlink. Root or the sealed Oracle account may own
it. World write is forbidden; group write is permitted only for that Oracle
account's primary group. Ownership or mapping failures include file UID, GID,
mode and inode metadata in the inspection report. No other input receives this
installer-file allowance.

Inspection binds the native target record to the plan, checks its unique oratab
mapping, checks current PRIMARY OPEN READ WRITE identity and listener readiness,
and reports extjob ownership/mode without changing it. It validates the fixed
host lock as a root-protected regular single-link inode, scans every accessible
process descriptor for that inode, and records PID, process start time, UID,
executable, argv and matching descriptors. Any process that cannot be inspected,
unrecognized holder, active Oracle executor/RMAN process, wrong target,
unfinished launch, altered record, or already claimed task blocks recovery.
Exact target-home Oracle background processes and its bound listener can be
classified from their kernel identities. Foreground sessions need additional
native database proof before they can be classified as inherited holders.

Exact target-home `oracleSID (LOCAL=NO)` foreground holders initially remain
unknown. The fixed read-only query maps
`V$PROCESS` to `V$SESSION`, reports process/session multiplicity and transaction
presence using both the session transaction address and `V$TRANSACTION`, and
includes every target USER session except the collector itself. An outer join
keeps USER sessions visible even if no process currently maps to them. It also
reports running `V$RMAN_STATUS` rows. Output includes sanitized client metadata,
never client SQL text. Programs and modules do not grant authority; a maintenance
indicator can only block recovery.

A foreground holder can become `database_session` only if its kernel PID/start
time maps uniquely to one native process address and one USER session SID/serial,
the session is DEDICATED and INACTIVE, and both transaction indicators are false.
Missing, duplicate, shared, pooled or otherwise ambiguous mappings block. Every
other target USER session must also be inactive, dedicated, unambiguous and
transaction-free; running RMAN work blocks the entire restart. These checks run
even if there are no foreground lock holders.

The same fixed query runs again immediately before the outage, between two
kernel-holder scans. Both kernel identities and each foreground session's stable
PID/start time/process address/SID/serial must match the original inspection.
Elapsed idle time and observation timestamps are excluded from the identity
comparison. A changed session, newly active work, transaction, maintenance client
or unknown holder prevents service changes.

Recovery first takes a separate global operation lock and repeats authority and
holder checks. It stops only the bound listener and shuts down only the bound
database with `shutdown immediate`. It acquires the existing host-lock inode
after those services release it; it never unlinks or replaces a lock, kills a
PID, or changes executable privileges. Every service subprocess closes inherited
file descriptors. Recovery restarts the database/listener, registers services,
verifies the same DBID and PRIMARY OPEN READ WRITE state, confirms the listener
is READY, and verifies no child retained the host lock.

An immutable audit directory is reserved before service changes at:

```text
/var/lib/oracle-patching-utility/lock-recovery/PLAN/RUN/
```

It contains the inspected evidence, native command logs, outage marker and sealed
`result.json`. The JSON digest is SHA-256 of the record without
`record_sha256`, serialized with sorted keys, compact separators and ASCII
escaping. Success is `status: completed` with original task status `pending`,
wrapper exit 75, preserved inode and verified `service_health`. The application
must also observe the detached recovery wrapper exit 0 before accepting it.

An existing attempt directory prevents every automatic retry, including after
disconnect, partial failure or missing terminal evidence. A failure remains
`recovery_required`. The guard can restore services while holding the normal
host lock; it will not race another executor or start services without exclusion.
An external lock race may therefore leave services stopped and require operator
reconciliation. If shutdown fails while the original, unchanged database-only
holders still retain that same lock, the guard may restore only the bound
listener; it does not start the database under that exception. The sealed plan
and original pending task remain unchanged.

Validation: `python3 -B tests/lock_recovery.py` uses fake procfs, real temporary
file-lock primitives and mocked native/Oracle commands; it never contacts a host
or runs Oracle. Live recovery is a separate explicitly authorized operation.

The app exposes operator-only `POST /api/plans/PLAN/lock-inspect` and
`POST /api/plans/PLAN/lock-recover` with the existing authorizing `actor` and
original application `run_id`. Both return a background run ID. The plan card
shows recovery only after a matching eligible inspection; recovery itself
repeats native eligibility checks. The original run records its maintenance
run reference before launch. Reconciliation reads that exact wrapper and audit,
without restarting services. Only verified success closes the original rejected
launch as failed before task claim; normal validation remains pending.

Controller validation: `python3 -B tests/lockctl.py`,
`python3 -B tests/webapp_control.py`, and `node --test tests/frontend.mjs`.
