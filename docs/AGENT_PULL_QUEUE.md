# Lab pull-agent work queue

Filesystem-backed pull queue that approximates the architecture doc's agent
pull model without PostgreSQL yet. Sealed plan executors remain the only
mutation boundary; the queue only distributes claims and records outcomes.

## Layout

- Jobs: `webapp/var/agent-queue/jobs/<plan>__<task>.json` (override with
  `OPU_AGENT_QUEUE_DIR`).
- Enrollment registry: `webapp/var/agent-registry/agents.json` (override with
  `OPU_AGENT_REGISTRY_FILE`). Tokens are stored as SHA-256 digests only; the
  plaintext token is printed exactly once at enroll time.

## Flow: enroll → publish → claim → run → complete

1. Enroll an agent identity (once per agent):
   `opu-agent-enroll enroll --node NAME --agent-id ID` → save the returned
   `agent_token`. Manage with `opu-agent-enroll list` / `revoke --agent-id ID`.
2. After dispatch, `POST /api/plans/{id}/publish-agent-queue` (or
   `planctl.publish_agent_queue`) enqueues pending sealed tasks.
3. Claim only (no execution): `opu-agent-work-pull --node NAME --agent-id ID
   [--agent-token TOKEN]` or `POST /api/agent/claim`.
4. Claim and execute in one step:
   `OPU_PLAN_STATE_DIR=... opu-agent-work-run --node NAME --agent-id ID
   [--lease-seconds N] [--queue-dir DIR] [--agent-token TOKEN]`.
   The tool maps the job's adapter through the shared executor registry,
   renews the configured queue lease while the executor runs, records durable
   launch admission before creating the subprocess, runs
   `<executor> execute --plan-id ... --task-id ... --actor <agent-id>`, and
   completes the job with the executor exit code and a stdout SHA-256 digest.
   Unknown adapters and a missing `OPU_PLAN_STATE_DIR` fail closed; a
   non-zero executor exit is completed only after verifying the native task's
   terminal evidence. An unclaimed pending task is deferred; an uncertain
   outcome requires reconciliation.
5. Manual completion remains available via `POST /api/agent/complete` or
   `agent_queue.complete`.

## Enrollment enforcement

Off by default so existing callers keep working. Set
`OPU_AGENT_ENROLLMENT_REQUIRED=1` and `agent_queue.claim` / `complete` require
a valid `agent_token` for the claiming `agent_id` (verified against the hashed
registry entry; revoked agents are rejected with HTTP-style 403).

## Test-only executor override

`OPU_AGENT_EXECUTOR_<ADAPTER>` (adapter name uppercased with dots and dashes
mapped to underscores, e.g. `OPU_AGENT_EXECUTOR_DATABASE_ROLLING_OPATCH`)
replaces the mapped executor path in `opu-agent-work-run`. It exists for
tests only and never bypasses the unknown-adapter refusal.

Controller-mediated SSH execute (`execute-next` / `execute-remaining`) remains
supported; the queue is an alternate distribution path for lab pull agents.

## Retry and interrupted claims

The existing plan retry route can reopen a verified failed queue attempt while
later tasks remain queued. It serializes that plan's claim admission, invokes
the native retry safety checks, and republishes only the verified successor
attempt. Old claim tokens cannot renew or complete the successor. If the
controller stops after native retry but before publication, repeating the retry
request completes publication without incrementing the native generation again.
An active or unresolved HTTP execution still prevents retry, and published
queue work continues to exclude HTTP execution of the same plan.

After lease expiry, an operator can use `POST /api/agent/reconcile` with
`job_id` to inspect the attempt. A managed `opu-agent-work-run` claim can return
to the queue only when the native task is still pending and its durable launch
admission was never granted. Reconciliation invalidates the old claim token
before a later worker can launch. Once launch admission is recorded, even a
crash immediately before subprocess creation remains uncertain and requires
verified native terminal evidence. The system does not infer safety from an
expired lease alone.

Historical claims and claim-only/manual workers do not carry managed launch
proof. They retain terminal-evidence-only reconciliation; do not remove their
queue records or blindly requeue a pending task. Managed recovery cannot prove
that an older external worker will not invoke an executor later.

New controller run records also carry a process-incarnation ownership lock.
An operator may reconcile interrupted non-detached work after that exact
controller exits and after verifying no execution remains, even if another
process reused its numeric PID. Missing or replaced lock proof fails closed.
Legacy PID-only records remain conservative: a live reused PID cannot establish
that the original controller exited. Detached executions still require their
existing verified native reconciliation path.
