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

`dispatch`, `claim`, and `renew` all recheck the open UTC window. The executor
rechecks it before mutation. Once OPatch has started, service
restoration is allowed to finish even if the window closes. The controller may
record sealed managed evidence for up to 15 minutes after lease expiry, but it
pauses the plan instead of authorizing the next stage.
