# Immutable Plan Control

`opu-patch-plan` is the control-plane boundary between prechecks and any
execution adapter. It does not accept a node list, Oracle home, patch ID,
artifact directory, or shell command from an operator.

## Create

Creation consumes only evidence documents produced by earlier pipeline stages:

```bash
opu-patch-plan create \
  --plan-id grid-20260717-001 \
  --requester patch-admin \
  --readiness /evidence/readiness.json \
  --reconciliation /evidence/topology-reconciliation.json \
  --artifact-manifest /evidence/artifact.json \
  --procedure-validation /evidence/procedure-validation.json \
  --compatibility /evidence/compatibility-reconciliation.json \
  --policy /evidence/readiness-policy.json \
  --recovery-evidence /evidence/recovery-ORCL.json \
  --window-start 2026-07-20T01:00:00Z \
  --window-end 2026-07-20T05:00:00Z
```

The controller verifies every evidence SHA-256 recorded by the readiness
result. It derives the active node list, patch ID, target family, procedure
method, minimum OPatch version, and rollback declaration from those documents.
For Database plans it also derives the exact database, Oracle home, owner, and
sealed recovery manifest. RAC plans additionally derive the Grid home,
coordinator, and reconciled rolling order. Grid plans bind their Grid recovery
record. The controller then writes an immutable `plan.json` with a canonical
`plan_sha256`.

The readiness result is not timeless authority. It seals each reconciled
snapshot path, SHA-256, normalized host, collection time, and policy-derived
expiry. Creation rejects missing, changed, future-dated, or expired snapshots,
and rejects a maintenance window that begins after the earliest snapshot
expiry.

A rollback cannot be created from the original readiness documents or by
changing an apply plan. `create-rollback` accepts only a new plan ID, requester,
the ID of a fully succeeded standalone or RAC Database apply plan, and a new
window. For standalone it derives the succeeded `final_validate` evidence. For
RAC it verifies the exact six tasks per source node plus coordinator datapatch
and final validation, including every task result, custodied evidence record,
and source actor. Both paths select the artifact README whose digest matches
the sealed Oracle reference and bind the complete source lineage into a new
`patch_rollback` plan.

## Authorization lifecycle

```text
awaiting_approval -> approved -> execution_authorized
```

`approve` requires a different identity from the requester and records a
change/approval ticket. `authorize` succeeds only while the declared UTC
maintenance window is open. Every state read re-verifies the immutable plan
hash. A changed plan is refused rather than re-approved or executed.

For apply plans, `approve`, `authorize`, and `dispatch` also re-hash the bound
topology snapshots and re-evaluate their policy expiry. Thus a previously
generated `ready_for_approval` document cannot be replayed after its discovery
evidence expires. Rollback plans are intentionally exempt from the original
apply-readiness expiry; they instead require sealed successful source results
and perform live recovery preconditions so recovery authority is not lost when
planning evidence ages out.

`execution_authorized` is intentionally not an execution result. Each managed
adapter consumes this exact plan digest, proves each task's preconditions, and
publishes structured postcondition evidence before a task lease can be
completed. For RAC rollback, the approver and operator must also differ from
every source-apply worker recorded in the sealed lineage.

## Typed rolling work

`dispatch` is the only way to turn an authorized plan into work. It derives
the order from the sealed `nodes` list and emits globally serial typed tasks.
Standalone apply materializes three local binary stages plus datapatch and
final validation. RAC Database apply materializes six stages per node
(`rac_precheck` through `rac_node_validate`) plus one coordinator datapatch and
final validation. Manual Grid apply materializes six stages per node
(`grid_precheck` through `grid_node_validate`) plus one coordinator final
validation. Every immutable task definition is bound to `plan_sha256`; `next`
returns a descriptor, not a command line or free-form payload.

Standalone rollback materializes a different five-stage adapter:
`rollback_precheck`, `rollback_binary`, `rollback_binary_validate`,
`rollback_datapatch`, and `rollback_final_validate`. It receives a new approval
ticket and operator authorization; apply approval is never reused.

RAC rollback reverses the sealed apply node order and materializes six stages
per node (`rac_rollback_precheck` through `rac_rollback_node_validate`), then
one coordinator `rac_rollback_datapatch` and
`rac_rollback_final_validate`. Grid rollback is now an immutable-plan
adapter: it reverses the sealed apply node order, materializes five stages
per node (`grid_rollback_precheck` through `grid_rollback_node_validate`, skipping
`grid_rootadd_rdbms`), then one coordinator
`grid_rollback_cluster_final_validate`.

## Leases and task evidence

`claim` can claim only the next pending descriptor, takes a lease bounded by
the maintenance window, and rejects any parallel claim. `renew` preserves the
same boundary. Managed `complete` requires a sealed evidence document whose
plan, task, stage, actor, target, patch, recovery evidence, log digests, exit
code, and postcondition all match the controller state. The controller copies
the evidence closure into task-local custody and seals its manifest before the
next task can run. Failed work pauses the plan. `reconcile` turns an expired
task into `unknown` and pauses the plan; it deliberately does not return the
task to pending or invoke a retry.

Executors keep renewing while they capture and seal stage evidence, then stop
and join the heartbeat and synchronously renew before handing completion to
the controller. A renewal failure during that handoff preserves the evidence
and blocks completion. A heartbeat failure already recorded as an unknown
stage outcome retains the existing failed-evidence reconciliation path.

`complete` records `completion_admitted_at_epoch` when it acquires the exclusive
plan task lock. It checks lease ownership at that admission time and retains
the lock throughout evidence verification and custody, preventing a competing
renewal, reconciliation, or claim. `completed_at_epoch` records the later
verified persistence time. For new results, `completed_after_lease` means the
lease had expired at admission; verification duration alone cannot make an
on-time completion late. Historical results without an admission timestamp
retain their original recorded meaning and are not rewritten.

Renewal likewise checks the existing lease at lock admission, then rechecks the
actual current maintenance window before persisting a bounded new expiry.
It cannot revive ownership already expired at admission. A new claim requires
at least 30 seconds left in the window. An already-valid owner may renew with
as little as one second left; that renewal still ends at the sealed window
deadline and cannot succeed once the window closes.

Dispatch now records a sealed manifest of the complete expected task set and
atomically reserves every target host. Overlapping plans, including database
and Grid plans, cannot dispatch concurrently. Reservations persist across
controller restarts, failed tasks, paused plans, and expired leases. They are
released only after every expected task and its custodied evidence verify as
successful. An old active plan without a task manifest or target reservation
is blocked; an operator must reconcile its actual Oracle state before creating
fresh execution authority. There is no timeout-based reservation release.

`retry-task` verifies the failed attempt's permanent controller custody first.
It permits a failure reporting no mutation, or an explicitly repeatable
validation/datapatch stage with a known binary state. Unknown postconditions,
heartbeat failures, a database left down, and recovery-required outcomes remain
blocked. Each retry receives a sealed generation and a new `TASK_ID-retryN`
directory; generation zero retains the original `TASK_ID` directory. Previous
task results are preserved under `attempts/`, and previous logs and evidence
are never deleted. Evidence from an earlier generation cannot complete a new
claim. `task-status --plan-id ID --task-id ID` returns a verified task and
rechecks terminal controller custody for recovery and queue reconciliation.

Controller live completion and detached-run reconciliation additionally compare
the verified task's definition hash and retry generation with the launch record.
A valid result from another attempt cannot resolve the earlier run. Legacy
launch records missing this binding remain unresolved and require operator
investigation; this check does not infer a match from the current task status.

Native UI execution captures one host-inventory snapshot before its first live
task and retains it through all remaining tasks. Per-task preflight and target
selection use that same snapshot. This prevents a configuration edit during the
operation from redirecting later tasks; it does not add a click-time approval
binding to legacy native UI requests. Chat confirmations retain their separate
admission and worker-start binding checks.

All managed mutation adapters also share a host-local kernel lock, independently
of their plan and adapter state directories. The mutation child retains that
lock if its supervisor dies. The production lock defaults to
`/var/lib/oracle-patching-utility/locks/host-mutation.lock`; deployments that
override `OPU_EXECUTION_LOCK_DIR` must use the same directory for all adapters.

Remote executors receive a per-plan sealed reservation receipt alongside the
task manifest and run with `OPU_PLAN_WORKER_SNAPSHOT=1`. That mode permits only
existing dispatched task execution and inspection; it cannot create plans,
approve, authorize, dispatch, or issue retries. Worker completion does not
change the controller's global reservation registry. After importing a
successful worker snapshot, the controller runs `reconcile --plan-id ID
--actor ACTOR` to verify every task, mirrored custody file, and completion audit
event before releasing the reservation. Original remote absolute paths remain
sealed in the records; custody verification resolves their files only inside
the corresponding mirrored plan evidence directory.

`dispatch`, `claim`, and `renew` all recheck the open UTC window. The executor
rechecks it before mutation. Once OPatch has started, service
restoration is allowed to finish even if the window closes. The controller may
record sealed managed evidence for up to 15 minutes after lease expiry, but it
pauses the plan instead of authorizing the next stage.
