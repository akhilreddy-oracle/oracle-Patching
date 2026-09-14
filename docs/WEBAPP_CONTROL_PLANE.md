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
