# Oracle Linux Bash Agent MVP

## Implemented boundary

The current agent is a local, read-only execution kernel. It proves the
operation registry, evidence envelope, durable completion, idempotency, and
discovery contract before network leasing or patch execution is introduced.

Registered operations:

| Operation | Version | Mutability | Current sources |
| --- | --- | --- | --- |
| `host.discover` | 1 | read-only | `/etc/os-release`, hostname, machine ID, kernel |
| `oracle_homes.discover` | 1 | read-only | `/etc/oratab`, `/etc/oraInst.loc`, central inventory XML |

The central-inventory parser intentionally reports `partial` coverage. Its
current conservative parser records same-line `HOME` entries without claiming
full XML correctness. An XML-aware, fixture-certified collector must replace
that limitation before the discovery accuracy gate closes.

## State layout

The development and production default is `/var/lib/oracle-patching-agent`:

```text
locks/<idempotency-key>.lock
runs/<task-id>/events.jsonl
runs/<task-id>/stdout.log
runs/<task-id>/stderr.log
runs/<task-id>/payload.json
runs/<task-id>/request.sha256
runs/<task-id>/result.json
task-bindings/<task-id>
results/<idempotency-key>/complete
results/<idempotency-key>/completion.sha256
results/<idempotency-key>/exit_code
results/<idempotency-key>/request.sha256
results/<idempotency-key>/result.json
results/<idempotency-key>/result.sha256
```

A completion is published as a same-filesystem directory rename. A repeated
delivery returns the original result. An existing run without a completion is
classified as an unknown outcome and is not silently rerun.

## Local verification

```bash
make check
```

The tests use filesystem fixtures and never require or invoke Oracle software.
The next verification layer will run the same suite inside Oracle Linux 8 and 9
containers with ShellCheck installed.

## Discovery outside the agent registry

The registry itself still exposes only the two read-only operations above.
Richer discovery now exists as a separate read-only tool,
`bin/opu-topology-discover`: OS and host identity, Oracle homes (oratab plus
central inventory), Clusterware membership, registered databases and services,
OPatch version and patch inventory (`lsinventory -xml` with an `lspatches`
fallback), and SQL-derived database state via local `sqlplus / as sysdba`.
That tool runs locally per host and feeds the control plane's snapshot
reconciliation; it is not routed through the agent operation registry.

## Lab pull queue, enrollment, and run bridge

Separately from this registry, the lab webapp provides a filesystem pull
queue and enrollment layer (see `docs/AGENT_PULL_QUEUE.md`):

- `bin/opu-agent-enroll` mints per-agent tokens (stored hashed) in
  `webapp/var/agent-registry/agents.json`.
- `bin/opu-agent-work-pull` claims a queued sealed-plan task for a node.
- `bin/opu-agent-work-run` claims a task and invokes the mapped sealed plan
  executor, then completes the queue job with the executor outcome.

`opu-agent-work-run` is a lab bridge: the sealed executors remain the only
mutation boundary, and the enrollment tokens are a lab identity layer, not an
enrolled mTLS agent.

## Explicitly not implemented

- mTLS enrollment, heartbeat streams, or network task leasing (the lab queue
  and token enrollment are filesystem-local stand-ins)
- signed JSON task envelopes and parameter schemas
- runtime-process discovery inside the agent registry (registry operations
  remain the two read-only discovery ops; broader discovery lives in
  `opu-topology-discover`)
- timeouts, output-size enforcement, redaction rules, or privilege brokerage
- mutation operations in the agent registry: no patch staging, stop/start,
  OPatch apply, datapatch, rollback, or recovery is registered

These omissions are gates, not implicit behavior. No unregistered action can be
requested through the current CLI.

## Next slice

1. Add a signed/fixture task-envelope contract with exact-field validation.
2. Replace central-inventory parsing with an XML-aware collector.
3. Add runtime process discovery and source reconciliation.
4. Add read-only OPatch version and `lsinventory` fixture adapters.
5. Add forced-interruption, restart-reconciliation, and stale-lock journal tests.
