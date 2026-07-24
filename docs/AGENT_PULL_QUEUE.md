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
   The tool maps the job's adapter to its sealed executor (same six-adapter
   mapping as `planctl.LIVE_EXECUTOR_BY_ADAPTER`), extends the queue lease to
   at least 600 seconds before invoking it, runs
   `<executor> execute --plan-id ... --task-id ... --actor <agent-id>`, and
   completes the job with the executor exit code and a stdout SHA-256 digest.
   Unknown adapters and a missing `OPU_PLAN_STATE_DIR` fail closed; a
   non-zero executor exit completes the job with `result.status = "failed"`
   and exits non-zero.
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
