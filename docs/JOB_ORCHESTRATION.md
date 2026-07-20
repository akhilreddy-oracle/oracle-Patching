# Patch Job Orchestration

`opu-jobctl` provides the first durable job-control layer for the fixed
`grid.rolling.patch.v1` job type. It is intentionally not a generic scheduler:
every job uses a registered topology, a numeric patch ID, an immutable node
list, and typed task stages.

## Lifecycle

```text
draft -> prechecking -> awaiting_approval -> scheduled -> running -> succeeded
                                                   \-> paused
```

A requester cannot approve their own job. `ready` requires the SHA-256 digest
of the precheck evidence, so an approval is associated with recorded evidence.
The plan is integrity-checked before every state read. Starting a job emits
sequential `apply` and `validate` tasks for every node in the submitted order.

An executor must atomically claim a task before completing it. Claims have a
bounded lease (30–3600 seconds), are tied to the executor identity, and task
completion requires an evidence digest. If a lease expires, `reconcile` marks
the outcome unknown and pauses the job; it never retries a destructive task.

## Example

```bash
export OPU_JOB_STATE_DIR=/var/lib/oracle-patching-jobs

bin/opu-jobctl create --job-id grid-20260721-001 \
  --requester patch-admin --topology grid-rolling \
  --patch-id 12345678 --patch-dir /stage/12345678 --nodes /root/grid-nodes
bin/opu-jobctl submit --job-id grid-20260721-001 --actor patch-admin
bin/opu-jobctl ready --job-id grid-20260721-001 --actor precheck-agent \
  --evidence-sha256 <precheck-evidence-sha256>
bin/opu-jobctl approve --job-id grid-20260721-001 --actor dba-approver
bin/opu-jobctl start --job-id grid-20260721-001 --actor patch-operator
bin/opu-jobctl claim --job-id grid-20260721-001 --actor grid-agent-01
bin/opu-jobctl complete --job-id grid-20260721-001 \
  --task-id 001-apply-node-a --actor grid-agent-01 --status succeeded \
  --evidence-sha256 <task-evidence-sha256>
```

The `next` result is a typed task descriptor, not a shell command. The next
slice leases these descriptors to managed host agents and requires evidence
and postcondition reconciliation before a destructive retry.
## Registered job types

- `grid-rolling` creates `grid.rolling.patch.v1` tasks: per-node `apply`, then
  per-node `validate`.
- `database-rolling` creates `database.rolling.patch.v1` tasks: per-node
  `apply` and `validate`, followed by cluster `datapatch` and final database
  validation. It requires `--database` and `--grid-home` at creation.

Tasks are deliberately globally serial within a rolling job. A worker must
complete the claimed task with evidence before another worker can claim the
next task; an expired lease pauses the job for human reconciliation.
