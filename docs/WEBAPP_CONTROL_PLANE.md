# Web control-plane identity and interrupted work

The web server authenticates a principal before it interprets API actors. When
`OPU_WEBAPP_PRINCIPALS_FILE` is set, the default principals file exists, or
`OPU_WEBAPP_RBAC=1`, each principal must have a distinct random Bearer credential.
`OPU_PRODUCTION_MODE=1` also requires principal authentication. A missing,
malformed, empty, or duplicate identity registry rejects requests; it cannot
silently revert to the shared lab token.

The registry format is:

```json
{
  "principals": [
    {
      "actor": "patch-requester",
      "roles": ["requester"],
      "token_sha256": "64 lowercase hexadecimal characters"
    }
  ]
}
```

Create a separate random token for each person or agent. This local command
prints a new credential and the digest to put in that principal's registry entry:

```sh
python3 - <<'PY'
import hashlib, secrets
token = secrets.token_urlsafe(32)
print("Bearer credential:", token)
print("token_sha256:", hashlib.sha256(token.encode()).hexdigest())
PY
```

Store only the digest in the registry and restrict registry permissions to its
owner (`chmod 600`). Give each principal only the roles it needs: `viewer`,
`requester`, `approver`, `operator`, or `admin`. Plan separation of duties still
requires distinct requester, approver, and operator identities. Rotating a token
means replacing its digest; requests validate against the registry each time.
Use TLS whenever credentials cross a network. This filesystem registry is not an
enterprise identity provider or an agent mTLS implementation.

`GET /api/session` (also `/api/auth/whoami`) returns the authenticated `actor`,
`roles`, `rbac_enabled`, and `mode`. `X-OPU-Actor`, `actor`, and `requester`, when
supplied, must match the credential's principal. The server supplies omitted
POST actor fields from that identity. Pipeline writes and execution require
operator/admin; plan/recovery creation, approval, and authorization use their
corresponding roles. API reads require a read-capable role. The health endpoint
remains an unauthenticated liveness check.

For a local development session without configured principals, the existing
`OPU_WEBAPP_TOKEN`/`webapp/var/api-token` mechanism remains available. Explicit
`OPU_WEBAPP_RBAC=0` also selects this lab mode outside production mode. Actor names
are editable in that mode and are not authenticated identities. Startup no longer
prints the credential. JSON request bodies must be objects of at most 1 MiB.

## Reviewed plan execution

Before Dispatch, Execute next or Execute remaining, read
`GET /api/plans/{plan_id}/action-review`. This returns the exact `plan`, `tasks`
and noncredential target routes used for its `confirmation`. Both browser
execution views render that snapshot. Submit its
`confirmation.expected_action_binding_sha256` as the POST field
`expected_action_binding_sha256` on `/dispatch`, `/execute-next` or
`/execute-remaining`. Existing actor, role, CSRF, approval and window rules still
apply; a review digest is not execution authority.

These three HTTP commands reject missing, malformed or stale confirmations with
409 before launching a run. Recheck the displayed plan and targets after a
rejection; do not silently obtain a new digest and repeat the old click. The
worker rechecks the reviewed binding when it starts and under its first native
transport admission lock. Accepted work retains the reviewed host snapshot
through subsequent tasks. Chat uses the same binding contract.

This is an intentional API compatibility change: older clients submitting only
an actor must obtain and present a review. Read-only status, tasks and reports
retain their existing routes. The review endpoint performs no managed-host SSH.

## Interrupted runs

The controller fsyncs a run's ownership before starting its worker. A filesystem
lock serializes durable ownership decisions across controller processes. After a
restart, persisted work without its original worker is exposed as `unknown` and
continues to block the same operation key. Loss of contact with a detached
executor also produces `unknown`; it does not release the operation for retry.

An operator can submit `POST /api/runs/{run_id}/reconcile` with an empty object to
inspect a detached execution. The controller uses the persisted host/node/run
directory, checks the original launch's exit record, reads its output, and
imports plan state. It never starts another executor. A missing exit record,
active process, inaccessible host, or changed host mapping keeps the run
`unknown`. A verified terminal result closes the run and releases its operation
key. Reconciliation of one task in an interrupted execute-remaining run does not
automatically launch the remaining tasks; explicitly resume the plan afterward.

Each launch has a unique remote log directory. Previous launch logs and executor
attempt evidence are retained on retries. Do not delete those directories to
force a retry.

New detached launches record a digest of the complete effective configured host
mapping, including privilege settings, before the first launch SSH call.
Reconciliation, execution observation and lock recovery verify this mapping
before contacting the host. Configuration drift or a missing legacy digest
blocks those remote operations. Saved logs remain readable; missing historical
authority is never reconstructed from current configuration. This digest covers
the application's host inventory, not external OpenSSH alias files or host-key
trust configuration, which remain deployment responsibilities.

For interrupted work that never registered a detached executor, first verify the
old controller has exited and no child operation remains active. An operator may
then send:

```json
{
  "confirm_no_active_execution": true,
  "note": "Evidence/reference for the manual process and target-state check"
}
```

This records an audited failed/interrupted outcome. It cannot clear a detached
execution with an unknown outcome or work owned by a still-running controller.
Corrupt persisted run JSON fails closed and requires restoration from retained
state/evidence before new launches.

## Target identity and recovery fixtures

Plan and recovery APIs return explicit `host_id` fields. New live plans record
the requested host independently of the chosen plan ID; historical plans can be
attributed through their bound canonical readiness document. Unknown attribution
is `null`, and plan-name prefixes never establish host identity. Lists support
`?host_id=...`; unassigned historical records remain visible in global lists.

Recovery demo creation accepts an optional configured `host_id` for workspace
association. It still runs against a disposable fake Oracle home. Request IDs
must be identifiers contained under the recovery fixture root, and an existing
request directory is never replaced. Use a new request ID for another demo;
preserve earlier state and backup evidence.

The no-network boundary regressions are run with
`python3 -B tests/webapp_control.py`. They exercise real HTTP handler dispatch,
principal matching, role checks, body rejection, fixture containment, explicit
host filtering, cross-process ownership, and detached-run reconciliation.
