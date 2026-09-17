# Standalone Database Patch Execution

`opu-database-single-instance-patch` is the fixed-operation worker for a sealed
`database_single_instance_opatch` plan. Its CLI accepts only a plan ID, task ID,
actor, and bounded lease. Database name, Oracle home, owner, patch path, patch
ID, commands, and SQL cannot be supplied by the operator.

The current database adapters support **non-CDB databases only**. Readiness
and live execution require explicit `CDB=NO` evidence. CDBs and unknown
container scope block before mutation: root-only SQL patch results do not
prove that every PDB and the seed were patched. This restriction also applies
to RAC Database, OJVM, home switching, and their rollback adapters.

## Ordered stages

The controller materializes and executes these stages serially:

1. `precheck` revalidates every source document, backup checksum, artifact
   digest, authoritative artifact ARU platform ID/name/source digest,
   discovered Oracle-home platform evidence, Oracle-home owner, OPatch
   version, the separate platform-applicability and conflict prerequisites,
   database role/open state, SID mapping, and listener health. It writes a
   sealed target binding. ARU platform `226` (`Linux x86-64`) is not
   interchangeable with ARU `46` (`Linux x86`).
2. `apply` revalidates recovery, artifact, window, OPatch, platform binding,
   `CheckPatchApplicableOnCurrentPlatform`, conflict, and target binding
   immediately before the first outage. Only after those checks pass does it
   shut down the database and stop the bound listener. It proves the home is
   quiesced, re-runs applicability and conflict at the binary-apply boundary,
   applies the binary patch, verifies inventory, and restores the database and
   listener.
3. `validate` verifies binary inventory and service health.
4. `datapatch` verifies recovery and the open window again. If the installed
   tool documents `-local_inventory`, it uses fresh, validated OPatch XML from
   the sealed Oracle home with `datapatch -verbose -local_inventory XML`.
   Otherwise it uses ordinary `datapatch -verbose`. Oracle selects the SQL
   actions from the actual inventory. No inventory bypass, forced patch list,
   or automatic retry is used. The worker verifies the latest target SQL
   action, then runs the home-shipped `utlrp.sql` through `catcon.pl`.
5. `final_validate` requires binary inventory, PRIMARY/READ WRITE/OPEN health,
   listener readiness, the README-required extjob ownership/mode, zero invalid
   objects, every enabled registry component
   `VALID`, and the latest target-patch row `APPLY/SUCCESS` in
   `DBA_REGISTRY_SQLPATCH`. Historical successes cannot mask a later rollback
   or failure. The rollback worker uses the same native SQL path and requires
   the latest target row to be `ROLLBACK/SUCCESS`.

Native SQL logs and local-inventory inputs are created in a private directory
accessible to the Oracle owner, then copied and hashed into the sealed task
evidence. Database and listener startup commands close the inherited host
lock descriptor; the supervisor, OPatch, datapatch, probes and shutdown keep
it. This prevents persistent services from retaining a completed task's lock.

A hash-bound README that explicitly requires
`chown root $ORACLE_HOME/bin/extjob` and `chmod 4750` activates a read-only
ownership/mode check before outage, after binary apply, and during final
validation. Missing or changed sealed README evidence blocks the check. It
opens only the
sealed home's regular, single-link extjob file without following symlinks.
The worker never promotes existing Oracle-writable bytes to a root setuid
executable: a README hash proves instructions, not executable provenance.
If ownership or mode is wrong, the task fails for a separately reviewed repair
with trusted binary provenance. If OPatch changed those permissions, this
failure occurs after binary apply while services remain stopped. No automatic
root permission repair is included. Test fixtures model this check using
their current UID and ordinary mode `0750`; they never create setuid files.

Run only the next controller-issued task:

```bash
task_json=$(sudo -n env OPU_PLAN_STATE_DIR=/path/to/plan-state \
  /home/opc/patching-oracle/bin/opu-patch-plan next --plan-id PLAN_ID)
task_id=$(printf '%s' "$task_json" | jq -r '.task_id')

sudo -n env OPU_PLAN_STATE_DIR=/path/to/plan-state \
  /home/opc/patching-oracle/bin/opu-database-single-instance-patch execute \
    --plan-id PLAN_ID \
    --task-id "$task_id" \
    --actor standalone-patch-worker \
    --lease-seconds 3600
```

Repeat only after the previous command returns sealed `succeeded` evidence.
Any command, lease, evidence, postcondition, or Oracle ambiguity pauses the
plan. The worker never retries an ambiguous mutation and never performs an
automatic binary rollback. Rollback requires its own newly sealed plan,
recovery decision, maintenance window, and independent approval. See
[Standalone Database rollback](STANDALONE_DATABASE_ROLLBACK.md).

Compatibility evidence is deliberately two-part. For the exact sealed
`node:home`, the plan contains independent results for
`CheckPatchApplicableOnCurrentPlatform` and
`CheckConflictAgainstOHWithDetail`, each with its native return code and log
SHA-256. The artifact, procedure, discovered Oracle home, readiness result,
plan, and task target must all carry the same numeric platform ID. A filename
such as `LINUX.zip` is never accepted as proof of platform suitability.

If either prerequisite fails during the apply-stage pre-outage recheck, the
task returns failed evidence with `outcome_class: no_mutation`; the database
and listener remain running. This is a safe blocked result. Do not bypass the
gate, reuse an older compatibility document, or advance to binary apply.

Execution evidence includes an `outcome_class`. If OPatch was invoked but the
result cannot be proven from inventory, the task is sealed as
`binary_state_unknown`; the plan is paused and the database is deliberately
not advanced to datapatch or an automatic retry.
