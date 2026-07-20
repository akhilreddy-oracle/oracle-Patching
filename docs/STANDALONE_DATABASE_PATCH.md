# Standalone Database Patch Execution

`opu-database-single-instance-patch` is the fixed-operation worker for a sealed
`database_single_instance_opatch` plan. Its CLI accepts only a plan ID, task ID,
actor, and bounded lease. Database name, Oracle home, owner, patch path, patch
ID, commands, and SQL cannot be supplied by the operator.

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
4. `datapatch` verifies recovery and the open window again, then invokes the
   Oracle-home `datapatch -verbose` against the sealed SID.
5. `final_validate` requires binary inventory, PRIMARY/READ WRITE/OPEN health,
   listener readiness, the policy-bound invalid-object threshold, and a clean
   successful target-patch row in `DBA_REGISTRY_SQLPATCH`.

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
