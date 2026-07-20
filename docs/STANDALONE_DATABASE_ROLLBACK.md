# Standalone Database Rollback

`opu-database-single-instance-rollback` executes only an independently
approved `database_single_instance_opatch_rollback` plan. It does not accept a
database name, Oracle home, patch path, patch ID, OPatch arguments, or SQL from
the operator.

## Authority boundary

Create rollback from a fully succeeded standalone apply plan:

```bash
opu-patch-plan create-rollback \
  --plan-id ORCL-RU-ROLLBACK-002 \
  --requester rollback-admin \
  --source-plan-id ORCL-RU-APPLY-001 \
  --window-start 2026-07-20T06:00:00Z \
  --window-end 2026-07-20T08:00:00Z
```

The controller verifies the source plan seal and succeeded state, its sealed
`final_validate` evidence and logs, known binary outcome, target, artifact,
recovery record, and patch README reference. It derives the fixed rollback
procedure and writes a new plan. A different approver and a different operator
must then run `approve`, `authorize`, and `dispatch` for this rollback plan.

## Fixed stages

1. `rollback_precheck` proves the patch is installed, the latest SQL patch
   action is `APPLY/SUCCESS`, the target is healthy, OPatch meets the README
   requirement, recovery evidence is unchanged, and OPatch rollback metadata
   exists.
2. `rollback_binary` rechecks every sealed input, shuts down the database and
   listener, proves the Oracle home is quiesced, and runs only
   `opatch rollback -id <sealed patch ID>`. It verifies the patch is absent
   before restoring the database and listener.
3. `rollback_binary_validate` proves binary absence and service health.
4. `rollback_datapatch` refuses to run while the binary patch is present, then
   runs the Oracle-home `datapatch -verbose`.
5. `rollback_final_validate` requires binary absence, PRIMARY/READ WRITE/OPEN
   health, listener readiness, the policy-bound invalid-object threshold, and
   the latest matching `DBA_REGISTRY_SQLPATCH` action to be
   `ROLLBACK/SUCCESS`.

Run only the next sealed task:

```bash
task_json=$(opu-patch-plan next --plan-id ORCL-RU-ROLLBACK-002)
task_id=$(printf '%s' "$task_json" | jq -r '.task_id')

opu-database-single-instance-rollback execute \
  --plan-id ORCL-RU-ROLLBACK-002 \
  --task-id "$task_id" \
  --actor rollback-worker \
  --lease-seconds 3600
```

If OPatch begins but inventory cannot prove whether rollback completed, the
worker records `binary_state_unknown`, leaves the plan paused, and does not run
datapatch or retry. Oracle-home or RMAN restoration is a separate recovery
adapter and is never an automatic consequence of a failed rollback.
