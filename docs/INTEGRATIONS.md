# Enterprise integrations (lab slice)

This document covers the S12 integration surfaces of the lab webapp:
outbound notifications (monitoring/ChatOps bridge) and the inbound ITSM
change-ticket gate. Both are stdlib-only and file-backed, matching the rest
of the lab control plane.

## Fail-safe semantics

The two directions have deliberately opposite failure behavior:

- **Outbound never blocks.** `notifications.emit()` never raises. A webhook
  that is down, slow (>5s), or misconfigured is recorded in a dead-letter log
  and the control-plane action proceeds unchanged. A broken monitoring
  integration cannot fail an approval, a dispatch, or a run.
- **Inbound ITSM fails closed.** When enforcement is on
  (`OPU_ITSM_REQUIRED=1`), plan approval requires a change ticket that exists
  in the registry with state `approved`. A missing, unreadable, or malformed
  registry **rejects** the approval (HTTP 403) — an external integration
  failure can never silently approve work (S12 acceptance).

## Outbound notifications

Module: `webapp/notifications.py`.

Config file: `webapp/var/notifications.json` (override with
`OPU_NOTIFICATIONS_FILE`):

```json
{
  "webhooks": [
    {"url": "http://127.0.0.1:9000/hook", "events": ["plan.*", "run.failed"]}
  ]
}
```

- `events` patterns are shell-style globs (`fnmatch`) matched against the
  event name.
- Delivery is an HTTP POST of `{"event": ..., "payload": ..., "emitted_at": ...}`
  with a 5-second timeout and no retries (lab).
- Every emitted event is appended to the audit log `webapp/var/events.jsonl`
  (override `OPU_EVENTS_FILE`) regardless of webhook configuration.
- Failed deliveries are appended to
  `webapp/var/notifications-deadletter.jsonl` (override
  `OPU_NOTIFICATIONS_DEADLETTER_FILE`) with the error and the original record.
- Events are emitted only after the underlying action result is durable
  (plan state written, run record persisted).

### Event catalog

| Event                    | Emitted when                                              |
| ------------------------ | --------------------------------------------------------- |
| `plan.created`           | Plan create (or rollback-plan create) run succeeded        |
| `plan.approved`          | Plan approve run succeeded                                 |
| `plan.authorized`        | Plan authorize run succeeded                               |
| `plan.dispatched`        | Plan dispatch run succeeded                                |
| `plan.execute.succeeded` | execute-next / execute-remaining run succeeded             |
| `plan.execute.failed`    | execute-next / execute-remaining raised                    |
| `run.failed`             | Any async run record ended `failed` (from pipeline_runner) |

## Inbound ITSM change-ticket gate

Module: `webapp/itsm.py`.

Registry file: `webapp/var/change-tickets.json` (override with
`OPU_ITSM_TICKETS_FILE`):

```json
{
  "tickets": [
    {
      "ticket": "CHG00123",
      "state": "approved",
      "window_start": "2026-07-24T00:00:00Z",
      "window_end": "2026-07-25T00:00:00Z"
    }
  ]
}
```

Enforcement switch: `OPU_ITSM_REQUIRED=1` (default off). When on, the
`POST /api/plans/{id}/approve` handler validates `approval_ticket` against
the registry **before** starting the approve run and rejects with 403
(`{"error": "itsm_rejected", "message": ...}`) when the ticket is unknown,
not `approved`, or the registry itself is unusable (fail-closed).

## API endpoints

| Endpoint            | Auth               | Purpose                                                                  |
| ------------------- | ------------------ | ------------------------------------------------------------------------ |
| `GET /api/health`   | none               | Liveness probe: `{"status": "ok", "time": ...}`                           |
| `GET /api/itsm/tickets` | token + read role | Registry contents plus `enabled` flag                                  |
| `GET /api/events`   | token + read role  | Tail of `events.jsonl` (`?limit=N`, default 100, max 1000)                |
| `GET /api/metrics`  | token + read role  | `runs_by_status`, event counts by name, dead-letter count, `itsm_enabled` |

The plan approve form in the UI shows a banner listing approved ticket IDs
whenever enforcement is on (fetched from `/api/itsm/tickets`).

## Tests

`tests/integrations.sh` covers: audit-log emission, dead-lettering on an
unreachable webhook, `run.failed` emission from a failed run, ticket
validation (approved / wrong-state / unknown / empty), fail-closed behavior
for corrupt and missing registries, and metrics counting from a synthetic
events file. It never starts the HTTP server.

## What production would add

- A real ITSM client (ServiceNow / Remedy REST API) with caching and a
  signed local mirror instead of a hand-edited JSON registry, plus CMDB
  correlation of tickets to targets and window enforcement at authorize time.
- A durable outbound queue with retries, backoff, and dead-letter replay
  instead of fire-and-forget webhooks; delivery signing (HMAC) for receivers.
- Prometheus exposition format for `/api/metrics` and alert rules on
  `run.failed` / dead-letter growth, rather than ad-hoc JSON counters.
