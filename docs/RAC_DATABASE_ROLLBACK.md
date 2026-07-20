# RAC Database Rollback

RAC Database rollback is a new, independently approved child plan. It is not
a retry, continuation, or mutation of the original apply plan. The controller
derives every target, node, Oracle home, patch identifier, artifact digest,
recovery record, and Oracle README reference from a fully succeeded
`database_rolling_opatch` source plan.

## Eligibility

`create-rollback` fails closed unless the source apply plan and all of its
sealed results still verify. In particular, the source plan must have:

- exactly one succeeded `rac_precheck`, `rac_drain`, `rac_stop`,
  `rac_opatch_apply`, `rac_start`, and `rac_node_validate` task for every
  sealed source node;
- a succeeded coordinator `cluster_datapatch` task;
- a succeeded coordinator `cluster_final_validate` task;
- controller-custodied evidence with known binary state for every required
  source checkpoint;
- unchanged source documents, artifact, recovery evidence, and the exact
  patch README selected by its sealed digest; and
- an Oracle README that explicitly contains the fixed RAC stop, OPatch
  rollback, start, and `datapatch -verbose` procedure.

Unknown, paused, failed, partially applied, or tampered source plans are not
rollback authority. A completed source apply may be used after its original
readiness window expires: rollback creates a new maintenance window, verifies
the immutable source lineage and recovery evidence, and performs fresh live
rollback prechecks. A source plan with expired or changed recovery authority
still fails closed and requires a separately designed recovery assessment.

## Independent authorization

Create a new rollback plan and a new maintenance window:

```bash
opu-patch-plan create-rollback \
  --plan-id ORCL-RAC-RU-ROLLBACK-002 \
  --requester rollback-admin \
  --source-plan-id ORCL-RAC-RU-APPLY-001 \
  --window-start 2026-07-20T06:00:00Z \
  --window-end 2026-07-20T10:00:00Z
```

The rollback requester, approver, and authorizing operator must be different
identities. The rollback approver and operator must also differ from every
source-apply worker sealed into the lineage. The source apply approval and
authorization are never reused:

```bash
opu-patch-plan approve \
  --plan-id ORCL-RAC-RU-ROLLBACK-002 \
  --actor rollback-approver \
  --approval-ticket CHG-RAC-ROLLBACK-002

opu-patch-plan authorize \
  --plan-id ORCL-RAC-RU-ROLLBACK-002 \
  --actor rollback-operator

opu-patch-plan dispatch \
  --plan-id ORCL-RAC-RU-ROLLBACK-002 \
  --actor rollback-operator
```

## Fixed execution graph

Nodes are rolled back one at a time in the reverse of the sealed apply order.
For each node the controller emits:

1. `rac_rollback_precheck`
2. `rac_rollback_drain`
3. `rac_rollback_stop`
4. `rac_opatch_rollback`
5. `rac_rollback_start`
6. `rac_rollback_node_validate`

Only after every node validates does the coordinator run:

7. `rac_rollback_datapatch`
8. `rac_rollback_final_validate`

The worker accepts only the plan ID, next sealed task ID, actor, and bounded
lease. It constructs fixed `srvctl`, `opatch rollback -id`, SQL*Plus, and
`datapatch -verbose` argument vectors from the immutable plan; it never accepts
an operator-supplied command, node, Oracle home, patch path, patch ID, or SQL.

## Failure semantics

Before mutation, a failed precheck or service drain records no mutation and
pauses the child plan. Once OPatch rollback starts, any nonzero result or
ambiguous inventory creates a `binary_state_unknown` result. The affected
instance remains stopped, the controller pauses the plan, and neither
automatic instance startup nor datapatch is attempted.

A successful child plan requires patch absence in every RAC Database home,
all instances and required services healthy, one coordinator datapatch run,
and a latest matching `DBA_REGISTRY_SQLPATCH` action of
`ROLLBACK/SUCCESS`. Every task result, log, and artifact is copied into
controller custody and sealed before the next task may be claimed.
